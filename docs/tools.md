# Tool reference

adas-mcp registers up to 39 tools, all named `adas_*`. Which ones register depends on two
switches, described in [configuration](configuration.md#capabilities):

- **22 read-only tools** always register.
- **`adas_execute_sql`** registers only with `ADAS_ENABLE_EXECUTE_SQL=true`.
- **16 write-capable tools** register only with `ADAS_CAPABILITIES=full`.

Each tool's own description, which every MCP client receives, is the full contract. This page
gives an overview and covers the behavior that spans several tools.

## Tool surface

### Read-only, always registered (22)

| Group | Tool | ADAS endpoint |
|---|---|---|
| schema | `adas_describe_schema` | *(bundled OpenAPI spec, no HTTP)* |
| entities | `adas_list_entities` | `GET /v1/queries/entities` |
| | `adas_describe_entity` | `GET /v1/queries/entities/schema/{name}` |
| | `adas_list_query_functions` | `GET /v1/queries/functions` (the DSL catalog: structured-query and `measures[].fn` names) |
| | `adas_list_sql_functions` | `GET /v1/queries/sql-functions` (the SQL catalog: functions, operators and constructs SQL may use) |
| sql | `adas_validate_sql` | `POST /v1/queries/validate-sql` (verdict only; contacts no data store) |
| tables | `adas_list_tables` | `GET /v1/tables` |
| | `adas_get_table` | `GET /v1/tables/{name}` |
| backfill | `adas_get_backfill_status` | `GET /v1/data-sync/backfill/{runId}` |
| | `adas_list_backfill_runs` | `GET /v1/data-sync/backfill` (filter: `state`; the ≤5 runs this instance retains) |
| sync failures | `adas_list_data_sync_failures` | `GET /v1/data-sync/failures` (outstanding failed blobs and days; filters: `state`, `kind`, `reason`, `from`/`to`, `backfill_run_id`, `limit`) |
| pipelines | `adas_list_pipelines` | `GET /v1/pipelines` (filters: `target`, `kind`, `enabled`, `updated_since`; projection: `view`) |
| | `adas_get_pipeline` | `GET /v1/pipelines/{name}` (projection: `view`) |
| | `adas_preview_pipeline` | `POST /v1/pipelines/{name}/preview` (dry run; both kinds) |
| catalog | `adas_preview_catalog_export` | `POST /v1/catalog/export/preview` (what an export would carry, and why) |
| | `adas_export_catalog` | `POST /v1/catalog/export` (the bundle; writes nothing) |
| | `adas_preview_catalog_import` | `POST /v1/catalog/import/preview` (multipart; validation pass, writes nothing) |
| saved queries | `adas_list_saved_queries` | `GET /v1/saved-queries` (filter: `scope`) |
| | `adas_get_saved_query` | `GET /v1/saved-queries/{id}` |
| dashboards | `adas_list_dashboards` | `GET /v1/dashboards` (items without widgets, plus per-caller `permissions`) |
| | `adas_get_dashboard` | `GET /v1/dashboards/{id}` |
| settings | `adas_get_setting` | `GET /v1/settings/{name}` (`app-routes` \| `sessions-source`) |

### `ADAS_ENABLE_EXECUTE_SQL=true` only (1)

| Group | Tool | ADAS endpoint |
|---|---|---|
| sql | `adas_execute_sql` | `POST /v1/queries/execute-sql` |

### `ADAS_CAPABILITIES=full` only (16)

| Group | Tool | ADAS endpoint |
|---|---|---|
| tables | `adas_create_table` | `POST /v1/tables` (identity only, → `PENDING`) |
| | `adas_define_table_schema` | `POST /v1/tables/{name}/schema` (DDL, → `ACTIVE`) |
| | `adas_patch_table_schema` | `PATCH /v1/tables/{name}/schema` (evolve an `ACTIVE` table) |
| backfill | `adas_trigger_backfill` | `POST /v1/data-sync/backfill` |
| sync failures | `adas_retry_data_sync_failures` | `POST /v1/data-sync/failures/retry` (requeue; at least one selector) |
| pipelines | `adas_create_pipeline` | `POST /v1/pipelines` (`kind`: `aggregate` \| `enrich`) |
| | `adas_patch_pipeline` | `PATCH /v1/pipelines/{name}` |
| | `adas_refresh_pipeline` | `POST /v1/pipelines/{name}/refresh` (`aggregate` only) |
| catalog | `adas_import_catalog` | `POST /v1/catalog/import` (multipart; creates tables without grants, disabled pipelines) |
| saved queries | `adas_create_saved_query` | `POST /v1/saved-queries` |
| | `adas_replace_saved_query` | `PUT /v1/saved-queries/{id}` (full replace) |
| | `adas_delete_saved_query` | `DELETE /v1/saved-queries/{id}` |
| dashboards | `adas_create_dashboard` | `POST /v1/dashboards` |
| | `adas_replace_dashboard` | `PUT /v1/dashboards/{id}` (whole document, `generation`-checked) |
| | `adas_delete_dashboard` | `DELETE /v1/dashboards/{id}` |
| settings | `adas_replace_setting` | `PUT /v1/settings/{name}` (whole document) |

## Results and errors

Tools never raise across the MCP boundary. A call returns either the ADAS response body or an
error envelope, `{"error": {"code", "message", "status"?, "path"?, "details"?}}`. The `code` is
derived from the HTTP status (`400`/`422` → `invalid_argument`, `401` → `unauthenticated`,
`403` → `permission_denied`, `404` → `not_found`, `409` → `conflict`, `5xx` → `internal_error`),
and `status` keeps the original one.

A bare-array response is wrapped as `{"items": [...]}`, and an empty one is `{"ok": true}`.

## Request bodies and the bundled schema

Write tools take complex request bodies as a single `body` object rather than a flat parameter
list. ADAS reads bodies **strictly**: an unrecognized member is a 400 that names the field.

`adas_describe_schema` is the authoritative field list. It reads a bundled snapshot of ADAS's
OpenAPI document and makes no network call. With no argument it lists every component schema
name, and `type_name=...` returns one schema verbatim. The snapshot is refreshed by hand when
ADAS changes, as described in [development](development.md#refreshing-the-bundled-openapi-spec).

## Entities and tables

`adas_list_entities` / `adas_describe_entity` return the **flattened, query-ready** view of an
entity: source columns and enrichment columns merged. Sensitive fields the caller is not cleared
for, and fields a column policy hides from its role, are omitted. A `projection_only` field is
computed after the query runs and may only be selected, never filtered, sorted or aggregated.
`adas_list_tables` / `adas_get_table` return the **raw catalog** definition: physical columns,
tags, and ordering, partition and grain metadata.

Creating a table takes **two calls**. `adas_create_table` records the identity as `PENDING` and
issues no DDL. `adas_define_table_schema` then sets the complete physical schema, runs the
`CREATE TABLE`, and flips the table to `ACTIVE`.

Once a table is `ACTIVE`, its columns change through `adas_patch_table_schema`:
`add`/`drop`/`rename`/`ttl` as one multi-command `ALTER`, plus `update` for catalog-only
metadata. The two schema endpoints are mutually exclusive by status: `POST` accepts only
`PENDING`/`FAILED` tables, and `PATCH` accepts only `ACTIVE` ones.

## Pipelines

The pipeline registry holds **two kinds** on one body, discriminated by `kind`:

- `aggregate`: an in-database rollup that ADAS refreshes on a cron.
- `enrich`: a `transform` (an LLM call or a SQL derivation) writing a target enrichment, run by
  the Enrichment Runner. `adas_create_pipeline` with `body={"kind": "enrich", ...}` registers
  an enrichment.

A member that belongs to the other kind is rejected with 422 rather than ignored.
`adas_refresh_pipeline` works only on `aggregate` pipelines.

### Preview

`adas_preview_pipeline` serves both kinds in one flat shape. It returns:

- newest-first `rows`, capped at 2048 bytes a cell, with typed `columns`;
- the declaration as a portable `statement` (ADAS SQL, runnable with `adas_execute_sql`) and
  as a `query` (structured-query DSL), for aggregate and Class-1 pipelines;
- `total_rows` (aggregate), `group_key` (group) and coded `notes`.

It echoes no declaration, calls no model and renders no prompt. Its body takes `group_key` or
`from`/`to`, and `cursor` is refused.

### Drafts

Pipelines can be saved as drafts. `adas_create_pipeline` needs only `name`, `kind` and
`target`. A pipeline stored disabled is not validated, so even a complete but invalid
`aggregate` body is accepted. The kind's validation gate runs when a write leaves the pipeline
enabled, or when a disabled pipeline is previewed. The preview reports the same first problem
that enabling it would (422 `pipeline_validation_failed`).

`enabled` is optional for `enrich` pipelines. Absent means `false`, and `true` runs the gate.

`adas_patch_pipeline` keeps replace semantics, except that `target` is sticky:

- A patch that leaves the pipeline enabled must carry the whole declaration.
- A patch that leaves it disabled may carry any subset while the stored declaration is
  incomplete. It may not make a complete declaration incomplete (422).
- On an enabled pipeline, a declaration patch is a 409 for `aggregate` and permitted for
  `enrich`.

### Views

Pipeline reads take a `view`:

- `source`, the default, is the declaration as authored. It omits undeclared members.
- `compiled` fills in exactly five derived values: `response_schema`, the resolved `outputs`
  mapping, the resolved `inputs`, `grain_key` and `version_column`. It refuses an incomplete
  `enrich` declaration (422), and the compiled listing omits such a pipeline.

Both views use the same field names. None of the five derived members is accepted on a write
(400), so a compiled body cannot be posted back.

`kind=enrich&view=compiled` is the Enrichment Runner's sync feed, and the runner runs only
`enabled: true` entries. `enabled` is a pipeline's single run control. A flag-only patch does
not bump `generation`, but `updated_at` still moves. To stop a pipeline, patch it to
`{"enabled": false}`. Pipeline deletion is not exposed.

### The `transform` block

An `enrich` declaration carries its definition as the nested `transform` block, with no
separate registration step. The block holds `type` (`llm` \| `sql`), `model`, `preset`,
`route`, `params`, `request_template`, `inputs` and `outputs`.

- `transform.outputs` is an ordered object keyed by the target column receiving each output.
  For `llm`, an output's `prose` is optional and defaults to that column's `description`.
- `transform.inputs` is the variable map the transform receives. Don't confuse it with the
  top-level `inputs`, which is the array of source table names.
- `preset` is `chat_completion` (the default) or `dial_app_route`. Under `dial_app_route` the
  runner POSTs the rendered body to the DIAL application's `/route/{route}` and reads the whole
  response. `transform.route` is then required, each output may take its value from the
  response with a JSONata `source`, and outputs may bind `object`/`map` columns. Arming needs
  the `(model, route)` pair on the instance's `app-routes` setting (`adas_get_setting`).
- `request_template` placeholders are `{{name}}` (escaped into a JSON string) or
  `{{name:json}}` (inserted as a raw JSON literal).
- A `sql` transform must carry none of `preset`, `model`, `route`, `params`,
  `request_template` or `response_schema`. An `llm` transform requires a non-blank `model` **and** a non-blank
  `request_template`.
- `transform.output_vars` (an alternative output shape) and a stored
  `transform.response_schema` (a pinned schema) are both read on `view=source` and accepted on
  `PATCH`, so a read-modify-write round-trips. `output_vars` is refused on `POST`, and a
  declaration carrying both output shapes is a 422.
- The transform is authored, so it appears in both views, except `transform.response_schema`.
  That one is `source`-only, because `compiled` already carries the same fact as the derived
  `response_schema`.

An enrich `schedule` trigger may omit `cron`, which defaults to every minute. A `group`
pipeline may cap its assembled input with `advanced.input_token_budget`.

An `enrich` pipeline must be registered **after** its target enrichment is `ACTIVE`, because it
derives `grain_key` from it. Unless the declaration names `inputs`, it reads that enrichment's
own parent source.

## Saved queries and dashboards

Saved queries and dashboards store authored intent, never results. Nothing executes on the
server, so a client runs each body itself (`adas_execute_sql` for SQL).

A dashboard is widgets on a 12-column grid. Each widget holds either a `saved_query_id` or an
`inline_query` snapshot. The `saved_query_id` is a loose reference that ADAS never
dereferences, so resolve it with `adas_get_saved_query`. Every body is validated on write
against the caller's view of the catalog.

`adas_replace_dashboard` must send back the `generation` it read. A stale one is a 409
`dashboard_conflict`. Both replace tools drop the server-assigned members (`id`, owner,
timestamps, `permissions`, and a saved query's `source` / `generation`), so a document read
back can be edited and sent as is.

A chart is `{type, x_field, y_field, legend}`. `y_field` is an array of series names. A legacy
single string is accepted and read back as a one-element array. The optional `legend` is
`{visible, position}`, where `position` is `above`, `right`, `below` or `left`. ADAS stores it as
sent and does not check it.

Authorization uses ADAS's saved-query privileges (`SAVED_QUERY_READ` / `_WRITE` / `_PUBLISH`),
not `FULL_ADMIN`. Something the caller may not see is a 404.

## Catalog promotion

Catalog promotion is preview-first on both sides:

1. On the source, `adas_preview_catalog_export`, then `adas_export_catalog`.
2. On the target, `adas_preview_catalog_import`, then `adas_import_catalog`, passing the
   exported bundle **unchanged** as `bundle`. The tool uploads it as the multipart `file` part.

Import never changes an existing object's definition, columns or pipeline. `resolution_policy`
is `FAIL_IF_EXISTS` or `SKIP_IF_EXISTS`, and `OVERWRITE` is refused. Every imported pipeline
starts disabled, so enable each one with `adas_patch_pipeline`.

A component can carry less than its closure. `"dependencies": false` exports a pipeline without
its inputs, target or enrichments, which must then already exist on the target.
`"access_only": true` exports only a table's access entries, and `"roles": [...]` narrows them to
the listed roles. Both of those need `include_access`.

Access travels only on request. Without `include_access`, the bundle holds no access entries, row
policies or column policies, and imported tables start ungranted. Grant each source on the target
with `PUT /v1/tables/{name}/access?role={role}`, which is not wrapped. An enrichment follows its
source's read access.

With `include_access: true` on the export, the bundle carries each covered table's per-role
entries, system tables included. With `include_access=true` on the import, those entries are
written last, in one transaction, under either resolution policy. Both sides need `TABLE_GRANT`.
An import never removes an entry, and role names and row-policy values are copied as they are,
so review the import preview's `access` before importing. Anything an export cannot carry is
reported only in the export preview's `access[].left_behind`.

Instance settings do not travel: allow an imported `dial_app_route` pipeline's pair with
`adas_replace_setting` before arming it. System tables travel by name only, as
`required_system_tables`. Bundles are `format_version` 4, which an older ADAS refuses.

## Data-sync failures

Every log blob or day that failed to ingest is kept in a durable failure ledger, whichever path
hit it (poller, reconciliation, backfill, retry sweep). Transient failures are retried by a
sweep with backoff, and deterministic ones are parked. `adas_list_data_sync_failures` lists what
is outstanding, and its `total` is what `adas_retry_data_sync_failures` would requeue with the
same filters. A backfill status reports `failedBlobs`. Filter the ledger by `backfill_run_id` to
see which blobs failed.

## Not wrapped

Some ADAS endpoints are deliberately left out:

- **The pipeline data plane** (`POST /v1/pipelines/{name}/scan`, `/group-rows`, `/materialize`,
  `/rows`). It is the Enrichment Runner's machine-to-machine API, and the only part of
  `/v1/pipelines` reachable without `FULL_ADMIN`.
- **`POST /v1/pipelines/{name}/trial-input`.** The read behind the Enrichment Runner's dry run.
  It requires the runner's `Adas-Contract-Version` header, so it belongs to the runner, not to
  an MCP client. `adas_preview_pipeline` covers the same sample.
- **`DELETE /v1/pipelines/{name}`.** To stop a pipeline, patch it to `{"enabled": false}`.

Others are not in scope yet and still need plain HTTP:

- the remaining table writes: `PUT`/`DELETE /v1/tables/{name}`, the role-keyed access API
  (`GET`/`PUT`/`DELETE /v1/tables/{name}/access`) and `POST /v1/tables/{name}/rows`;
- `POST /v1/queries/execute` (structured-query DSL) and the translation endpoints;
- the OTLP sinks (`/v1/otel/**`);
- the audit surfaces: `/v1/activities` and the `.../revision/{revision}` reads.
