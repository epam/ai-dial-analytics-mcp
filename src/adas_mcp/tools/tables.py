from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client, path_segment
from ..errors import MissingAuthError, invalid_argument, structured_error


async def adas_list_tables(ctx: Context) -> dict[str, Any]:
    """List all table definitions in the ADAS catalog.

    This is the raw catalog view (as opposed to the flattened query entities
    from `adas_list_entities`). Column details are omitted here — use
    `adas_get_table` for a single table's columns.

    Returns: `{tables: [TableDto]}` — the backend `TableListDto` envelope,
      **not** `items`; table definitions ordered by name, each with `name`,
      `description`, `type` (source/enrichment), `status`, `system`, etc.
      `layout` is never listed here — read it per table with `adas_get_table`.

    Related: adas_get_table, adas_list_entities.
    """

    return await get_client().request("GET", "/v1/tables", ctx=ctx, require_auth=False)


async def adas_get_table(ctx: Context, name: str) -> dict[str, Any]:
    """Get one table's full catalog definition, including its columns.

    Returns the raw `TableDto` for the named table — the definitional view of a
    single catalog table (physical/exposed column names, types, nullability,
    tags, sensitivity, ordering/partition/grain metadata). Contrast with
    `adas_describe_entity`, which returns the flattened, query-ready schema.

    Returns: `TableDto` `{name, description, type, write, source_table, status,
      system, identity_column, version_column, ordering_key, partition_by, ttl,
      grain: {grain_key, cardinality}, tag_order, permissions, columns:
      [{source_name, name, type, element_type, nullable, description,
      display_name, tag, sensitive, heavy}], layout}`. `identity_column` /
      `version_column` are the source's scan metadata — the pair the governed
      incremental scan pages on, and the pair an `enrich` pipeline's
      `version_column` is derived from. `partition_by` on an enrichment means
      ADAS laid it out aligned with its source (a caller cannot set it).
      `layout` (read-only, enrichments only, absent when there is nothing to
      report): `aligned` — partitioned like its source and joined on (grain
      key, partition column); `migrating` — ADAS is relaying the existing table
      out into that layout by itself; `unsupported` — the relayout skipped it
      (an `llm` writer, a same-named column it cannot adopt, or a database
      engine that cannot exchange tables). `columns` is filtered for the
      caller: a `sensitive` column without the clearance, and a column a
      **column policy** hides from the caller's role, are omitted (and not
      counted) as if absent; the structural fields (`ordering_key`,
      `partition_by`, `grain_key`, ...) are returned unfiltered. `permissions`
      says what the caller may do and how it is narrowed: `rows_restricted`
      (row policies confine its reads), `columns_restricted` (column policies
      hide some columns), `grant` (holds `TABLE_GRANT`, i.e. may use the
      role-keyed access API `GET`/`PUT`/`DELETE /v1/tables/{name}/access`,
      which this server does not wrap). `{error: {code: "not_found"}}` for an
      unknown table.

    Related: adas_list_tables, adas_describe_entity.

    Examples:
      name="dial_usage_log"
    """

    if not name:
        return invalid_argument("name is required")
    return await get_client().request(
        "GET", f"/v1/tables/{path_segment(name)}", ctx=ctx, require_auth=False
    )


async def adas_create_table(ctx: Context, body: dict[str, Any]) -> dict[str, Any]:
    """Create a table definition — **identity only**, no ClickHouse DDL.

    Step 1 of 2. This records the table as `PENDING`; nothing exists in
    ClickHouse until you call `adas_define_table_schema`, which issues the
    `CREATE TABLE` and flips it to `ACTIVE`.

    `body` is a `CreateTableRequest` — call `adas_describe_schema` with
    `type_name="CreateTableRequest"` for the exact shape.
    Load-bearing fields:
      - `name` (also the ClickHouse table name) and `type` (`source` |
        `enrichment`) are required.
      - `source_table` — the source this enrichment enriches. Enrichment only,
        and effectively required for one.
      - `write` — source only, defaults to `append`. `append` keeps every row
        (MergeTree); `upsert_by_key` collapses to the latest row per
        `ordering_key` (ReplacingMergeTree), making it the table's unique key.
        Immutable: it picks the engine, which is frozen at materialization. An
        enrichment always collapses by its grain key, so the field is rejected
        for one.
      - `description`, `tag_order` (display hint, max 100 tag names).
      - `system` — accepted and **ignored**: a created table is never `system`
        (only a seeded, migration-owned one is). It is in the vocabulary only so
        a body round-tripped from `adas_get_table` is not refused for a field
        the caller never chose to send.
      - **Columns, physical keys (`ordering_key`, `partition_by`, `grain_key`,
        `cardinality`) and `ttl` are rejected here with 422** — they all belong
        to `adas_define_table_schema`, even though the DTO lists them. Any
        member the DTO does *not* declare is a 400 naming it: ADAS reads request
        bodies strictly.

    Returns: the created `TableDto` with `status: "pending"` (see
      `adas_get_table`). `{error: {code: "conflict"}}` if the name is taken;
      `invalid_argument` (422) if the body carries schema-time fields.

    Related: adas_define_table_schema, adas_describe_schema, adas_get_table.

    Examples:
      body={"name": "widget_sentiment", "type": "enrichment", "source_table": "dial_usage_log", "description": "LLM sentiment per request"}
      body={"name": "widget_metrics", "type": "source", "write": "upsert_by_key"}
    """

    if not isinstance(body, dict) or not body:
        return invalid_argument("body must be a non-empty object")
    try:
        return await get_client().request(
            "POST", "/v1/tables", json=body, ctx=ctx, require_auth=False
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_define_table_schema(
    ctx: Context, name: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Define a PENDING table's physical schema and materialize it in ClickHouse.

    Step 2 of 2, after `adas_create_table`. Sets the **complete** schema —
    columns plus the physical keys — issues the `CREATE TABLE`, and flips the
    table to `ACTIVE`. There is no partial define: send the whole schema.

    `body` is a `DefineSchemaRequest` — call `adas_describe_schema` with
    `type_name="DefineSchemaRequest"` for the exact shape.
    Load-bearing fields:
      - `columns` — each `{name, source_name, type}` required (`name` is the
        exposed flat field, `source_name` the physical ClickHouse column), plus
        optional `element_type` (required iff `type` is `array`), `nullable`
        (defaults false for sources, true for enrichments), `description`,
        `display_name`, `tag`, `sensitive`, `heavy`.
      - **source only**: `ordering_key` (MergeTree ORDER BY, physical
        `source_name`s) and optional `partition_by` `{column, granularity:
        day|month|year}`.
      - **enrichment only**: `grain_key` (any declared column of the source,
        not necessarily one of its `ordering_key` columns) and `cardinality`
        (v1: `zero_or_one`). **Do not also declare the grain key in `columns`** —
        ADAS prepends it itself as a hidden physical column typed from the
        source, so declaring it emits it twice in the `CREATE TABLE`, which
        ClickHouse rejects and leaves the definition `FAILED`. `partition_by`
        is refused (422) for an enrichment — see the aligned layout below.

    **Aligned layout (enrichments, decided by ADAS).** When the `grain_key` is
    the source's `identity_column`, the source declares a partition column
    other than that, and the source is partition-local (an `append` source, or
    an `upsert_by_key` one whose partition column is in its `ordering_key`),
    the enrichment is created `PARTITION BY` the source's partition expression
    with a **hidden** column of the source partition column's name and type
    (`request_time` for `dial_usage_log`) — not listed in `columns`, not
    queryable. No request member asks for or suppresses it; the response shows
    `partition_by` and `layout: "aligned"`. A declared column of that name is
    **adopted** (kept visible, values written by ADAS from the source row)
    when it has the source column's type, is non-nullable and neither
    `sensitive` nor `heavy`; otherwise it is a 422 `validation_error` naming
    the collision. With no `ttl` declared, an aligned enrichment inherits its
    source's TTL in ClickHouse (not mirrored into the catalog, so `ttl` stays
    absent); a declared `ttl` overrides it. Rows written to it through the
    rows API must carry the source row's value of that column.
      - `identity_column` + `version_column` — source only, and required
        *together* for the table to be scannable by the governed incremental
        scan; omitting both just makes the source non-scannable. Rejected for an
        enrichment. The scan's exactly-once guarantee rests on `identity_column`
        being genuinely unique per row and `version_column` being monotonic and
        never backdated — nothing here can verify either.
      - `ttl` `{column, interval, unit}` — optional row expiry; all three fields
        required together, `interval` must be positive.
      - `write` is rejected here (422) — it is identity, declared at
        `adas_create_table`.

    For an enrichment, the **five** provenance columns are *injected* when you
    do not declare them — `pipeline_generation` (long), `model` (string),
    `enriched_at` (timestamp), `group_version` (long), `truncated` (boolean) —
    appended in that order with the `system` tag. `pipeline_generation` is what
    identifies the declaration that wrote a row.
    Injection only ever adds: a provenance column you declare yourself is left
    exactly as you declared it, never duplicated, overwritten or reordered. A
    column that is absent is silently skipped on write rather than failing it.

    If you do declare them, the types are load-bearing:
      - `pipeline_generation` — `long`. ADAS stamps it by the *declared* type:
        a `string` column gets the generation as text, and an unsupported type
        is silently left unstamped rather than failing the write.
      - `enriched_at` — `timestamp`. The runner sends `Instant.toString()`, and
        ADAS's row-write path accepts ISO-8601 for a `timestamp` column.
      - `group_version` (group-triggered pipelines only) — **`long`, never
        `timestamp`**. The runner sends epoch millis as a bare JSON number; a
        `timestamp` column runs it through the write-timestamp normalizer, which
        rejects a non-string with 422 — and normalization runs over the whole
        batch before ClickHouse, so **no row of the batch is written** and every
        group dead-letters at the upsert stage.
      - `model`, `group_version` and `truncated` stay empty on an enrichment
        computed by a `transform.type: sql` pipeline, which makes no model call
        and may not use the group trigger.
    Columns can be added afterwards with `adas_patch_table_schema`.

    Returns: the materialized `TableDto` with `status: "active"`.
      `{error: {code: "conflict"}}` (409) if already materialized;
      `invalid_argument` (422) if the schema is incomplete or inconsistent;
      `not_found` for an unknown table. On a ClickHouse failure the table is
      marked `FAILED` and the call may be retried.

    Related: adas_create_table, adas_describe_schema, adas_get_table,
      adas_create_pipeline.

    Examples:
      name="widget_sentiment", body={"grain_key": "request_id", "cardinality": "zero_or_one", "columns": [{"name": "sentiment", "source_name": "sentiment", "type": "string"}, {"name": "pipeline_generation", "source_name": "pipeline_generation", "type": "long"}, {"name": "model", "source_name": "model", "type": "string"}, {"name": "enriched_at", "source_name": "enriched_at", "type": "timestamp"}]}
      name="widget_metrics", body={"ordering_key": ["widget_id", "event_time"], "partition_by": {"column": "event_time", "granularity": "day"}, "identity_column": "event_id", "version_column": "event_time", "columns": [{"name": "event_id", "source_name": "event_id", "type": "string"}, {"name": "widget_id", "source_name": "widget_id", "type": "string"}, {"name": "event_time", "source_name": "event_time", "type": "timestamp"}]}
    """

    if not name:
        return invalid_argument("name is required")
    if not isinstance(body, dict) or not body:
        return invalid_argument("body must be a non-empty object")
    try:
        return await get_client().request(
            "POST",
            f"/v1/tables/{path_segment(name)}/schema",
            json=body,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_patch_table_schema(
    ctx: Context, name: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Evolve a materialized (ACTIVE) table's schema — add/drop/rename/retag columns, set a TTL.

    The live-schema surface, and the only way to change a table's columns after
    `adas_define_table_schema` has materialized it. **`ACTIVE` tables only**: a
    `PENDING` or `FAILED` table is rejected with 409 — define and materialize it
    first. The reverse also holds: an `ACTIVE` table can never go back through
    `adas_define_table_schema`.

    `body` is a `PatchSchemaRequest` — call `adas_describe_schema` with
    `type_name="PatchSchemaRequest"` for the exact shape.
    Members:
      - `add` — new columns, each `{name, source_name, type}` plus optional
        `element_type` (required iff `type` is `array`), `nullable`,
        `description`, `display_name`, `tag`, `sensitive`, `heavy`.
      - `drop` — exposed names to remove.
      - `rename` — `[{from, to}]`, `from` being the current exposed name. A
        column's `display_name`, `tag`, `sensitive`, `heavy` and `description`
        all survive a rename.
      - `update` — **catalog-only metadata**, keyed by the column's current
        exposed name: `{name, tag?, display_name?, description?, sensitive?,
        heavy?}`. Merge-patch per field — absent/null leaves it unchanged, a
        blank string clears it, a non-blank string sets it, and
        `sensitive`/`heavy` set the flag when present. Issues no `ALTER` at all,
        so a metadata-only request touches ClickHouse not at all.
      - `ttl` — merge-patch of the row-expiry TTL: absent/null leaves it,
        a full `{column, interval, unit}` sets or replaces it, and an empty
        object `{}` removes it. A partial object is 422. The TTL column resolves
        *after* this request's own drop/rename/add, so it may name a
        just-added or just-renamed column.

    All physical commands of one request render in the canonical order
    **drop → rename → add → ttl** and ship as a *single* multi-command
    `ALTER TABLE`, which ClickHouse validates as a whole: one invalid command
    and the physical table is left completely unchanged. The Postgres catalog is
    written only after the `ALTER` succeeds. A failed `ALTER` leaves the table
    `ACTIVE` — it is never marked `FAILED`.

    Rejected with 422: the frozen physical keys (`ordering_key`, `partition_by`,
    `grain_key`, `cardinality`), `tag_order` (that is `PUT /v1/tables/{name}`,
    not wrapped here) and `write`; a type change on an existing column; a
    drop/rename of a `_`-prefixed system column, the grain key, or an
    ordering-key column; a **drop** of the table's `identity_column` or
    `version_column` (a **rename** of either is allowed — it repoints the stored
    scan metadata and bumps the `generation` of every pipeline reading that
    source, without invalidating any scan cursor); a drop/rename of a source
    column that an `ACTIVE` enrichment names as its `grain_key`; a drop of the
    column the stored TTL references; `sensitive: true` on a cursor column; and
    an `update` of a column that does not exist after this request's own
    operations, or updated twice in one request.

    Rejected with **409**, naming the pipelines: on an enrichment, dropping or
    renaming a column another pipeline reads, or one that receives a transform
    output of the pipeline targeting it. That is checked before any `ALTER` is
    issued, so a refused patch leaves ClickHouse untouched. Also **409**
    `conflict`, whatever the patch, while the table's automatic relayout runs
    (`layout: "migrating"` on `adas_get_table`) — retry once it reads `aligned`.

    Aligned layout (see `adas_define_table_schema`): the hidden alignment
    column cannot be dropped or renamed; an adopted one cannot be marked
    `sensitive` or `heavy` (422). On an enrichment eligible for the aligned
    layout but not yet relaid out, an `add` of a column with the alignment
    column's name, or a `rename` onto it, is a 422 `validation_error` unless
    the column could be adopted (source column's type, non-nullable, neither
    flag). `"ttl": {}` does not remove an inherited TTL, which is not in the
    catalog; declaring a `ttl` replaces it.

    On an enrichment this is also where you add a **provenance** column an
    already-materialized target is missing — `pipeline_generation` (long),
    `model` (string), `enriched_at` (timestamp), `group_version` (long),
    `truncated` (boolean); one that is absent is
    silently skipped on write. This endpoint does **not** inject them the way
    `adas_define_table_schema` does: it applies exactly the operations you
    list, because widening an explicit operation list into a five-column DDL on
    a populated table is not something to do on a caller's behalf.
    `pipeline_generation` is the one exception you do not have to add by hand —
    a startup reconciler adds it to every enrichment an `enrich` pipeline
    targets.

    One bump to know about: on an `llm` target, an `update` that edits a
    column's `description` bumps the targeting pipeline's `generation`. A
    metadata-only patch normally bumps nothing, but an output's `prose`
    defaults to its column's `description`, which makes the description a live
    input to the composed prompt.

    The patch resolves against the columns **visible to you** — the set
    `adas_get_table` returns you: a `sensitive` column without the clearance,
    or one a column policy hides from you, is treated as not existing. While
    the stored TTL is on a column hidden from you, any `ttl` member is refused
    (403 `column_not_entitled` for a policy-hidden column, 403 `forbidden`
    for a sensitive one), and without the clearance any `sensitive` member in
    `update`, or an `add` declaring `sensitive: true`, is 403. A `drop` that
    would leave a role's column-policy allowlist empty is a 409
    `column_in_column_policy`; a rename is carried into the policies.

    Under `SECURITY_MODE=oidc` this needs `FULL_ADMIN` or a matching `modify`
    access role on the table. A `system` table (e.g. `dial_usage_log`) refuses
    it outright with 409 `table_is_system`.

    Returns: the updated `TableDto` (see `adas_get_table`).
      `{error: {code: "conflict"}}` (409) when the table is not materialized,
      is a system table, is being relaid out, or the patch would drop/rename a
      column a pipeline reads or writes; `invalid_argument` (422) for the rejections above;
      `not_found` for an unknown table.

    Related: adas_define_table_schema, adas_get_table, adas_describe_schema,
      adas_create_pipeline.

    Examples:
      name="conversation_insights", body={"add": [{"name": "group_version", "source_name": "group_version", "type": "long", "nullable": True, "tag": "provenance", "description": "Group version stamped by the Enrichment Runner"}]}
      name="conversation_insights", body={"update": [{"name": "topics", "description": "Topics, comma-plus-space separated"}]}
      name="widget_metrics", body={"rename": [{"from": "label", "to": "category"}], "ttl": {"column": "event_time", "interval": 90, "unit": "day"}}
      name="widget_metrics", body={"ttl": {}}
    """

    if not name:
        return invalid_argument("name is required")
    if not isinstance(body, dict) or not body:
        return invalid_argument("body must be a non-empty object")
    try:
        return await get_client().request(
            "PATCH",
            f"/v1/tables/{path_segment(name)}/schema",
            json=body,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_list_tables")(adas_list_tables)
    mcp.tool(name="adas_get_table")(adas_get_table)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_create_table")(adas_create_table)
    mcp.tool(name="adas_define_table_schema")(adas_define_table_schema)
    mcp.tool(name="adas_patch_table_schema")(adas_patch_table_schema)
