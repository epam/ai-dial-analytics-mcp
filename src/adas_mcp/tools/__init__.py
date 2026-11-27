from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import (
    backfill,
    catalog,
    dashboards,
    entities,
    pipelines,
    saved_queries,
    schema,
    settings,
    sql,
    sync_failures,
    tables,
)


def register_all(
    mcp: FastMCP,
    capabilities: str = "limited",
    execute_sql_enabled: bool = False,
) -> None:
    """Register tool groups on the FastMCP instance.

    Read-only tools are always on — they are the whole point of the `limited`
    capability. `adas_validate_sql` is one of them: it answers whether
    `adas_execute_sql` would accept a statement and contacts nothing to do it,
    so a `limited` deployment can compose SQL correctly rather than blind.

    When `capabilities == "full"`, the write-capable tools register too: every
    mutation (tables, pipelines, backfill trigger, failure requeue, catalog
    import, saved queries, dashboards, instance settings). The catalog
    export, its preview and the import preview write nothing, so they stay
    read-only.

    `adas_execute_sql` is on its own switch, `execute_sql_enabled`
    (`ADAS_ENABLE_EXECUTE_SQL`), and is **off by default**. It is read-only in
    SQL semantics but the only tool that actually executes against ClickHouse,
    so a deployment decides that independently of whether writes are allowed:
    `full` alone does not register it, and it can be turned on under `limited`.

    This is a local guardrail, not authorization: ADAS still enforces its own
    per-endpoint privileges under `SECURITY_MODE=oidc`.
    """

    # Read-only — always available (limited + full).
    schema.register(mcp)
    entities.register(mcp)
    tables.register(mcp)
    backfill.register(mcp)
    sync_failures.register(mcp)
    pipelines.register(mcp)
    sql.register(mcp)
    catalog.register(mcp)
    saved_queries.register(mcp)
    dashboards.register(mcp)
    settings.register(mcp)

    if execute_sql_enabled:
        sql.register_execute_sql(mcp)

    if capabilities == "full":
        tables.register_full(mcp)
        backfill.register_full(mcp)
        sync_failures.register_full(mcp)
        pipelines.register_full(mcp)
        catalog.register_full(mcp)
        saved_queries.register_full(mcp)
        dashboards.register_full(mcp)
        settings.register_full(mcp)
