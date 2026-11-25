# Configuration

adas-mcp is configured through environment variables. Only `ADAS_BASE_URL` is required, and it
is read when the first tool call needs it, so the server starts without it.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ADAS_BASE_URL` | yes | — | ADAS backend URL, for example `http://localhost:5000` |
| `ADAS_API_KEY` | no | — | Admin `Api-Key`, used only when a request carries no other credential |
| `ADAS_USER_JWT` | no | — | User JWT for stdio runs in local development |
| `ADAS_HTTP_TIMEOUT` | no | `30` | Timeout for calls to ADAS, in seconds |
| `ADAS_CAPABILITIES` | no | `limited` | `limited` \| `full`. Gates the 16 write-capable tools |
| `ADAS_ENABLE_EXECUTE_SQL` | no | `false` | Registers `adas_execute_sql`. Independent of `ADAS_CAPABILITIES` |
| `FASTMCP_HOST` | no | `127.0.0.1` | Bind host (the Docker image sets `0.0.0.0`) |
| `FASTMCP_PORT` | no | `5000` | Bind port for streamable-http. ADAS also defaults to `5000`, so set a different port (for example `5001`) when running both locally |
| `OTEL_*` | no | — | Standard OpenTelemetry configuration. See [observability](observability.md) |

## Capabilities

Two independent switches decide which tools are registered. The full list is in the
[tool reference](tools.md#tool-surface).

`ADAS_CAPABILITIES` gates the write-capable tools:

- `limited` (default): the 22 read-only tools.
- `full`: those **plus** the 16 write-capable tools.

`ADAS_ENABLE_EXECUTE_SQL` gates `adas_execute_sql` on its own, and is **off by default**.
Accepted values are `1`/`true`/`yes`/`on` and `0`/`false`/`no`/`off`/empty, case-insensitive.
Anything else stops the server with a configuration error.

Executing a query is a separate decision from allowing writes, so neither switch implies the
other. `full` alone does **not** register `adas_execute_sql`, and the flag can be turned on under
`limited` to allow query execution while every write tool stays withheld.

`adas_validate_sql` always registers. It answers whether `adas_execute_sql` would accept a
statement, and reaches neither ClickHouse nor any write path to do it. Without it, a deployment
with execution disabled would have no SQL tool at all and would have to compose statements
blind.

Both switches are a local guardrail, not authorization. ADAS still enforces its own
`FULL_ADMIN` / `READ_ONLY_ADMIN` roles under `SECURITY_MODE=oidc`. **Every** `/v1/pipelines`
endpoint is `FULL_ADMIN` on the backend, including the GETs.

## Authentication

Each request to ADAS carries one credential, picked in this order:

1. A Bearer JWT from the MCP request (its `_meta`, or the `Authorization` header on
   streamable-http).
2. `ADAS_USER_JWT`.
3. An `Api-Key` header forwarded from the MCP request.
4. `ADAS_API_KEY`.

If none is available, the request goes out without credentials. ADAS's default
`SECURITY_MODE=none` needs none. Under `oidc`, ADAS answers 401, which the tool returns as an
`unauthenticated` error.

ADAS applies the caller's own permissions, so each user sees only the tables, pipelines and
saved queries they can access.

## Docker image

The image built from the [`Dockerfile`](../Dockerfile) sets these defaults:

| Variable | Image default |
|---|---|
| `FASTMCP_HOST` / `FASTMCP_PORT` | `0.0.0.0` / `5000` |
| `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` | `none` |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.name=adas-mcp` |
| `OTEL_EXPORTER_PROMETHEUS_HOST` | `0.0.0.0` |

The entrypoint is `opentelemetry-instrument adas-mcp` and the default command is
`--transport http`. The image always includes the `otel` extra, so telemetry is turned on
with environment variables, never by rebuilding.

The default `service.name` is set through `OTEL_RESOURCE_ATTRIBUTES`, so a deployment's own
value for that variable replaces it. `OTEL_SERVICE_NAME` is left unset because it would take
precedence over that value.

The image keeps its attack surface small:

- The runtime stage runs `apt-get upgrade` on the base image. This picks up Debian security
  fixes released after the base image was published, without listing packages one CVE at a
  time.
- The base image's `pip`, `setuptools` and `wheel` are uninstalled. The server runs from the
  self-contained virtualenv in `/app/.venv`, which cannot see the system site-packages, so
  those packages would only ever show up as scanner findings.
- The process runs as the unprivileged `app` user (uid/gid `10001`).

It exposes port `5000` (streamable-http) and `9464` (Prometheus, served only when
`OTEL_METRICS_EXPORTER` includes `prometheus`).
