from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import ConfigError

_VALID_CAPABILITIES = ("limited", "full")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


def capabilities_from_env() -> str:
    """Read and validate ADAS_CAPABILITIES without requiring a base URL.

    Kept independent of the rest of Settings so the tool surface can be built
    at import time (in server.py) while ADAS_BASE_URL stays lazy — it is only
    required when a tool actually fires.
    """

    value = os.environ.get("ADAS_CAPABILITIES", "limited").strip().lower()
    if value not in _VALID_CAPABILITIES:
        raise ConfigError(
            f"ADAS_CAPABILITIES must be one of {_VALID_CAPABILITIES}, got {value!r}"
        )
    return value


def execute_sql_enabled_from_env() -> bool:
    """Read and validate ADAS_ENABLE_EXECUTE_SQL — the `adas_execute_sql` flag.

    Deliberately independent of ADAS_CAPABILITIES: executing SQL touches
    ClickHouse directly, so it is opt-in on its own switch and stays **off by
    default** even under `full`. Like capabilities, it is validated without a
    base URL so the tool surface can be built at import time.
    """

    raw = os.environ.get("ADAS_ENABLE_EXECUTE_SQL", "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ConfigError(
        "ADAS_ENABLE_EXECUTE_SQL must be a boolean "
        f"({'|'.join(sorted(_TRUE_VALUES))} / {'|'.join(sorted(v for v in _FALSE_VALUES if v))}), "
        f"got {raw!r}"
    )


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str | None
    timeout_seconds: float
    capabilities: str
    execute_sql_enabled: bool

    @classmethod
    def from_env(cls) -> Settings:
        base = os.environ.get("ADAS_BASE_URL")
        if not base:
            raise ConfigError("ADAS_BASE_URL is required")
        timeout_raw = os.environ.get("ADAS_HTTP_TIMEOUT", "30")
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise ConfigError(
                f"ADAS_HTTP_TIMEOUT must be numeric, got {timeout_raw!r}"
            ) from exc
        return cls(
            base_url=base.rstrip("/"),
            api_key=os.environ.get("ADAS_API_KEY") or None,
            timeout_seconds=timeout,
            capabilities=capabilities_from_env(),
            execute_sql_enabled=execute_sql_enabled_from_env(),
        )
