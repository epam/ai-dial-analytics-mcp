"""Registration smoke tests: the tool surface reaches FastMCP, and each
endpoint-backed tool makes exactly the call it claims to."""

from __future__ import annotations

import pytest
import respx
from mcp.server.fastmcp import FastMCP

from adas_mcp.tools import register_all

# Read-only tools — registered under every capability level.
EXPECTED_LIMITED = {
    "adas_describe_schema",
    "adas_list_entities",
    "adas_describe_entity",
    "adas_list_query_functions",
    "adas_list_sql_functions",
    "adas_validate_sql",
    "adas_list_tables",
    "adas_get_table",
    "adas_get_backfill_status",
    "adas_list_backfill_runs",
    "adas_list_data_sync_failures",
    "adas_list_pipelines",
    "adas_get_pipeline",
    "adas_preview_pipeline",
    "adas_preview_catalog_export",
    "adas_export_catalog",
    "adas_preview_catalog_import",
    "adas_list_saved_queries",
    "adas_get_saved_query",
    "adas_list_dashboards",
    "adas_get_dashboard",
    "adas_get_setting",
}

# Query execution — gated by its own flag (ADAS_ENABLE_EXECUTE_SQL), off by
# default and independent of ADAS_CAPABILITIES.
EXPECTED_EXECUTE_SQL = {"adas_execute_sql"}

# Write-capable tools — ADAS_CAPABILITIES=full only.
EXPECTED_FULL_ONLY = {
    "adas_create_table",
    "adas_define_table_schema",
    "adas_patch_table_schema",
    "adas_trigger_backfill",
    "adas_retry_data_sync_failures",
    "adas_create_pipeline",
    "adas_patch_pipeline",
    "adas_refresh_pipeline",
    "adas_import_catalog",
    "adas_create_saved_query",
    "adas_replace_saved_query",
    "adas_delete_saved_query",
    "adas_create_dashboard",
    "adas_replace_dashboard",
    "adas_delete_dashboard",
    "adas_replace_setting",
}

EXPECTED_FULL = EXPECTED_LIMITED | EXPECTED_FULL_ONLY

# Everything that can be registered at once: full writes + execution enabled.
EXPECTED_EVERYTHING = EXPECTED_FULL | EXPECTED_EXECUTE_SQL


def _registered(capabilities: str, execute_sql_enabled: bool = False) -> set[str]:
    mcp = FastMCP("smoke")
    register_all(mcp, capabilities, execute_sql_enabled)
    return {t.name for t in mcp._tool_manager.list_tools()}


@pytest.mark.parametrize(
    ("capabilities", "execute_sql_enabled", "expected"),
    [
        ("limited", False, EXPECTED_LIMITED),
        ("full", False, EXPECTED_FULL),
        ("limited", True, EXPECTED_LIMITED | EXPECTED_EXECUTE_SQL),
        ("full", True, EXPECTED_EVERYTHING),
    ],
)
def test_registered_surface_is_exact(
    capabilities: str, execute_sql_enabled: bool, expected: set[str]
):
    names = _registered(capabilities, execute_sql_enabled)
    assert not expected - names, f"expected tools missing: {expected - names}"
    assert not names - expected, f"unexpected tools registered: {names - expected}"


def test_limited_is_a_strict_subset_of_full():
    limited, full = _registered("limited"), _registered("full")
    assert limited < full
    assert full - limited == EXPECTED_FULL_ONLY


def test_writes_are_not_registered_under_limited():
    names = _registered("limited")
    leaked = names & EXPECTED_FULL_ONLY
    assert not leaked, f"write-capable tools leaked into limited: {leaked}"


def test_execute_sql_is_off_by_default_even_under_full():
    assert "adas_execute_sql" not in _registered("full")
    assert "adas_execute_sql" not in _registered("limited")


def test_execute_sql_is_independent_of_capabilities():
    """The flag turns execution on under `limited` without unlocking writes."""

    names = _registered("limited", execute_sql_enabled=True)
    assert "adas_execute_sql" in names
    assert not names & EXPECTED_FULL_ONLY


async def test_every_endpoint_tool_smoke_calls_backend():
    """Each endpoint-backed tool, invoked with minimal valid args, makes one
    HTTP call and returns a dict with no error envelope."""

    from adas_mcp.tools import (
        backfill,
        catalog,
        dashboards,
        entities,
        pipelines,
        saved_queries,
        settings,
        sql,
        sync_failures,
        tables,
    )

    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/queries/entities").respond(200, json=[])
        router.get("http://adas.test/v1/queries/entities/schema/x").respond(
            200, json={"entity": "x", "fields": []}
        )
        router.get("http://adas.test/v1/queries/functions").respond(200, json=[])
        router.get("http://adas.test/v1/queries/sql-functions").respond(
            200, json={"functions": []}
        )
        router.post("http://adas.test/v1/queries/execute-sql").respond(
            200, json={"rows": []}
        )
        router.post("http://adas.test/v1/queries/validate-sql").respond(
            200, json={"valid": True, "issues": [], "warnings": []}
        )
        router.get("http://adas.test/v1/tables").respond(200, json={"tables": []})
        router.post("http://adas.test/v1/tables").respond(
            201, json={"name": "x", "status": "PENDING"}
        )
        router.get("http://adas.test/v1/tables/x").respond(
            200, json={"name": "x", "columns": []}
        )
        router.post("http://adas.test/v1/tables/x/schema").respond(
            200, json={"name": "x", "status": "ACTIVE", "columns": []}
        )
        router.patch("http://adas.test/v1/tables/x/schema").respond(
            200, json={"name": "x", "status": "ACTIVE", "columns": []}
        )
        router.get("http://adas.test/v1/data-sync/backfill/r1").respond(
            200, json={"runId": "r1", "state": "running"}
        )
        router.get("http://adas.test/v1/data-sync/backfill").respond(
            200, json={"runs": []}
        )
        router.post("http://adas.test/v1/data-sync/backfill").respond(
            202,
            json={"runId": "r1", "from": "2026-05-26", "to": "2026-05-27", "days": 2},
        )
        router.get("http://adas.test/v1/pipelines").respond(200, json={"pipelines": []})
        router.post("http://adas.test/v1/pipelines").respond(201, json={"name": "p"})
        router.get("http://adas.test/v1/pipelines/p").respond(200, json={"name": "p"})
        router.patch("http://adas.test/v1/pipelines/p").respond(200, json={"name": "p"})
        router.post("http://adas.test/v1/pipelines/p/preview").respond(
            200,
            json={"statement": "SELECT ...", "columns": [], "rows": [], "notes": []},
        )
        router.post("http://adas.test/v1/pipelines/p/refresh").respond(
            200, json={"rows_written": 0, "has_more": False, "lag_seconds": 1}
        )
        router.post("http://adas.test/v1/catalog/export/preview").respond(
            200, json={"objects": [], "required_system_tables": [], "skipped": []}
        )
        router.post("http://adas.test/v1/catalog/export").respond(
            200, json={"format": "adas-catalog", "format_version": 1}
        )
        router.post("http://adas.test/v1/catalog/import/preview").respond(
            200, json={"tables": [], "pipelines": [], "required_system_tables": []}
        )
        router.post("http://adas.test/v1/catalog/import").respond(
            200, json={"import_id": "i1", "outcome": "completed"}
        )
        router.get("http://adas.test/v1/saved-queries").respond(
            200, json={"saved_queries": []}
        )
        router.post("http://adas.test/v1/saved-queries").respond(
            201, json={"id": "sq_1"}
        )
        router.get("http://adas.test/v1/saved-queries/sq_1").respond(
            200, json={"id": "sq_1"}
        )
        router.put("http://adas.test/v1/saved-queries/sq_1").respond(
            200, json={"id": "sq_1"}
        )
        router.delete("http://adas.test/v1/saved-queries/sq_1").respond(204)
        router.get("http://adas.test/v1/dashboards").respond(
            200, json={"dashboards": [], "permissions": {}}
        )
        router.post("http://adas.test/v1/dashboards").respond(
            201, json={"id": "dash_1"}
        )
        router.get("http://adas.test/v1/dashboards/dash_1").respond(
            200, json={"id": "dash_1"}
        )
        router.put("http://adas.test/v1/dashboards/dash_1").respond(
            200, json={"id": "dash_1"}
        )
        router.delete("http://adas.test/v1/dashboards/dash_1").respond(204)
        router.get("http://adas.test/v1/data-sync/failures").respond(
            200, json={"failures": [], "truncated": False, "total": 0}
        )
        router.post("http://adas.test/v1/data-sync/failures/retry").respond(
            200, json={"requeued": 0, "retry_sweep": {"enabled": True}}
        )
        router.get("http://adas.test/v1/settings/app-routes").respond(
            200, json={"routes": []}
        )
        router.put("http://adas.test/v1/settings/app-routes").respond(
            200, json={"routes": []}
        )

        calls = [
            entities.adas_list_entities(ctx=None),
            entities.adas_describe_entity(ctx=None, name="x"),
            entities.adas_list_query_functions(ctx=None),
            entities.adas_list_sql_functions(ctx=None),
            tables.adas_list_tables(ctx=None),
            tables.adas_get_table(ctx=None, name="x"),
            tables.adas_create_table(ctx=None, body={"name": "x", "type": "source"}),
            tables.adas_define_table_schema(
                ctx=None, name="x", body={"columns": [], "ordering_key": ["id"]}
            ),
            tables.adas_patch_table_schema(ctx=None, name="x", body={"drop": ["c"]}),
            sql.adas_execute_sql(ctx=None, sql="SELECT 1 FROM t"),
            sql.adas_validate_sql(ctx=None, sql="SELECT 1 FROM t"),
            backfill.adas_get_backfill_status(ctx=None, run_id="r1"),
            backfill.adas_list_backfill_runs(ctx=None),
            backfill.adas_trigger_backfill(
                ctx=None, date_from="2026-05-26", date_to="2026-05-27"
            ),
            pipelines.adas_list_pipelines(ctx=None),
            pipelines.adas_get_pipeline(ctx=None, name="p"),
            pipelines.adas_preview_pipeline(ctx=None, name="p"),
            pipelines.adas_create_pipeline(ctx=None, body={"name": "p"}),
            pipelines.adas_patch_pipeline(ctx=None, name="p", body={"enabled": True}),
            pipelines.adas_refresh_pipeline(ctx=None, name="p"),
            catalog.adas_preview_catalog_export(ctx=None),
            catalog.adas_export_catalog(ctx=None),
            catalog.adas_preview_catalog_import(
                ctx=None,
                bundle={"format": "adas-catalog"},
                resolution_policy="SKIP_IF_EXISTS",
            ),
            catalog.adas_import_catalog(
                ctx=None,
                bundle={"format": "adas-catalog"},
                resolution_policy="SKIP_IF_EXISTS",
            ),
            saved_queries.adas_list_saved_queries(ctx=None),
            saved_queries.adas_get_saved_query(ctx=None, saved_query_id="sq_1"),
            saved_queries.adas_create_saved_query(
                ctx=None, body={"name": "q", "sql": "SELECT 1 FROM t"}
            ),
            saved_queries.adas_replace_saved_query(
                ctx=None,
                saved_query_id="sq_1",
                body={"name": "q", "sql": "SELECT 1 FROM t"},
            ),
            saved_queries.adas_delete_saved_query(ctx=None, saved_query_id="sq_1"),
            dashboards.adas_list_dashboards(ctx=None),
            dashboards.adas_get_dashboard(ctx=None, dashboard_id="dash_1"),
            dashboards.adas_create_dashboard(
                ctx=None, body={"name": "d", "visibility": "private", "widgets": []}
            ),
            dashboards.adas_replace_dashboard(
                ctx=None,
                dashboard_id="dash_1",
                body={
                    "generation": 1,
                    "name": "d",
                    "visibility": "private",
                    "widgets": [],
                },
            ),
            dashboards.adas_delete_dashboard(ctx=None, dashboard_id="dash_1"),
            sync_failures.adas_list_data_sync_failures(ctx=None),
            sync_failures.adas_retry_data_sync_failures(
                ctx=None, body={"state": "parked"}
            ),
            settings.adas_get_setting(ctx=None, name="app-routes"),
            settings.adas_replace_setting(
                ctx=None, name="app-routes", body={"routes": []}
            ),
        ]
        # Guard: this list must not fall behind the registered surface.
        # `adas_describe_schema` makes no HTTP call — tests/test_schema.py covers it.
        expected_here = EXPECTED_EVERYTHING - {"adas_describe_schema"}
        called = {coro.__name__ for coro in calls}
        assert called == expected_here, (
            f"smoke list out of sync — missing: {expected_here - called}, "
            f"unexpected: {called - expected_here}"
        )

        for coro in calls:
            result = await coro
            assert isinstance(result, dict), f"non-dict result: {result!r}"
            assert "error" not in result, f"unexpected error envelope: {result!r}"
