# Development

## Setup

```bash
uv sync       # install dependencies, including the dev group
make test     # uv run pytest -q
make lint     # ruff check + ruff format --check
make format   # apply ruff fixes and formatting
```

The test suite mocks ADAS with `respx` and needs nothing running. CI runs `make lint` and
`make test` against the committed `uv.lock`. After changing dependencies in `pyproject.toml`,
run `uv lock` and commit the lock with it.

Architecture, conventions and the steps for adding a tool are in [CLAUDE.md](../CLAUDE.md).

## Design

- One shared async `httpx` client. It sends a user JWT first and falls back to an admin key,
  forwards correlation headers verbatim, and logs outbound calls at INFO.
- HTTP statuses map to error codes, and every error is returned as a
  `{"error": {"code", "message", ...}}` envelope. Tools never raise across the MCP boundary.
- `adas_describe_schema` reads the bundled OpenAPI spec from disk, parses it once and memoizes
  it, and makes no network call.
- Complex request bodies are passed as a single `body` object rather than a flat parameter
  list. The tool descriptions point agents at `adas_describe_schema` for the exact shape.

## Refreshing the bundled OpenAPI spec

`src/adas_mcp/swagger/adas-openapi.json` is a committed snapshot of ADAS's runtime-generated
spec. Schema lookups therefore work when the backend is unreachable or runs with
`API_DOCS_ENABLED=false`, and cannot drift mid-session. The snapshot is **not** updated
automatically. Refresh it when ADAS's DTOs change.

ADAS commits the document its `/v3/api-docs` serves as `docs/openapi.json`, normalised (sorted
keys, no `servers`) and checked against the code by its build, so the snapshot for any ADAS
commit or release tag can be taken without running the service:

```bash
# ADAS_REPO is a checkout of the ADAS repository; pick the ref to match
git -C "$ADAS_REPO" show <ref>:docs/openapi.json \
  | python3 -m json.tool --sort-keys > src/adas_mcp/swagger/adas-openapi.json
```

Against a running ADAS (local development) the served document gives the same result:

```bash
curl -s localhost:5000/v3/api-docs \
  | python3 -m json.tool --sort-keys > src/adas_mcp/swagger/adas-openapi.json
```

The document marks operations that may still change with `x-preview` (the query surface) and
those not meant for API clients with `x-internal` (the pipeline data plane, the OTLP sinks).

Keep `--sort-keys`. springdoc emits its maps in Java hash order. That order is stable for a
given set of keys, so two refreshes against the same ADAS build are byte-identical. It
reshuffles wholesale, though, as soon as ADAS's schemas gain or lose a key. Without the flag, a
real change would rewrite about 1400 lines that mean nothing and bury the handful that do.
Review the diff structurally: compare the parsed JSON, not the lines.

The file lives inside the package, so both the wheel (`hatchling` ships the whole
`src/adas_mcp` tree) and the Docker image (`COPY src/`) pick it up with no extra build
configuration.
