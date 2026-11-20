# Observability

adas-mcp uses the standard OpenTelemetry Python setup. It has no custom switch and does no SDK
setup of its own. Run it under the zero-code launcher, `opentelemetry-instrument`, and the SDK
reads the standard `OTEL_*` variables: OTLP over grpc or http/protobuf, Prometheus, logs, log
correlation, propagators, and `OTEL_SDK_DISABLED`.

The Docker image already runs this way, with every exporter defaulting to `none`, so a run
without a collector stays quiet. Setting `OTEL_TRACES_EXPORTER` / `OTEL_METRICS_EXPORTER` /
`OTEL_LOGS_EXPORTER` turns each signal on. The image defaults are listed in
[configuration](configuration.md#docker-image).

## What is recorded

ADAS traces the requests that reach it. adas-mcp covers the layer in front of them, which ADAS
cannot see:

- **One `SERVER` span per `tools/call`.** The span is named `tools/call <tool>` and carries
  attributes from the OpenTelemetry MCP semantic conventions: `mcp.method.name`,
  `gen_ai.tool.name`, `gen_ai.operation.name`, `jsonrpc.request.id`, `mcp.session.id` and
  `network.transport`.
- **Error envelopes become span errors.** Tools never raise, so at the protocol level a refused
  call is an ordinary result. The envelope's `code` becomes the span's `error.type` and status,
  with `adas.error.code`, plus `adas.error.status` when ADAS answered. Failures that never reach
  ADAS are recorded the same way, such as `invalid_argument` and argument-validation errors
  (`error.type=tool_error`).
- **One trace from agent to ADAS.** A `traceparent` sent by the MCP client in the request's
  `_meta`, or as an HTTP header on streamable-http, parents the span. The auto-instrumented
  httpx call to ADAS is a child span and passes `traceparent` on.
- **Metrics.** The `mcp.server.operation.duration` histogram by tool and `error.type`, plus
  httpx's `http.client.request.duration` for the calls to ADAS.
- **Logs.** `OTEL_LOGS_EXPORTER=otlp` ships the Python logs, and
  `OTEL_PYTHON_LOG_CORRELATION=true` adds `trace_id`/`span_id` to every stderr line.

## What is not recorded

Tool arguments and results are never recorded: SQL text and request bodies are written by an
LLM and may carry user data. Error text is left out for the same reason. A span's status
description is the error code or exception type, never the message, because ADAS errors and
pydantic validation errors quote the input.

## Installation

The code depends only on `opentelemetry-api`, which is a no-op without an SDK. The SDK,
exporters and instrumentations are the optional `otel` extra (`pip install 'adas-mcp[otel]'`).
The Docker image and `uv sync` (through the dev dependency group) include it.

## Example configuration

A typical deployment:

```yaml
OTEL_RESOURCE_ATTRIBUTES: "service.name=adas-mcp"
OTEL_TRACES_EXPORTER: "otlp"
OTEL_METRICS_EXPORTER: "otlp,prometheus"   # prometheus serves :9464/metrics
OTEL_LOGS_EXPORTER: "otlp"
OTEL_PYTHON_LOG_CORRELATION: "true"
OTEL_EXPORTER_OTLP_ENDPOINT: "http://<collector-host>:4317"
OTEL_EXPORTER_OTLP_PROTOCOL: "grpc"        # or http/protobuf on :4318
```

Locally, without Docker:

```bash
ADAS_BASE_URL=http://localhost:5000 \
  OTEL_TRACES_EXPORTER=otlp OTEL_METRICS_EXPORTER=none OTEL_LOGS_EXPORTER=none \
  uv run opentelemetry-instrument adas-mcp --transport http
```

On `--transport stdio`, don't use the `console` exporters. They print to stdout, which is the
MCP protocol stream.
