# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server (FastMCP, Python) that wraps the Analytics Data Access Service (ADAS, a Java/Spring Boot app 
on port `5000`, base path `/v1`) REST API as MCP tools. Every tool is named `adas_*`.

**39 tools across 10 groups**: schema introspection (bundled spec), query discovery + ad-hoc SQL,
the table catalog (read + create/materialize/evolve), the data-sync backfill control plane, the
data-sync failure ledger (list + requeue), the pipeline registry + control plane, catalog
promotion (export/import between environments), saved queries, dashboards, and instance settings
(`app-routes`, `sessions-source`). 22 read-only tools always register; the 16 write-capable ones need
`ADAS_CAPABILITIES=full`, and `adas_execute_sql` has its own flag,
`ADAS_ENABLE_EXECUTE_SQL` (off by default, independent of capabilities).

The pipeline registry holds **both kinds** on one flat, `kind`-discriminated body: `aggregate` (an
in-DB rollup ADAS refreshes on a cron) and `enrich` (a **`transform`** — an LLM call or a SQL
derivation — writing a target enrichment, driven by the Enrichment Runner). Both kinds live in
`tools/pipelines.py`, and `adas_create_pipeline` with `"kind": "enrich"` is what registers an
enrichment.

An `enrich` declaration *carries* its definition as the nested, enrich-only `transform` block
(`type`, `model`, `preset`, `route`, `params`, `request_template`, `inputs`, `outputs`, plus the
optional `output_vars` / stored `response_schema`). `preset` is `chat_completion` (default) or
`dial_app_route`, which calls a DIAL application route (`transform.route`, per-output `source`)
and is armed only for a `(model, route)` pair on the `app-routes` instance setting. A member belonging to no kind is refused at the
**binding with 400**, unlike the 422 a cross-kind member gets. Note the nesting: a pipeline's
top-level `inputs` is the array of source table names, `transform.inputs` is the variable map, so
a misplaced member fails to bind rather than being silently accepted. Rows carry
`pipeline_generation`, the provenance column that identifies the declaration that wrote them. A
member belonging to the other kind is refused with 422
rather than ignored, which is the only thing that makes the flat union safe — the docstrings carry
both field lists for that reason. `adas_refresh_pipeline` is `aggregate`-only (422 on an `enrich`
pipeline); `adas_preview_pipeline` serves **both** kinds in **one flat shape** — `{statement?, query?,
columns, rows, total_rows?, group_key?, notes}` (`statement` is the declaration as portable ADAS
SQL and `query` the same as structured-query DSL, both absent under `statement_not_portable`), newest-first rows capped at 2048 bytes a cell, coded notes,
and no declaration echo (that is `adas_get_pipeline`'s job) — taking an optional `body` (`group_key`,
optional on a `group` pipeline, or `from`/`to`) that positions an `enrich` sample; `cursor` is not a
member and is refused with 400. Pipeline **reads** take a `view`: `source` (the default, what was authored) or
`compiled` (the derived values filled in — exactly five: `response_schema`, `outputs`, resolved
`inputs`, `grain_key`, `version_column`), the same field names in both. The `transform` is
authored, so it appears in *both* projections, except
`transform.response_schema`, which is `source`-only so the schema does not cross the feed twice.
None of the five derived members is accepted on a write (400), so a compiled body cannot be posted
back. A declaration patch on an *enabled*
pipeline is a
409 for `aggregate` and permitted for `enrich`. **Drafts** are storable: registration needs only `name` + `kind` + `target`, a pipeline
left disabled is stored ungated (`enabled` is optional for `enrich`, absent = `false`), and the
kind's gate runs only when a write leaves the pipeline enabled or on preview of a disabled one.
PATCH keeps replace semantics with a sticky `target`; a disabled patch may not make a complete
declaration incomplete; `/rows` and `/materialize` refuse a disabled pipeline with 409. The
client enforces none of this — `adas_create_pipeline` / `adas_patch_pipeline` check only that
`body` is a non-empty object, so the rules live in the docstrings, not in validation. A pipeline's read-only `state` splits the same
way: the run-position half (`cursor_*`, `lag_seconds`, `last_run_at`, `next_run_at`,
`last_error`, `has_more`) is ADAS-owned and therefore **absent** on an `enrich` pipeline, whose
position the Enrichment Runner holds, while the freshness half (`clamp`, `unclamped_reads`,
`rebuild_required`, `drained_at`) reports for both kinds.

`enabled` is a pipeline's **single run control** for both kinds: a flag-only patch keeps progress and
does not bump `generation` (the declaration's revision); `updated_at` moves on every change. The
Enrichment Runner reads the unfiltered `kind=enrich&view=compiled` feed and runs only `enabled: true`
entries.

Saved queries (`tools/saved_queries.py`) and dashboards (`tools/dashboards.py`) store **authored
intent only** — ADAS has no execute endpoint for either; a client runs a body through the query
surface. A dashboard widget holds exactly one of `saved_query_id` (loose, never dereferenced) or
`inline_query` (a snapshot `{name, sql | query, result_view?, chart?}`), and every inline body is
revalidated against the caller's catalog view on each write. `adas_replace_dashboard` requires the
`generation` it read (checked client-side; stale → 409 `dashboard_conflict`). Both modules strip the
server-assigned members ADAS rejects with 422 before sending, so a read-back document round-trips
— one of the two places the client edits a body (the other is `adas_replace_setting`, which
drops the server-stamped `updated_at`/`updated_by`); a dashboard replace keeps `generation`, every
other write drops it. Authorization is the saved-query privilege set (`SAVED_QUERY_READ`/`_WRITE`/
`_PUBLISH`, by scope or visibility), not `FULL_ADMIN`; an invisible row is 404, never 403.

Table creation is deliberately two tools mirroring ADAS's own two-phase split:
`adas_create_table` (`POST /v1/tables`) records identity as `PENDING` with no DDL and rejects
columns/physical keys/`ttl` with 422; `adas_define_table_schema` (`POST /v1/tables/{name}/schema`)
sets the complete schema, issues `CREATE TABLE`, and flips it to `ACTIVE`. `write` is identity
(phase 1, immutable — it picks the engine); everything else physical is phase 2. A third tool
covers life *after* materialization: `adas_patch_table_schema` (`PATCH /v1/tables/{name}/schema`)
evolves an `ACTIVE` table — `add`/`drop`/`rename` columns and a `ttl` merge-patch as one
multi-command `ALTER`, plus `update`, a catalog-only metadata merge-patch that issues no DDL. The
two schema endpoints are mutually exclusive by status: `POST` is `PENDING`/`FAILED` only, `PATCH`
is `ACTIVE` only, each answering 409 on the other's state.

Deliberately **not** wrapped, and not by omission:
- The pipeline data plane (`POST /v1/pipelines/{name}/scan`, `/group-rows`, `/materialize`,
  `/rows`) — the Enrichment Runner's machine-to-machine API (cursor paging, batched row
  write-back). These four are also the only `/v1/pipelines` paths `OidcSecurityConfiguration` pins
  as `.authenticated()` rather than `FULL_ADMIN`, authorized per call by the target enrichment's
  WRITE grant.
- `POST /v1/queries/execute` (structured-query DSL), `QueryTranslationController`
  (`/v1/queries/translate`, `/translate-sql`), the
  OTLP sinks (`/v1/otel/**`), the audit activity feed (`POST /v1/activities`,
  `GET /v1/activities/{id}`) and the `GET .../revision/{revision}` snapshot reads on tables,
  pipelines and saved queries, `DELETE /v1/pipelines/{name}` (pipeline deletion is not exposed;
  `enabled: false` is the stop control), and the remaining table-management writes
  (`PUT`/`DELETE /v1/tables/{name}`, `POST /v1/tables/{name}/rows`, and `TableAccessController`'s
  role-keyed `GET`/`PUT`/`DELETE /v1/tables/{name}/access`) — not yet in scope.

## Commands

```bash
uv sync                                       # install deps (incl. dev group)
uv run pytest -q                              # run the full test suite
uv run pytest tests/test_client.py -q         # single file
uv run pytest -k describe_entity              # by keyword
make lint                                     # ruff check + ruff format --check (what CI runs)
make format                                   # apply ruff fixes and formatting
make test                                     # uv run pytest -q (what CI runs)

# Run the server (ADAS_BASE_URL required; credentials optional under SECURITY_MODE=none):
ADAS_BASE_URL=http://localhost:5000 uv run adas-mcp --transport stdio   # Claude Desktop / Code
ADAS_BASE_URL=http://localhost:5000 uv run adas-mcp --transport http    # streamable-http sidecar
ADAS_BASE_URL=http://localhost:5000 ADAS_CAPABILITIES=full uv run adas-mcp --transport stdio
ADAS_BASE_URL=http://localhost:5000 ADAS_ENABLE_EXECUTE_SQL=true uv run adas-mcp --transport stdio

docker build -t adas-mcp .

# Refresh the bundled OpenAPI snapshot from ADAS's committed docs/openapi.json (no running service):
# --sort-keys is load-bearing; see docs/development.md before dropping it
git -C "$ADAS_REPO" show <ref>:docs/openapi.json | python3 -m json.tool --sort-keys > src/adas_mcp/swagger/adas-openapi.json
# or from a locally running ADAS:
curl -s localhost:5000/v3/api-docs | python3 -m json.tool --sort-keys > src/adas_mcp/swagger/adas-openapi.json
```

`uv.lock` is committed and CI installs from it with `uv sync --frozen`, as does the `Dockerfile`:
after changing dependencies in `pyproject.toml`, run `uv lock` and commit the lock with it.
CI is GitHub Actions (`.github/workflows/`), delegating to the shared `epam/ai-dial-ci` Python/uv
workflows: `make lint` and `make test` (after `uv sync --frozen --all-extras`), an ORT license
scan of `uv export --frozen --all-extras` (dev group included), and a `linux/amd64,linux/arm64`
image build that fails on any Trivy finding with a fix available, `LOW` included (`trivy.yaml`
sets the DB mirrors). Releases push the image to GHCR and Docker Hub as
`epam/ai-dial-analytics-mcp`, and set the version in `pyproject.toml` from the release line, so the
committed `0.0.0` is a placeholder. `hygiene-check.yml` runs the hygiene sweep on a PR's added
lines and reports hits as warnings. PRs and releases target `development` (the default branch) and
`release-*`; the PR title must be a Conventional Commit.

`pyproject.toml` sets `asyncio_mode = "auto"` — write `async def test_*` directly, no `@pytest.mark.asyncio` needed.

## Architecture

### Entry path
`__main__.main` parses `--transport` → `server.run` → `FastMCP("adas-mcp")` with
`register_all(mcp, capabilities_from_env(), execute_sql_enabled_from_env())`.
`tools/__init__.register_all` is the single registration point. Transport `http` is aliased to `streamable-http`.

### Capability and feature toggles
Two independent switches, both read and validated at import — kept independent of the rest of
`Settings` so the tool surface builds at import while `ADAS_BASE_URL` stays lazy.

`ADAS_CAPABILITIES` (`limited` default | `full`), via `config.capabilities_from_env()`, gates the
write-capable tools. Each tool module exposes up to two registrars: `register(mcp)` for its
read-only tools (always called) and `register_full(mcp)` for its write-capable ones (called only
under the `if capabilities == "full":` branch). Grouping by controller keeps a group's tools in
one file while the capability split stays in `register_all`.

`ADAS_ENABLE_EXECUTE_SQL` (boolean, **`false` by default**), via
`config.execute_sql_enabled_from_env()`, gates `adas_execute_sql` alone — registered by
`sql.register_execute_sql(mcp)` under its own `if execute_sql_enabled:` branch, deliberately not
`register_full`. It is read-only in SQL semantics but the only tool that actually executes
against ClickHouse, so that is its own decision, not a consequence of allowing writes: `full`
alone does not register it, and it can be enabled under `limited`. Accepted spellings are
`1|true|yes|on` / `0|false|no|off|""` (case-insensitive, trimmed); anything else is a
`ConfigError`. Its sibling `adas_validate_sql` always registers for the mirror-image reason — it
runs the same checks with the round trip removed and contacts nothing — so a deployment with
execution disabled can still get SQL right on the first try.

Both toggles are a **local guardrail, not authorization** — ADAS enforces `FULL_ADMIN` /
`READ_ONLY_ADMIN` itself under `SECURITY_MODE=oidc`, and note that every `/v1/pipelines` endpoint
is `FULL_ADMIN` there, including the GETs.

### OpenTelemetry (standard zero-code setup)
There is no project-specific switch and no SDK setup code. `server.py` always builds
`telemetry.InstrumentedFastMCP`, which uses only `opentelemetry-api` (a core
dependency, a no-op without an SDK). The SDK, exporters (OTLP grpc + http,
Prometheus) and the httpx/logging instrumentations are the optional `otel` extra,
and `opentelemetry-instrument` configures them from the standard `OTEL_*` variables.
The Docker entrypoint is `opentelemetry-instrument adas-mcp`, with all exporters
defaulting to `none`. `InstrumentedFastMCP` overrides `call_tool`, the bound
`tools/call` handler, so argument-validation and unknown-tool failures get a span
too. Its `add_tool` wraps each tool function, so the error envelope (invisible once
serialized) becomes `error.type` on the span via a `ContextVar`. Outbound
`traceparent` to ADAS comes from the auto-instrumented httpx, not from this code.
`__main__._configure_logging` adds its stderr handler beside OpenTelemetry's
handlers rather than through `basicConfig`, which they would turn into a no-op.
Never record tool arguments or results as attributes, and never error *messages*
as status descriptions or exception events: pydantic `ValidationError`s and ADAS
errors quote the input, so the description is the code or exception type. Unknown
tool names collapse to `_OTHER` in the metric (client-chosen, unbounded) but not
on the span. `tests/test_telemetry.py` injects in-memory providers and drives the
real handler through an in-memory client session.

### One singleton `AdasClient`
`client.get_client()` lazily builds `AdasClient(Settings.from_env())` on first call and
registers an `atexit` close. `client.reset_client_for_tests()` exists for `conftest.py`.
Settings (`ADAS_BASE_URL`, `ADAS_API_KEY`, `ADAS_HTTP_TIMEOUT`, `ADAS_CAPABILITIES`,
`ADAS_ENABLE_EXECUTE_SQL`) are read from env at first use — running without `ADAS_BASE_URL` raises `ConfigError` only when a tool
actually fires, not at import.

### Auth precedence (per request)
`client._build_headers` resolves credentials in this order and attaches one auth header **when
available**:
1. Bearer JWT from the MCP request context (`ctx.request_context.meta["authorization"]`, then
   `...request.headers["authorization"]`). See `auth.extract_user_jwt`.
2. `ADAS_USER_JWT` env (stdio dev fallback).
3. Forwarded inbound `Api-Key` header → sent as `Api-Key: <key>`.
4. `ADAS_API_KEY` env → sent as `Api-Key: <key>` (admin fallback).
5. If none is available *and* the caller passed `require_auth=True`, raises `MissingAuthError`.

**Every tool calls `request(..., require_auth=False)`, writes included** — ADAS's default
`SECURITY_MODE=none` needs no credentials, so the client must not hard-fail locally when none are
configured. Credentials are still forwarded when present (steps 1–4), so under `oidc` mode ADAS
enforces auth and returns 401 → mapped to an `unauthenticated` error envelope.

Write tools are nonetheless wrapped in `try/except MissingAuthError` returning
`structured_error("unauthenticated", ...)`, so requiring credentials up front later is a one-word
change (`require_auth=True`) per tool rather than a restructuring. Those `except` branches are
marked `# pragma: no cover` because they are unreachable while `require_auth=False`.

### Error envelope contract — tools never raise across the MCP boundary
Every endpoint-backed tool returns either the backend body (success) or
`{"error": {"code", "message", "status"?, "path"?, "details"?}}`. `client._parse_response`
maps non-2xx via `errors.status_to_code` (400→invalid_argument, 401→unauthenticated,
403→permission_denied, 404→not_found, 409→conflict, 412→etag_mismatch,
422→invalid_argument, 5xx→internal_error). **422 matters**: ADAS answers declaration and
governance failures with it (`pipeline_validation_failed`, `validation_error` — what a
rejected table declaration reports as — and `write_column_not_allowed`), not 400 — so it
maps to the same code as 400 rather than to a generic `http_error`. The original status is
preserved in the envelope's `status`. ADAS also reads request bodies **strictly**: an
unrecognized member is a 400 `bad_request` naming the field, which is why every `body`-taking
docstring points at `adas_describe_schema` rather than listing fields from memory.
Transport `httpx.HTTPError` becomes `internal_error`. Tool wrappers catch only `MissingAuthError`.

### Response normalization
`_parse_response` reshapes successful bodies: 204/empty → `{"ok": True}`, top-level list →
`{"items": [...]}`, top-level dict → passthrough, scalar → `{"value": ...}`. So
`adas_list_entities` returns `{items: [...]}` even though the backend returns a bare array.

Which endpoints return a bare array matters for the tool docstrings: `/v1/queries/entities`,
and `/v1/queries/functions` do (→ `{items: [...]}`), while
`/v1/pipelines`, `/v1/tables`, `/v1/saved-queries`, `/v1/dashboards` and
`/v1/queries/sql-functions` return their own envelope (`{pipelines: [...]}` / `{tables: [...]}` /
`{saved_queries: [...]}` / `{dashboards: [...], permissions}` / `{functions: [...]}`) and pass through unchanged.

### Bundled OpenAPI schema
`tools/schema.adas_describe_schema` reads a **committed snapshot** at
`src/adas_mcp/swagger/adas-openapi.json` — no HTTP, and no `ctx` parameter. `_candidate_paths()`
looks in the installed package (`importlib.resources`) then the repo checkout;
`@lru_cache(maxsize=1) _load_spec()` parses it once. `reset_cache_for_tests()` calls
`_load_spec.cache_clear()` and is invoked by the autouse conftest fixture.

The file lives inside the package on purpose: `hatchling`'s `packages = ["src/adas_mcp"]` ships
the whole tree and the `Dockerfile`'s `COPY src/` picks it up, so neither needs extra build
configuration. Refreshing it is manual — see *Commands*.

### Adding a new tool
1. Create `src/adas_mcp/tools/<group>.py`, one module per ADAS controller. Each tool is an
   `async def adas_*(ctx: Context, ...)` that validates its args with `invalid_argument(...)`
   *before* any HTTP, then calls `await get_client().request(..., require_auth=False)`; write
   tools wrap that in `try/except MissingAuthError`. End the module with `register(mcp)` and/or
   `register_full(mcp)` calling `mcp.tool(name="adas_*")(fn)` per tool.
2. Import the module in `tools/__init__.register_all` and call the right registrar — writes go
   under the `full` branch; anything that executes against ClickHouse gets its own feature flag
   (`adas_execute_sql` is the precedent) rather than riding on capabilities.
3. Add the name to `EXPECTED_LIMITED` or `EXPECTED_FULL_ONLY` in `tests/test_tools_smoke.py`,
   plus a `respx` stub and a call in the smoke block — the smoke test asserts its call list
   covers the whole registered surface, so it fails if you skip that.
4. A multipart endpoint passes httpx's `files` shape to `request(..., files=...)`
   (`{part: (filename, content, content_type)}`), with scalar options as query `params`
   rather than extra parts. `tools/catalog.py` is the precedent.
5. Complex/nested request bodies take a single `body: dict[str, Any]` guarded by
   `if not isinstance(body, dict) or not body`, with the docstring pointing at
   `adas_describe_schema(type_name="...")` — not a flat parameter list.
6. Path/query parameter names that collide with Python keywords or builtins get renamed
   (`date_from`/`date_to` → wire `from`/`to`). Say so in the docstring.
7. Interpolate every path parameter through `client.path_segment(...)`, never raw. The values
   come from an LLM, and httpx resolves the merged URL before sending: an unescaped `/`, `..`,
   `?` or `#` silently retargets the request at a different endpoint
   (`id="../tables/dial_usage_log"` would turn `DELETE /v1/saved-queries/{id}` into
   `DELETE /v1/tables/dial_usage_log`).

### Tests
- `tests/conftest.py` is autouse: sets `ADAS_BASE_URL=http://adas.test` + `ADAS_API_KEY=test-admin-key`,
  clears `ADAS_USER_JWT`/`ADAS_CAPABILITIES`/`ADAS_ENABLE_EXECUTE_SQL`, and resets the client + schema cache before and after each test.
- HTTP is mocked with `respx` against `http://adas.test`.
- `tests/test_tools_smoke.py` is the source of truth for the tool surface:
  `test_registered_surface_is_exact` pins all four toggle combinations exactly,
  `test_limited_is_a_strict_subset_of_full` and `test_writes_are_not_registered_under_limited`
  guard the gating.
- Per-group modules follow a fixed template: passthrough happy path, list wrapping (or explicit
  envelope preservation), `invalid_argument` on an empty required arg, 404 → `not_found`, plus
  wire-level assertions where a name is translated.

## Conventions worth keeping

- Tool docstrings are the MCP tool description shown to LLM clients — keep them concrete, lead
  with what the tool does, and include `Examples:` blocks. They are the agent-facing API docs.
- **No docstring ever spells a tool call.** `Examples:` lines carry arguments only
  (`sql="SELECT 1"`, `name="widget_metrics", body={...}`), never `adas_x(sql="SELECT 1")`, and
  in-prose pointers name the tool outside the parens (``call `adas_describe_schema` with
  `type_name="TableDto"``). Some MCP clients register these tools under a prefix
  (DIAL Quick Apps uses `ADAS_MCP_adas_*`); call syntax in a description contradicts the schema name
  the model was given, and it emits the bare name — which a client that does exact-name lookup
  rejects as an unknown tool. Bare names in prose and `Related:` lines are fine and worth
  keeping: they are how a client discovers the workflow.
- Don't catch `httpx` errors inside tools; let them bubble to `client._send` so the error
  envelope shape stays uniform.
- New write-capable tools go behind the `full` capability branch in `register_all`.
- Lift docstring content from ADAS's own `@Operation` descriptions and OpenSpec specs
  (`openspec/specs/**` in the ADAS repository) rather than inventing it — the backend's
  wording carries the constraints that actually bite (limits rejected vs clamped, which fields
  must be sent together, what "lag_seconds" really measures).
- `_clean_params` drops `None`, so optional query args pass straight through as
  `params={"a": a, "b": b}` with no filtering.

## Public-repo hygiene

This repository is open source. Everything tracked — code, comments, docstrings, tests, docs and
the bundled OpenAPI snapshot — must read as the project's own neutral engineering record, not as
one company's operations log. Tool docstrings deserve the most care: they are shipped to every
LLM client that connects.

- **No environment attribution.** Don't write "on dev", "UAT", "prod", "in production", "the
  reference deployment", or cluster, namespace, host, registry, bucket or tenant names. Say "a
  running deployment" or describe the condition itself; use placeholders (`<adas-host>`,
  `adas.example.com`) in examples. "Local development" is fine.
- **No operational history.** No incident dates, deploy narratives, MR or ticket numbers, branch
  chatter, people, teams, or "the user decided". State a design decision with its reasons.
- **No real-data figures.** Never copy row counts, percentages, sizes or timings measured on a real
  environment. Test fixtures use invented values and patterned ids
  (`00000000-0000-4000-8000-000000000001`), never ids copied from logs.
- **Entity names: real is fine, framing is not.** Table and pipeline names such as
  `dial_usage_log` may appear as examples and fixtures; describe them by what they do, never as
  live, deployed or ours.
- **Only reference what ships.** Don't point at untracked files (`*.local.md`), private
  repositories, internal registries or internal design ids that resolve nowhere.
- **No machine-specific content.** Keep home paths, local socket overrides and personal tooling
  out of tracked files.
- **No temporary scaffolding.** No "TODO: remove before release" code or demo tools. Remove it,
  or make it a documented, opt-in feature.
- **Verify with PCRE.** For a branch, run `perl scripts/hygiene-sweep.pl <base>`,
  which holds the pattern list (names and real-data figures still need a read). `git grep -E` on
  macOS ignores `\b`, so a sweep built on it silently misses hits. Use `git grep -nP`, for example:
  `git grep -nIP '\b(dev|UAT|prod)\b|reference[- ]deployment|in production|gitlab|artifactory|MR ?!\d+|\.local\.md'`.
  A line-based grep misses phrases wrapped across lines, so add a multiline pass, for example:
  `git ls-files -z | xargs -0 perl -0ne 'print "$ARGV\n" if /reference[-\s]+deployment|in\s+production/'`.
