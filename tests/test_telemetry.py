"""OpenTelemetry instrumentation of MCP tool calls.

Calls go through a real in-memory MCP client session so the `tools/call`
handler, argument validation and `_meta` propagation are the production path.
Providers are injected; in a deployment they are the global ones that
`opentelemetry-instrument` installs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx
import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode

from adas_mcp.__main__ import _configure_logging
from adas_mcp.telemetry import InstrumentedFastMCP
from adas_mcp.tools import register_all

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def metric_reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def tracer_provider(spans: InMemorySpanExporter) -> Iterator[TracerProvider]:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    yield provider
    provider.shutdown()


@pytest.fixture
def server(
    tracer_provider: TracerProvider, metric_reader: InMemoryMetricReader
) -> Iterator[InstrumentedFastMCP]:
    meter_provider = MeterProvider(metric_readers=[metric_reader])
    mcp = InstrumentedFastMCP(
        "otel-test", tracer_provider=tracer_provider, meter_provider=meter_provider
    )
    register_all(mcp, "limited", False)
    yield mcp
    meter_provider.shutdown()


@pytest.fixture
def instrumented_httpx(tracer_provider: TracerProvider) -> Iterator[None]:
    # What `opentelemetry-instrument` does for every installed instrumentation.
    instrumentor = HTTPXClientInstrumentor()
    instrumentor.instrument(tracer_provider=tracer_provider)
    yield
    instrumentor.uninstrument()


def _tool_span(spans: InMemorySpanExporter) -> ReadableSpan:
    (span,) = [s for s in spans.get_finished_spans() if s.name.startswith("tools/call")]
    return span


def _durations(reader: InMemoryMetricReader) -> list[dict]:
    data = reader.get_metrics_data()
    points = []
    for resource_metrics in data.resource_metrics:
        for scope in resource_metrics.scope_metrics:
            for metric in scope.metrics:
                if metric.name == "mcp.server.operation.duration":
                    assert metric.unit == "s"
                    points += [dict(p.attributes) for p in metric.data.data_points]
    return points


@respx.mock
async def test_successful_call_is_a_server_span_with_mcp_attributes(
    server: InstrumentedFastMCP,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
):
    respx.get("http://adas.test/v1/tables/widget_metrics").mock(
        return_value=httpx.Response(200, json={"name": "widget_metrics"})
    )

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("adas_get_table", {"name": "widget_metrics"})

    assert result.isError is False
    span = _tool_span(spans)
    assert span.name == "tools/call adas_get_table"
    assert span.kind is SpanKind.SERVER
    assert span.status.status_code is StatusCode.UNSET
    attrs = dict(span.attributes)
    assert attrs["mcp.method.name"] == "tools/call"
    assert attrs["gen_ai.tool.name"] == "adas_get_table"
    assert attrs["gen_ai.operation.name"] == "execute_tool"
    assert attrs["network.transport"] == "pipe"
    assert "jsonrpc.request.id" in attrs
    assert "error.type" not in attrs
    # Arguments are never recorded.
    assert "widget_metrics" not in str(attrs)

    assert _durations(metric_reader) == [
        {"mcp.method.name": "tools/call", "gen_ai.tool.name": "adas_get_table"}
    ]


@respx.mock
async def test_error_envelope_marks_the_span_as_failed(
    server: InstrumentedFastMCP,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
):
    respx.get("http://adas.test/v1/tables/missing").mock(
        return_value=httpx.Response(404, json={"message": "table not found"})
    )

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("adas_get_table", {"name": "missing"})

    # At the protocol level the envelope is an ordinary result...
    assert result.isError is False
    # ...the span is where the failure shows.
    span = _tool_span(spans)
    assert span.status.status_code is StatusCode.ERROR
    # The code, never ADAS's message, which may quote user input.
    assert span.status.description == "not_found"
    assert span.attributes["error.type"] == "not_found"
    assert span.attributes["adas.error.code"] == "not_found"
    assert span.attributes["adas.error.status"] == 404
    assert _durations(metric_reader)[0]["error.type"] == "not_found"


async def test_failure_before_any_http_is_still_recorded(
    server: InstrumentedFastMCP, spans: InMemorySpanExporter
):
    # invalid_argument is decided locally — ADAS never sees this call.
    with respx.mock(assert_all_called=False) as router:
        async with create_connected_server_and_client_session(server) as session:
            await session.call_tool("adas_get_table", {"name": ""})
        assert router.calls.call_count == 0

    span = _tool_span(spans)
    assert span.attributes["error.type"] == "invalid_argument"
    assert "adas.error.status" not in span.attributes


async def test_argument_validation_failure_is_a_tool_error(
    server: InstrumentedFastMCP,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
):
    # The wrong type makes pydantic quote the value in its error message.
    secret = "SELECT password FROM users"
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("adas_get_table", {"name": {"q": secret}})

    assert result.isError is True
    span = _tool_span(spans)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValidationError"
    assert span.attributes["error.type"] == "tool_error"
    # Neither the status nor an exception event carries the arguments.
    assert span.events == ()
    assert secret not in str(span.to_json())
    assert _durations(metric_reader)[0]["error.type"] == "tool_error"


async def test_an_unwrapped_exception_records_its_type_not_its_message(
    server: InstrumentedFastMCP,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
):
    # FastMCP wraps tool failures in ToolError; anything else (an elicitation
    # error, a failure around the tool) reaches the generic branch.
    secret = "SELECT password FROM users"

    async def boom(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(server._tool_manager, "call_tool", boom)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("adas_get_table", {"name": "x"})

    assert result.isError is True
    span = _tool_span(spans)
    assert span.status.description == "RuntimeError"
    assert span.attributes["error.type"] == "RuntimeError"
    assert span.events == ()
    assert secret not in str(span.to_json())
    assert _durations(metric_reader)[0]["error.type"] == "RuntimeError"


def test_a_duplicate_registration_is_not_wrapped_twice(
    server: InstrumentedFastMCP,
):
    tool = server._tool_manager.get_tool("adas_get_table")
    wrapped = tool.fn

    server.add_tool(wrapped.__wrapped__, name="adas_get_table")

    assert server._tool_manager.get_tool("adas_get_table").fn is wrapped


async def test_unknown_tool_names_do_not_reach_the_metric(
    server: InstrumentedFastMCP,
    spans: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
):
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool("adas_no_such_tool", {})

    assert result.isError is True
    assert _tool_span(spans).attributes["gen_ai.tool.name"] == "adas_no_such_tool"
    assert _durations(metric_reader) == [
        {
            "mcp.method.name": "tools/call",
            "gen_ai.tool.name": "_OTHER",
            "error.type": "tool_error",
        }
    ]


@respx.mock
@pytest.mark.usefixtures("instrumented_httpx")
async def test_meta_traceparent_parents_the_span_and_reaches_adas(
    server: InstrumentedFastMCP, spans: InMemorySpanExporter
):
    route = respx.get("http://adas.test/v1/tables/widget_metrics").mock(
        return_value=httpx.Response(200, json={"name": "widget_metrics"})
    )

    async with create_connected_server_and_client_session(server) as session:
        await session.call_tool(
            "adas_get_table",
            {"name": "widget_metrics"},
            meta={"traceparent": TRACEPARENT},
        )

    span = _tool_span(spans)
    assert format(span.context.trace_id, "032x") == TRACE_ID
    assert format(span.parent.span_id, "016x") == PARENT_SPAN_ID

    # The httpx client span is a child of the tool span, and ADAS receives
    # the same trace in its traceparent.
    (http_span,) = [s for s in spans.get_finished_spans() if s.kind is SpanKind.CLIENT]
    assert http_span.parent.span_id == span.context.span_id
    sent = route.calls.last.request.headers["traceparent"]
    assert sent.split("-")[1] == TRACE_ID
    assert sent.split("-")[2] == format(http_span.context.span_id, "016x")


@respx.mock
async def test_without_caller_context_the_span_starts_a_trace(
    server: InstrumentedFastMCP, spans: InMemorySpanExporter
):
    respx.get("http://adas.test/v1/tables/widget_metrics").mock(
        return_value=httpx.Response(200, json={"name": "widget_metrics"})
    )

    async with create_connected_server_and_client_session(server) as session:
        await session.call_tool("adas_get_table", {"name": "widget_metrics"})

    assert _tool_span(spans).parent is None


async def test_without_an_sdk_the_server_works_and_records_nothing():
    # The API alone (no `opentelemetry-instrument`) is a no-op.
    mcp = InstrumentedFastMCP("no-sdk")
    register_all(mcp, "limited", False)
    with respx.mock:
        respx.get("http://adas.test/v1/tables/widget_metrics").mock(
            return_value=httpx.Response(200, json={"name": "widget_metrics"})
        )
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool(
                "adas_get_table", {"name": "widget_metrics"}
            )
    assert result.isError is False


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    yield root
    root.handlers, root.level = saved_handlers, saved_level


def test_logging_adds_a_stderr_handler_next_to_an_export_handler(
    root_logger: logging.Logger,
):
    export_handler = logging.Handler()  # stands in for OpenTelemetry's
    root_logger.addHandler(export_handler)

    _configure_logging(logging.WARNING)

    assert export_handler in root_logger.handlers
    streams = [h for h in root_logger.handlers if type(h) is logging.StreamHandler]
    assert len(streams) == 1
    assert root_logger.level == logging.WARNING


def test_logging_keeps_an_existing_correlation_handler(root_logger: logging.Logger):
    # OTEL_PYTHON_LOG_CORRELATION=true has already installed a stream handler
    # whose format carries the trace ids.
    correlated = logging.StreamHandler()
    root_logger.addHandler(correlated)

    _configure_logging(logging.DEBUG)

    streams = [h for h in root_logger.handlers if type(h) is logging.StreamHandler]
    assert streams == [correlated]
    assert root_logger.level == logging.DEBUG
