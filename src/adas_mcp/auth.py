from __future__ import annotations

import os
from typing import Any


def _strip_bearer(token: str | None) -> str | None:
    if not token:
        return None
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None


def _from_headers(headers: Any) -> str | None:
    if headers is None:
        return None
    try:
        items = headers.items()
    except AttributeError:
        try:
            items = list(headers)
        except TypeError:
            return None
    for key, value in items:
        if str(key).lower() == "authorization":
            return _strip_bearer(str(value))
    return None


def extract_user_jwt(ctx: Any) -> str | None:
    """Pull a user JWT out of the FastMCP request context, falling back to env.

    Probes (in order):
      1. ctx.request_context.meta["authorization"] (dict-like)
      2. ctx.request_context.request.headers["authorization"]
      3. ADAS_USER_JWT env var
    """

    request_context = getattr(ctx, "request_context", None) if ctx is not None else None
    if request_context is not None:
        meta = getattr(request_context, "meta", None)
        if isinstance(meta, dict):
            token = _strip_bearer(
                meta.get("authorization") or meta.get("Authorization")
            )
            if token:
                return token
        request = getattr(request_context, "request", None)
        headers = getattr(request, "headers", None) if request is not None else None
        token = _from_headers(headers)
        if token:
            return token
    return _strip_bearer(os.environ.get("ADAS_USER_JWT"))
