from __future__ import annotations

import atexit
import logging
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .auth import extract_user_jwt
from .config import Settings
from .errors import MissingAuthError, status_to_code, structured_error

log = logging.getLogger("adas_mcp.client")


_CORRELATION_HEADERS = ("X-Correlation-ID", "X-Request-ID", "X-Trace-ID")


def path_segment(value: Any) -> str:
    """Percent-encode one caller-supplied value for use as a single path segment.

    Path parameters (`name`, `run_id`, `version`, …) arrive from an LLM, so they
    are escaped rather than interpolated. httpx resolves the merged URL before
    sending, so an unescaped separator silently retargets the request at a
    different endpoint: `id="../tables/dial_usage_log"` turns
    `DELETE /v1/saved-queries/{id}` into `DELETE /v1/tables/dial_usage_log`,
    `"x?a=1"` injects a query parameter, and `"x#y"` truncates to `x`. Escaped,
    all three reach the intended endpoint and come back as an honest 400/404.

    Dot-only segments are encoded explicitly: `quote` treats `.` as unreserved,
    so `".."` would survive to the URL and still be removed as a dot segment.
    Every valid ADAS name/id (identifiers and UUIDs) is unaffected by escaping.
    """

    text = str(value)
    if text in (".", ".."):
        return text.replace(".", "%2E")
    return quote(text, safe="")


def _read_request_header(ctx: Any, name: str) -> str | None:
    request_context = getattr(ctx, "request_context", None) if ctx is not None else None
    if request_context is None:
        return None
    meta = getattr(request_context, "meta", None)
    if isinstance(meta, dict):
        for key in (name, name.lower(), name.replace("-", "_").lower()):
            if key in meta and meta[key]:
                return str(meta[key])
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None) if request is not None else None
    if headers is None:
        return None
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
    except AttributeError:
        return None
    return None


def _correlation_headers(ctx: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in _CORRELATION_HEADERS:
        value = _read_request_header(ctx, name)
        if value:
            result[name] = value
    if "X-Correlation-ID" not in result:
        result["X-Correlation-ID"] = uuid.uuid4().hex
    return result


class AdasClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def _build_headers(
        self,
        ctx: Any,
        *,
        require_auth: bool,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        headers.update(_correlation_headers(ctx))
        jwt = extract_user_jwt(ctx)
        # ADAS accepts a Bearer JWT/opaque token (Authorization) or a DIAL
        # API key (Api-Key header). Forward the end-user identity when present
        # so the request runs under their roles; fall back to the admin key.
        forwarded_key = _read_request_header(ctx, "Api-Key")
        if jwt:
            headers["Authorization"] = f"Bearer {jwt}"
        elif forwarded_key:
            headers["Api-Key"] = forwarded_key
        elif self._settings.api_key:
            headers["Api-Key"] = self._settings.api_key
        elif require_auth:
            raise MissingAuthError(
                "No credentials available. Forward a Bearer token or an "
                "Api-Key, or set ADAS_API_KEY. (Not needed when ADAS runs "
                "with SECURITY_MODE=none.)"
            )
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ctx: Any = None,
        require_auth: bool = True,
    ) -> dict[str, Any]:
        """Send one request. `files` makes it `multipart/form-data` (httpx's
        `files` shape, `{part: (filename, content, content_type)}`); it is
        exclusive with `json`."""

        hdrs = self._build_headers(ctx, require_auth=require_auth, extra=headers)
        return await self._send(
            method, path, json=json, files=files, params=params, headers=hdrs
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any,
        params: dict[str, Any] | None,
        headers: dict[str, str],
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        corr = headers.get("X-Correlation-ID", "-")
        try:
            response = await self._http.request(
                method,
                path,
                json=json,
                files=files,
                params=_clean_params(params),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            log.info("adas %s %s -> transport_error [corr=%s]", method, path, corr)
            return structured_error(
                "internal_error",
                f"transport error calling {path}: {exc}",
                path=path,
            )
        log.info("adas %s %s -> %s [corr=%s]", method, path, response.status_code, corr)
        return _parse_response(response, path=path)


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    cleaned = {k: v for k, v in params.items() if v is not None}
    return cleaned or None


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        text = response.text
        return {"raw": text} if text else None


def _parse_response(response: httpx.Response, *, path: str) -> dict[str, Any]:
    status = response.status_code
    body = _parse_body(response)
    if 200 <= status < 300:
        if body is None:
            return {"ok": True}
        if isinstance(body, list):
            return {"items": body}
        if isinstance(body, dict):
            return body
        return {"value": body}

    message = None
    details: Any = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        details = body
    elif isinstance(body, str):
        message = body
    if not message:
        message = f"{status} {response.reason_phrase or 'error'} from {path}"
    return structured_error(
        status_to_code(status),
        str(message),
        status=status,
        path=path,
        details=details,
    )


_client: AdasClient | None = None


def get_client() -> AdasClient:
    global _client
    if _client is None:
        _client = AdasClient(Settings.from_env())
        atexit.register(_close_client_sync)
    return _client


def _close_client_sync() -> None:
    global _client
    client = _client
    _client = None
    if client is None:
        return
    import asyncio

    try:
        asyncio.run(client.aclose())
    except RuntimeError:
        pass


def reset_client_for_tests() -> None:
    global _client
    _client = None
