from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client, path_segment
from ..errors import invalid_argument


async def adas_list_entities(ctx: Context) -> dict[str, Any]:
    """List the queryable entities exposed by ADAS.

    An entity is a materialized `source` table with all of its enrichments
    flattened in — it is the unit you query against. Enrichments are not listed
    separately; their exposed columns show up in the entity's schema.

    Returns: `{items: [{name}]}` where `name` is the wire name of an entity
      (e.g. `dial_usage_log`). Use it with `adas_describe_entity`.

    Related: adas_describe_entity, adas_list_tables.
    """

    return await get_client().request(
        "GET", "/v1/queries/entities", ctx=ctx, require_auth=False
    )


async def adas_describe_entity(ctx: Context, name: str) -> dict[str, Any]:
    """Describe the flat queryable schema of one entity.

    Returns the union of the source's columns plus every enrichment's exposed
    columns, with foreign-key/join columns hidden. A field the caller may not
    see is **omitted entirely**, indistinguishable from one that does not
    exist: a `sensitive` column without the clearance on its owning table, and
    a column a **column policy** hides from the caller's role (an enrichment's
    access follows its source's). This is the schema you use to build queries
    — field names here are the queryable names (some are JSONB-derived, e.g.
    `metric:Accuracy:score`).

    Returns: `{entity, fields: [{name, type, source, description, tag,
      sensitive, projection_only}]}`. `type` is one of string/integer/long/
      decimal/boolean/date/timestamp/uuid/object/array; `source` names the
      backing entity field. `projection_only: true` marks a field ADAS
      **computes after the query runs** rather than stores (e.g.
      `dial_usage_log_payload.request_body_full`, the request body rebuilt
      from its delta chain, and `...request_body_rebuild`, which says how):
      it may only be selected as a plain row-mode projection — never
      filtered, sorted, grouped, aggregated or passed to a function (400 on
      the query surfaces, 422 in a pipeline declaration, though a
      `transform.inputs` entry may bind it).
      Returns `{error: {code: "not_found"}}` for an unknown entity.

    Related: adas_list_entities, adas_get_table, adas_describe_schema.

    Examples:
      name="dial_usage_log"
    """

    if not name:
        return invalid_argument("name is required")
    return await get_client().request(
        "GET",
        f"/v1/queries/entities/schema/{path_segment(name)}",
        ctx=ctx,
        require_auth=False,
    )


async def adas_list_query_functions(ctx: Context) -> dict[str, Any]:
    """List the structured-query DSL's `fn` names — for pipeline `measures[].fn`, not SQL.

    This is ADAS's **DSL function catalog** (`GET /v1/queries/functions`): the
    closed set of names a structured-query `fn` expression accepts, and so the
    source of an aggregate pipeline's `measures[].fn` (its `aggregate` group).
    ADAS accepts no function outside it.

    **Writing SQL, use `adas_list_sql_functions` instead** — for
    `adas_execute_sql`, `adas_validate_sql`, and the SQL-parsed pipeline
    fragments (`filter`, `measures[].where`, `sql` transform output
    expressions). The two catalogs disagree on purpose: five names here
    (`plus`, `minus`, `multiply`, `div`, `modulo`) are not callable in SQL
    (write `+ - * / %`), and some signatures are spelled differently there
    (`percentile_cont(fraction) WITHIN GROUP (ORDER BY value)`, `count(*)`,
    `array_has_any(array, ARRAY[...])`).

    Returns: `{items: [{name, group, signature, returns, distinct_supported,
      deterministic, description, args}]}`. `deterministic` is false for the
      functions whose value depends on when they run — `now()` and its
      relatives — so two evaluations over the same rows need not agree;
      `group` is scalar/aggregate/ordered_set_aggregate/window;
      `returns` is one of string/integer/long/numeric/boolean/timestamp/array/
      same_as_argument/element_of_argument. Each arg carries `name`, `kind`
      (expression or a literal kind), optional `optional`/`variadic` flags,
      and optional `constraints` (`allowed_values`, `min`, `max`) — e.g.
      `date_trunc`'s unit is restricted to a fixed set of strings.

    Related: adas_list_sql_functions, adas_create_pipeline,
      adas_describe_entity.
    """

    return await get_client().request(
        "GET", "/v1/queries/functions", ctx=ctx, require_auth=False
    )


async def adas_list_sql_functions(ctx: Context) -> dict[str, Any]:
    """List every function, operator and construct SQL passed to `adas_execute_sql` / `adas_validate_sql` may use.

    This is the **SQL surface's own catalog** (`GET /v1/queries/sql-functions`):
    every function-like spelling ADAS's SQL front end accepts — functions,
    operators such as `+` and `||`, and keyword constructs such as `CASE` and
    `CAST(x AS VARCHAR)` — each with a runnable example. The same Calcite front
    end parses the SQL-shaped pipeline fragments, so it also covers a
    pipeline's `filter`, `measures[].where` and `sql` transform output
    expressions. ADAS accepts no spelling outside it: an unknown name, or a
    known one with the wrong arity or argument kinds, is rejected at
    translation, before ClickHouse is contacted — `adas_validate_sql` reports it
    as `unknown_function` / `unsupported_function` / `function_arity`, and this
    is the catalog those issues' remedies point at. Check here before writing
    SQL that calls a function, including aggregates like `count`/`sum`.

    It is exact in both directions: every entry, written as its `example`
    shows and under each alias, is accepted; the DSL's arithmetic names
    (`plus`, `minus`, `multiply`, `div`, `modulo`) are absent because SQL
    refuses them as calls. ClickHouse-native functions are not accepted. Not
    listed: the predicate and statement grammar (comparisons, `IN`,
    `BETWEEN`, `IS NULL`, `AND`/`OR`/`NOT`, joins, set operations).

    Returns: `{functions: [{name, form, aliases, group, signature, returns,
      distinct_supported, deterministic, description, dsl_function, example,
      args}]}`, ordered by `name` case-insensitively.
      - `name` is the primary spelling, lowercase, matched case-insensitively;
        `aliases` are other accepted spellings (`char_length` for `length`,
        `ceiling` for `ceil`, `current_timestamp` for `now`, `not like` for
        `like`), possibly empty.
      - `form` is `function` (`name(args)`), `operator` (`+`, `||`, `LIKE`,
        `ILIKE`) or `syntax` (`CASE … END`, `CAST(x AS VARCHAR)`).
      - `signature` is written **as in SQL** — `count(*)` alongside
        `count([DISTINCT] value)`, `percentile_cont(fraction) WITHIN GROUP
        (ORDER BY value)`, an `OVER` clause on the window-only functions.
      - `dsl_function` names the `adas_list_query_functions` entry it lowers
        onto, or `null` for SQL-only constructs (`coalesce`, `nullif`, `case`,
        `like`, `ilike`).
      - `example` is one complete `SELECT` the SQL surface accepts — a
        starting point to adapt.
      - `group`, `returns`, `distinct_supported`, `deterministic` and `args`
        mean what they do in the DSL catalog (`deterministic` is false for
        `now()` and its relatives, which pipeline declarations refuse).

    Related: adas_validate_sql, adas_execute_sql, adas_describe_entity,
      adas_list_query_functions.
    """

    return await get_client().request(
        "GET", "/v1/queries/sql-functions", ctx=ctx, require_auth=False
    )


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_list_entities")(adas_list_entities)
    mcp.tool(name="adas_describe_entity")(adas_describe_entity)
    mcp.tool(name="adas_list_query_functions")(adas_list_query_functions)
    mcp.tool(name="adas_list_sql_functions")(adas_list_sql_functions)
