"""Ad-hoc SQL: validation (`SqlValidationController`) and execution (`SqlQueryController`).

Two groups, and the split is the point. `adas_execute_sql` reaches ClickHouse,
so it sits behind its own feature flag, `ADAS_ENABLE_EXECUTE_SQL`, and is off
by default — independent of `ADAS_CAPABILITIES`, because executing a query is
not the same decision as allowing writes. `adas_validate_sql` reaches nothing —
it runs the same checks with the round trip removed — so it registers with the
read-only tools and is always available. Without it a deployment with execution
disabled has no SQL tool at all and has to compose statements blind.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client
from ..errors import MissingAuthError, invalid_argument, structured_error


async def adas_execute_sql(ctx: Context, sql: str) -> dict[str, Any]:
    """Execute one read-only SQL SELECT against a queryable entity.

    The statement is translated to ADAS's structured-query DSL and run through
    the same pipeline (and the same governance) as the DSL endpoint. Only the
    DSL-expressible SQL subset is accepted:

      - exactly one `SELECT` statement — no DDL, no DML, no batches
      - `LIKE` / `ILIKE` only for `'%text%'` patterns (`LIKE` is case-sensitive,
        `ILIKE` case-insensitive)
      - only the functions, operators and constructs in ADAS's SQL catalog —
        list them (SQL signatures, aliases, a runnable example each) with
        `adas_list_sql_functions`; any other spelling, ClickHouse-native
        functions included, is rejected before ClickHouse is contacted.
        Arithmetic is the operators `+ - * / %`, never the DSL names
        `plus(a, b)` etc.
      - `LIMIT` defaults to 100 and maxes at 1000. A higher `LIMIT` is
        **rejected, never clamped** — page with `OFFSET` instead.
      - `SELECT *` expands to the non-heavy source columns only; heavy and
        enrichment columns come back when named explicitly.

    Field names must come from `adas_describe_entity` — enrichment columns use
    their qualified flat name (`"deployment_ref.display_name"`, quoted).

    **Relative time windows are anchored on `now()`, never on a literal date.**
    When the request is relative to the present — "in the last 7 days", "this
    past hour", "over the last month", "recently" — filter with `now()` and an
    interval (`request_time >= now() - INTERVAL '7' DAY`), not with today's
    date written out as a literal. The statement then stays correct whenever
    it is re-run. Use literal timestamps only when the user names an explicit
    date or range ("on 2026-09-01", "during September 2026").

    A statement whose tables are all in `INFORMATION_SCHEMA` (`TABLES`,
    `COLUMNS`, `SCHEMATA`) is answered from ADAS's own catalog and never reaches
    ClickHouse, so none of the limits above apply to it and arithmetic/CAST are
    allowed.

    Returns: `{rows: [{<column>: <value>}]}` — every projected column is present
      in every row, `null` where the cell is null; arrays and objects come back
      as native JSON. There is no `totalCount` key on this path.
      `{error: {code: "invalid_argument"}}` for a parse failure, an unsupported
      construct, an unknown entity/column, or an over-limit `LIMIT`;
      `{error: {code: "internal_error", status: 503}}` when ClickHouse is down.

    Related: adas_describe_entity, adas_list_entities, adas_list_sql_functions.

    Examples:
      sql="SELECT request_id FROM dial_usage_log LIMIT 10"
      sql="SELECT deployment, count(*) AS n FROM dial_usage_log GROUP BY deployment ORDER BY n DESC LIMIT 10"
      sql="SELECT deployment, count(*) AS n FROM dial_usage_log WHERE request_time >= now() - INTERVAL '7' DAY GROUP BY deployment ORDER BY n DESC LIMIT 1"
      sql="SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'dial_usage_log'"
    """

    if not sql or not sql.strip():
        return invalid_argument("sql is required")
    try:
        return await get_client().request(
            "POST",
            "/v1/queries/execute-sql",
            json={"sql": sql},
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_validate_sql(ctx: Context, sql: str) -> dict[str, Any]:
    """Check whether a SQL SELECT would be accepted, without running it.

    **Use this before handing any statement to a user or to
    `adas_execute_sql`.** It runs every check `adas_execute_sql` runs except the
    ClickHouse round trip — the statement screen, parse, `SELECT *` expansion,
    entity and field validation, the closed function catalog, DSL or composite
    translation, and the row governance — so `valid: true` means execute-sql
    will not reject the statement. Nothing is executed and no data is read, so
    it is safe to call repeatedly while composing.

    The loop this is for: validate → fix what `issues` names → validate again →
    hand over only once `valid` is true.

    Returns: `{valid, effective_limit, issues: [...], warnings: [...]}` with
      HTTP 200 whether or not the statement is valid — an invalid statement is
      a normal result here, not an error, so do not treat `valid: false` as a
      failed call. `effective_limit` is the row ceiling execution would apply
      (the statement's own `LIMIT`, or the default 100), absent for an invalid
      statement and for an `INFORMATION_SCHEMA` one.

      Each issue and warning carries a stable `code`, the verbatim `message`,
      `line`/`column` where the diagnosis has a position, and a `remedy` where
      there is a concrete fix. `issues[0]` is the rejection execute-sql itself
      would return; further entries are additional unresolvable identifiers,
      reported together — so fix all of them before revalidating rather than
      one per call. An unrecognised diagnosis comes back as
      `code: "invalid_sql"` with its message intact.

      Codes worth handling specially, because the underlying message points
      away from the real problem: `unknown_column` (Calcite reports a
      select-list column that resolves to nothing as "depends on itself"),
      `in_list_too_long` (an `IN` list past 20 literals becomes a sub-query and
      is refused as `LogicalValues` — batch it), `function_arity`
      (`count()` → write `count(*)`), `reserved_word_alias` (an unquoted
      reserved word as an alias, reported at the preceding token's position).

      `{error: {code: "invalid_argument"}}` only for a malformed *request* — a
      missing or blank `sql`.

    **`valid: true` is not a promise that execution succeeds.** The query budget
    is enforced by ClickHouse as the statement runs, so a valid statement can
    still come back `422 query_budget_exceeded`. `warnings` names the shapes
    that most often do — `heavy_column_unbounded` (a heavy column read with no
    filter on a timestamp field), `unbounded_group_by`, and
    `default_limit_applied` (the result will be silently capped). Warnings are
    advisory heuristics, never verdicts, and never affect `valid`; act on them
    by narrowing the statement, not by treating it as broken. A composite
    statement (a join, CTE or subquery) receives only the limit warning.

    Field names must come from `adas_describe_entity`, functions and operators
    from `adas_list_sql_functions` (the closed catalog this check enforces, with
    each entry's SQL signature, arity and argument kinds — look there before
    fixing an `unknown_function`, `unsupported_function` or `function_arity`
    issue; not `adas_list_query_functions`, the DSL catalog, whose names and
    signatures differ in SQL).

    Anchor relative time windows ("last 7 days", "past hour") on `now()` —
    `request_time >= now() - INTERVAL '7' DAY` — not on a literal date; use
    literal timestamps only when the user names an explicit date or range.

    Related: adas_execute_sql, adas_describe_entity, adas_list_sql_functions.

    Examples:
      sql="SELECT request_id FROM dial_usage_log LIMIT 10"
      sql="SELECT deployment, count(*) AS n FROM dial_usage_log GROUP BY deployment"
    """

    if not sql or not sql.strip():
        return invalid_argument("sql is required")
    try:
        return await get_client().request(
            "POST",
            "/v1/queries/validate-sql",
            json={"sql": sql},
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_validate_sql")(adas_validate_sql)


def register_execute_sql(mcp: FastMCP) -> None:
    """Registered only when ADAS_ENABLE_EXECUTE_SQL is on — see tools/__init__."""

    mcp.tool(name="adas_execute_sql")(adas_execute_sql)
