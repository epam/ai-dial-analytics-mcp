from __future__ import annotations

import os

from .config import capabilities_from_env, execute_sql_enabled_from_env
from .telemetry import InstrumentedFastMCP
from .tools import register_all

_HOST = os.environ.get("FASTMCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("FASTMCP_PORT", "5000"))

# Instrumented unconditionally: it speaks only the OpenTelemetry API, which is a
# no-op until the SDK is configured (`opentelemetry-instrument`, see telemetry).
mcp = InstrumentedFastMCP("adas-mcp", host=_HOST, port=_PORT)
# capabilities and the execute-sql flag are validated at import (cheap, no
# network); ADAS_BASE_URL stays lazy and is only required when a tool actually
# calls the backend.
register_all(mcp, capabilities_from_env(), execute_sql_enabled_from_env())


def run(transport: str) -> None:
    if transport == "http":
        transport = "streamable-http"
    mcp.run(transport=transport)
