# DIAL Analytics MCP

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11+-blue)

adas-mcp is an MCP server for DIAL Analytics. It gives AI agents access to the
[Analytics Data Access Service (ADAS)](https://github.com/epam/ai-dial-analytics). The server,
its command and its Python distribution are all named `adas-mcp`.

It exposes the ADAS REST API as MCP tools, all named `adas_*`. With them an agent can explore
the data catalog, write and check SQL, and manage tables, pipelines, saved queries and
dashboards. It is built on the Python MCP SDK (FastMCP) and runs over stdio for desktop and
coding agents, or over streamable HTTP as a hosted service.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Features](#features)
- [Getting Started](#getting-started)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [Tools](#tools)
- [Monitoring](#monitoring)
- [Security](#security)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A reachable [ADAS](https://github.com/epam/ai-dial-analytics) instance. The test suite mocks
  ADAS and needs nothing running.

## Features

- **Schema discovery:** list queryable entities, their columns, and the SQL and DSL functions
  ADAS accepts.
- **SQL validation and execution:** check a statement without running it, and optionally
  execute it.
- **Table catalog:** read tables, then create, materialize and evolve them.
- **Pipelines:** register and control aggregate rollups and enrichment pipelines, and preview
  their output without writing anything.
- **Data sync:** trigger and track backfill runs, and list and requeue failed ingests.
- **Catalog promotion:** export tables and pipelines from one environment and import them into
  another, with a preview step on both sides.
- **Saved queries and dashboards:** store, share and update queries and dashboard layouts.
- **Instance settings:** read and replace the app-routes allowlist and the sessions source.
- **Read-only by default:** write tools and SQL execution are each switched on separately.
- **Bundled API schema:** agents look up the exact request shapes offline, from a snapshot of
  the ADAS OpenAPI document.
- **OpenTelemetry:** a span per tool call, linked into the trace of the ADAS request.

## Getting Started

### Quick start

Install the dependencies and run the server against a local ADAS:

```bash
uv sync
ADAS_BASE_URL=http://localhost:5000 uv run adas-mcp --transport stdio
```

To register it in Claude Code:

```bash
claude mcp add adas --env ADAS_BASE_URL=http://localhost:5000 -- uv --directory "$PWD" run adas-mcp --transport stdio
```

### Run modes

```bash
# read-only tools only (default)
ADAS_BASE_URL=http://localhost:5000 uv run adas-mcp --transport stdio

# plus the write-capable tools
ADAS_BASE_URL=http://localhost:5000 ADAS_CAPABILITIES=full uv run adas-mcp --transport stdio

# plus SQL execution, still without write tools
ADAS_BASE_URL=http://localhost:5000 ADAS_ENABLE_EXECUTE_SQL=true uv run adas-mcp --transport stdio

# streamable HTTP, served at http://localhost:5001/mcp
ADAS_BASE_URL=http://localhost:5000 FASTMCP_PORT=5001 uv run adas-mcp --transport http
```

The HTTP transport binds port 5000 by default, the same port a local ADAS listens on. When both
run on one machine, set `FASTMCP_PORT` to a free port (`5001` above); stdio needs no port.

### Run with Docker

Released images are published to the GitHub Container Registry and Docker Hub as
`ghcr.io/epam/ai-dial-analytics-mcp` and `epam/ai-dial-analytics-mcp`, for `linux/amd64` and
`linux/arm64`:

```bash
docker run --rm -p 5001:5000 -e ADAS_BASE_URL=http://<adas-host>:5000 ghcr.io/epam/ai-dial-analytics-mcp:<version>
```

Or build it from this repo:

```bash
docker build -t adas-mcp .
docker run --rm -p 5001:5000 -e ADAS_BASE_URL=http://<adas-host>:5000 adas-mcp
```

The image serves streamable HTTP on port 5000 inside the container; the examples publish it on
host port 5001 (`http://localhost:5001/mcp`) so it does not collide with a local ADAS on 5000.
Inside the container `localhost` is the container itself, so point `ADAS_BASE_URL` at a host it
can reach — for an ADAS running on your machine, `http://host.docker.internal:5000` (on Linux, add
`--add-host=host.docker.internal:host-gateway`).

### Build and test

```bash
make test     # run the test suite
make lint     # ruff check + format check, as in CI
make format   # apply ruff fixes and formatting
```

## Deployment

For a hosted, shared instance, follow the
**[deployment and AI Query Helper guide](docs/deployment-guide.md)**. It covers environments
without the DIAL Deployment Manager, in two parts:

1. **Deploy adas-mcp to Kubernetes and register it in DIAL Core.** This is a read-only
   (`limited`) instance on streamable HTTP at `/mcp`, running the published image. The guide
   gives the reference configuration, Deployment and Service manifests, ingress, the DIAL Core
   toolset, and verification steps.
2. **Create the AI Query Helper.** This is a DIAL Quick App that helps analytics UI users write
   ADAS SQL through the adas-mcp toolset, and is connected to the UI by
   `DIAL_QUERY_ASSISTANT_DEPLOYMENT`. Its system prompt is
   [`docs/query-helper-system-prompt.md`](docs/query-helper-system-prompt.md).

## Configuration

adas-mcp is configured through environment variables. Only `ADAS_BASE_URL` is required. The
full reference is [`docs/configuration.md`](docs/configuration.md).

Main settings:

- **Backend:** `ADAS_BASE_URL` and `ADAS_HTTP_TIMEOUT`.
- **Credentials:** `ADAS_API_KEY` (admin fallback) and `ADAS_USER_JWT` (local stdio runs).
  Neither is needed when ADAS runs with `SECURITY_MODE=none`.
- **Tool surface:** `ADAS_CAPABILITIES` (`limited` | `full`) and `ADAS_ENABLE_EXECUTE_SQL`.
- **Server:** `FASTMCP_HOST` and `FASTMCP_PORT`.
- **Telemetry:** the standard `OTEL_*` variables.

## Tools

The server registers up to 39 tools. What an instance exposes depends on two switches:

| Set | Tools | Enabled by |
|---|---|---|
| Read-only | 22 | always |
| SQL execution | 1 (`adas_execute_sql`) | `ADAS_ENABLE_EXECUTE_SQL=true` |
| Write-capable | 16 | `ADAS_CAPABILITIES=full` |

| Area | Example tools |
|---|---|
| Schema and discovery | `adas_describe_schema`, `adas_list_entities`, `adas_describe_entity`, `adas_list_sql_functions` |
| SQL | `adas_validate_sql`, `adas_execute_sql` |
| Tables | `adas_list_tables`, `adas_create_table`, `adas_define_table_schema`, `adas_patch_table_schema` |
| Pipelines | `adas_list_pipelines`, `adas_preview_pipeline`, `adas_create_pipeline`, `adas_patch_pipeline` |
| Data sync | `adas_trigger_backfill`, `adas_get_backfill_status`, `adas_list_data_sync_failures`, `adas_retry_data_sync_failures` |
| Catalog promotion | `adas_export_catalog`, `adas_preview_catalog_import`, `adas_import_catalog` |
| Saved queries | `adas_list_saved_queries`, `adas_create_saved_query`, `adas_replace_saved_query` |
| Dashboards | `adas_list_dashboards`, `adas_create_dashboard`, `adas_replace_dashboard` |
| Settings | `adas_get_setting`, `adas_replace_setting` |

Tools never raise errors to the client. A failed call returns an
`{"error": {"code", "message", ...}}` envelope instead. Each tool's description carries its
full contract. The complete list, with the ADAS endpoint behind each tool and the endpoints left
unwrapped, is in the [tool reference](docs/tools.md).

## Monitoring

adas-mcp uses the standard zero-code OpenTelemetry setup, and the Docker image runs under
`opentelemetry-instrument`. Every exporter is off by default. Setting the standard `OTEL_*`
variables turns on traces, metrics (OTLP, or Prometheus on port 9464) and logs.

Tool arguments, results and error messages are never recorded. See
[observability](docs/observability.md).

## Security

The capability switches are a local guardrail, not authorization. ADAS enforces access itself:
under `SECURITY_MODE=oidc`, each request runs with the caller's own credentials and
permissions. adas-mcp forwards a user JWT or an `Api-Key` and falls back to `ADAS_API_KEY`. See
[authentication](docs/configuration.md#authentication).

Keep `ADAS_CAPABILITIES=limited` and leave `ADAS_ENABLE_EXECUTE_SQL` unset on any instance
that should stay read-only.

To report a vulnerability, follow [SECURITY.md](SECURITY.md). Do not open a public issue.

## Documentation

| Topic | Document |
|---|---|
| Tool reference | [docs/tools.md](docs/tools.md) |
| Configuration reference | [docs/configuration.md](docs/configuration.md) |
| Observability | [docs/observability.md](docs/observability.md) |
| Development and the bundled OpenAPI spec | [docs/development.md](docs/development.md) |
| Deploying to Kubernetes and DIAL Core, creating the AI Query Helper | [docs/deployment-guide.md](docs/deployment-guide.md) |
| AI Query Helper system prompt | [docs/query-helper-system-prompt.md](docs/query-helper-system-prompt.md) |
| Architecture and conventions | [CLAUDE.md](CLAUDE.md) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Copyright 2026 EPAM Systems, Inc.

Licensed under the [Apache License, Version 2.0](LICENSE).
