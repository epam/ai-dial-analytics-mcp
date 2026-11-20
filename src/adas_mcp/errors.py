from __future__ import annotations

from typing import Any


class ConfigError(RuntimeError):
    pass


class MissingAuthError(RuntimeError):
    pass


_STATUS_TO_CODE: dict[int, str] = {
    400: "invalid_argument",
    401: "unauthenticated",
    403: "permission_denied",
    404: "not_found",
    409: "conflict",
    412: "etag_mismatch",
    # ADAS answers declaration/governance failures with 422 (pipeline_validation_failed,
    # validation_error — what a rejected table declaration reports as — and
    # write_column_not_allowed), not 400. For a caller it means the same thing as 400 —
    # the request is malformed and must be changed — so it maps to the same code rather
    # than falling through to a generic http_error that reads like an unknown failure.
    # The original 422 stays in `status`.
    422: "invalid_argument",
}


def status_to_code(status: int) -> str:
    if status in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status]
    if 500 <= status < 600:
        return "internal_error"
    if 400 <= status < 500:
        return "http_error"
    return "http_error"


def structured_error(code: str, message: str, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    for key, value in fields.items():
        if value is not None:
            body[key] = value
    return {"error": body}


def invalid_argument(message: str, **fields: Any) -> dict[str, Any]:
    return structured_error("invalid_argument", message, **fields)
