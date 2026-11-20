"""OpenTelemetry spans and metrics for MCP tool calls.

Written against the OpenTelemetry **API** only, so it costs nothing until an
SDK is configured. adas-mcp does no SDK setup of its own: run it under the
standard zero-code launcher, `opentelemetry-instrument adas-mcp …`, and the SDK,
its exporters (OTLP over grpc or http, Prometheus), log correlation and the
httpx instrumentation are all configured from the standard `OTEL_*` variables.
The Docker image does this already (see the Dockerfile).

What only this layer can report, and so what this module adds:

* one SERVER span per `tools/call`, named and attributed after the
  OpenTelemetry MCP semantic conventions (`mcp.method.name`,
  `gen_ai.tool.name`, …);
* the outcome a tool reports in its **error envelope**. Tools never raise, so
  at the protocol level a refused call looks like a success. The envelope's
  `code` becomes the span's `error.type` and status. Argument-validation
  failures, which never leave this process, are marked the same way;
* the caller's trace context: a `traceparent` the MCP client sends in the
  request's `_meta` (the MCP convention, and the only carrier on stdio) or as
  an HTTP header parents the span. The auto-instrumented httpx client then
  carries it on to ADAS;
* the `mcp.server.operation.duration` histogram, by tool and `error.type`.

Tool arguments and results are never recorded: SQL text and request bodies are
LLM-authored and may carry user data. That covers error text too: a span's
status description is the error code or exception type, never a message.
"""

from __future__ import annotations

import contextvars
import functools
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context as OtelContext
from opentelemetry.metrics import MeterProvider
from opentelemetry.trace import SpanKind, Status, StatusCode, TracerProvider

INSTRUMENTATION_NAME = "adas_mcp"

# Semantic-convention names, spelled out rather than imported from the
# incubating semconv modules, whose import paths are not stable.
ATTR_MCP_METHOD = "mcp.method.name"
ATTR_MCP_SESSION = "mcp.session.id"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_OPERATION = "gen_ai.operation.name"
ATTR_REQUEST_ID = "jsonrpc.request.id"
ATTR_TRANSPORT = "network.transport"
ATTR_ERROR_TYPE = "error.type"
# Project-specific: the error envelope's own fields.
ATTR_ADAS_ERROR_CODE = "adas.error.code"
ATTR_ADAS_ERROR_STATUS = "adas.error.status"

METRIC_OPERATION_DURATION = "mcp.server.operation.duration"
# Bucket boundaries the MCP semantic conventions recommend for this histogram.
_DURATION_BUCKETS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60, 120, 300]

# The error.type the conventions prescribe for a call that failed inside the
# tool layer (FastMCP turns those into an `isError` result).
TOOL_ERROR = "tool_error"

# Stands in for a tool name that is not registered, in the metric only: the name
# comes from the client, and an unbounded set of values would blow up the
# histogram's cardinality. The span keeps the name as sent.
_OTHER_TOOL = "_OTHER"

_PROPAGATION_KEYS = ("traceparent", "tracestate", "baggage")


def _own_version() -> str:
    try:
        return package_version("adas-mcp")
    except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
        return "0+unknown"


@dataclass
class _Outcome:
    """What the tool function reported, handed from the tool wrapper (which
    sees the raw return value) to `call_tool` (which owns the span)."""

    error_code: str | None = None
    error_status: int | None = None


_outcome: contextvars.ContextVar[_Outcome | None] = contextvars.ContextVar(
    "adas_mcp_tool_outcome", default=None
)


class InstrumentedFastMCP(FastMCP):
    """FastMCP with a span and a duration sample around every `tools/call`.

    `call_tool` is the method FastMCP binds as its `tools/call` handler, so
    overriding it covers argument validation and unknown tools as well as the
    tool body. Each registered tool function is additionally wrapped so the
    error envelope it returns — invisible once FastMCP has serialized it — is
    reported on the span.

    The providers default to the global ones the SDK installs. Tests pass their
    own.
    """

    def __init__(
        self,
        *args: Any,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        ver = _own_version()
        self._tracer = trace.get_tracer(
            INSTRUMENTATION_NAME, ver, tracer_provider=tracer_provider
        )
        self._duration = metrics.get_meter(
            INSTRUMENTATION_NAME, ver, meter_provider=meter_provider
        ).create_histogram(
            METRIC_OPERATION_DURATION,
            unit="s",
            description="Duration of MCP requests handled by the server.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS,
        )

    def add_tool(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        name = kwargs.get("name") or (args[0] if args else None) or fn.__name__
        # A duplicate name keeps the tool already registered (FastMCP only
        # warns), so it must not be wrapped a second time.
        is_new = self._tool_manager.get_tool(name) is None
        super().add_tool(fn, *args, **kwargs)
        tool = self._tool_manager.get_tool(name)
        if is_new and tool is not None and tool.is_async:
            tool.fn = _report_envelope(tool.fn)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[Any] | dict[str, Any]:
        request_context = _request_context(self)
        attributes: dict[str, Any] = {
            ATTR_MCP_METHOD: "tools/call",
            ATTR_TOOL_NAME: name,
            ATTR_OPERATION: "execute_tool",
            **_request_attributes(request_context),
        }
        outcome = _Outcome()
        token = _outcome.set(outcome)
        error_type: str | None = None
        started = time.perf_counter()
        try:
            with self._tracer.start_as_current_span(
                f"tools/call {name}",
                context=_parent_context(request_context),
                kind=SpanKind.SERVER,
                attributes=attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    result = await super().call_tool(name, arguments)
                except ToolError as exc:
                    # Never the message: FastMCP builds it from the failure,
                    # and a pydantic ValidationError quotes the arguments.
                    error_type = TOOL_ERROR
                    cause = exc.__cause__ or exc
                    span.set_attribute(ATTR_ERROR_TYPE, error_type)
                    span.set_status(Status(StatusCode.ERROR, type(cause).__qualname__))
                    raise
                except Exception as exc:
                    # No `record_exception`: its event carries the message.
                    error_type = type(exc).__qualname__
                    span.set_attribute(ATTR_ERROR_TYPE, error_type)
                    span.set_status(Status(StatusCode.ERROR, error_type))
                    raise
                if outcome.error_code is not None:
                    error_type = outcome.error_code
                    span.set_attribute(ATTR_ERROR_TYPE, error_type)
                    span.set_attribute(ATTR_ADAS_ERROR_CODE, outcome.error_code)
                    if outcome.error_status is not None:
                        span.set_attribute(ATTR_ADAS_ERROR_STATUS, outcome.error_status)
                    # The envelope's message is not recorded: ADAS and
                    # ClickHouse errors quote identifiers and SQL fragments.
                    span.set_status(Status(StatusCode.ERROR, outcome.error_code))
                return result
        finally:
            _outcome.reset(token)
            metric_attributes = {
                ATTR_MCP_METHOD: "tools/call",
                ATTR_TOOL_NAME: name
                if self._tool_manager.get_tool(name) is not None
                else _OTHER_TOOL,
            }
            if error_type is not None:
                metric_attributes[ATTR_ERROR_TYPE] = error_type
            self._duration.record(time.perf_counter() - started, metric_attributes)


def _report_envelope(fn: Callable[..., Any]) -> Callable[..., Any]:
    # Wrapped after registration: FastMCP has already derived the argument
    # schema from `fn`, and calls `tool.fn` with keyword arguments only.
    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        result = await fn(**kwargs)
        outcome = _outcome.get()
        error = result.get("error") if isinstance(result, dict) else None
        if outcome is not None and isinstance(error, dict) and "code" in error:
            outcome.error_code = str(error["code"])
            status = error.get("status")
            outcome.error_status = status if isinstance(status, int) else None
        return result

    return wrapper


# --------------------------------------------------------------------------
# Request context → attributes and parent trace context


def _request_context(server: FastMCP) -> Any:
    try:
        return server.get_context().request_context
    except (LookupError, ValueError):
        return None


def _http_headers(request_context: Any) -> Mapping[str, str] | None:
    request = getattr(request_context, "request", None)
    return getattr(request, "headers", None)


def _request_attributes(request_context: Any) -> dict[str, Any]:
    if request_context is None:
        return {}
    attributes: dict[str, Any] = {}
    request_id = getattr(request_context, "request_id", None)
    if request_id is not None:
        attributes[ATTR_REQUEST_ID] = str(request_id)
    headers = _http_headers(request_context)
    # No HTTP request behind the call means stdio, which the conventions
    # report as "pipe".
    attributes[ATTR_TRANSPORT] = "pipe" if headers is None else "tcp"
    if headers is not None:
        session_id = headers.get("mcp-session-id")
        if session_id:
            attributes[ATTR_MCP_SESSION] = session_id
    return attributes


def _parent_context(request_context: Any) -> OtelContext | None:
    """Extract the caller's trace context: `params._meta` first (where the MCP
    conventions put it, and the only carrier on stdio), then HTTP headers."""

    if request_context is None:
        return None
    carrier: dict[str, str] = {}
    meta = getattr(request_context, "meta", None)
    if meta is not None:
        values = meta if isinstance(meta, dict) else _model_fields(meta)
        for key in _PROPAGATION_KEYS:
            if values.get(key):
                carrier[key] = str(values[key])
    headers = _http_headers(request_context)
    if "traceparent" not in carrier and headers is not None:
        for key in _PROPAGATION_KEYS:
            value = headers.get(key)
            if value:
                carrier[key] = value
    return propagate.extract(carrier) if carrier else None


def _model_fields(model: Any) -> dict[str, Any]:
    dump = getattr(model, "model_dump", None)
    return dump() if callable(dump) else {}
