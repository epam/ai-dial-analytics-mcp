from __future__ import annotations

import json

import respx

from adas_mcp.tools.tables import (
    adas_create_table,
    adas_define_table_schema,
    adas_get_table,
    adas_list_tables,
    adas_patch_table_schema,
)


async def test_list_tables_passes_through():
    body = {"tables": [{"name": "dial_usage_log", "type": "source"}]}
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables").respond(200, json=body)
        result = await adas_list_tables(ctx=None)
    assert result == body


async def test_get_table_requires_name():
    result = await adas_get_table(ctx=None, name="")
    assert result["error"]["code"] == "invalid_argument"


async def test_get_table_passes_through():
    body = {
        "name": "dial_usage_log",
        "type": "source",
        "columns": [{"name": "id", "type": "uuid", "nullable": False}],
    }
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables/dial_usage_log").respond(200, json=body)
        result = await adas_get_table(ctx=None, name="dial_usage_log")
    assert result == body


async def test_get_table_passes_layout_through():
    """`layout` is a read-only member of the single-table read only; the tool
    neither strips nor defaults it."""
    body = {
        "name": "usage_client_identity",
        "type": "enrichment",
        "partition_by": {"column": "request_time", "granularity": "day"},
        "layout": "aligned",
    }
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables/usage_client_identity").respond(
            200, json=body
        )
        result = await adas_get_table(ctx=None, name="usage_client_identity")
    assert result == body


async def test_get_table_unknown_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables/nope").respond(
            404, json={"message": "no such table"}
        )
        result = await adas_get_table(ctx=None, name="nope")
    assert result["error"]["code"] == "not_found"


async def test_create_table_requires_body():
    assert (await adas_create_table(ctx=None, body={}))["error"][
        "code"
    ] == "invalid_argument"
    assert (await adas_create_table(ctx=None, body="nope"))["error"][
        "code"
    ] == "invalid_argument"


async def test_create_table_posts_body_and_passes_through():
    body = {
        "name": "widget_sentiment",
        "type": "enrichment",
        "source_table": "dial_usage_log",
    }
    created = {**body, "status": "PENDING"}
    with respx.mock(assert_all_called=True) as router:
        route = router.post("http://adas.test/v1/tables").respond(201, json=created)
        result = await adas_create_table(ctx=None, body=body)
    assert result == created
    assert json.loads(route.calls.last.request.content) == body


async def test_create_table_duplicate_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/tables").respond(
            409, json={"message": "table exists"}
        )
        result = await adas_create_table(ctx=None, body={"name": "x", "type": "source"})
    assert result["error"]["code"] == "conflict"


async def test_create_table_rejecting_schema_fields_maps_invalid_argument():
    """ADAS answers a create carrying columns/keys with 422, not 400."""
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/tables").respond(
            422, json={"message": "columns are not accepted at create"}
        )
        result = await adas_create_table(
            ctx=None, body={"name": "x", "type": "source", "columns": []}
        )
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_define_table_schema_requires_name_and_body():
    assert (await adas_define_table_schema(ctx=None, name="", body={"columns": []}))[
        "error"
    ]["code"] == "invalid_argument"
    assert (await adas_define_table_schema(ctx=None, name="t", body={}))["error"][
        "code"
    ] == "invalid_argument"


async def test_define_table_schema_posts_to_schema_subpath():
    body = {
        "grain_key": "request_id",
        "cardinality": "zero_or_one",
        "columns": [
            {"name": "request_id", "source_name": "request_id", "type": "string"}
        ],
    }
    active = {
        "name": "widget_sentiment",
        "status": "ACTIVE",
        "columns": body["columns"],
    }
    with respx.mock(assert_all_called=True) as router:
        route = router.post(
            "http://adas.test/v1/tables/widget_sentiment/schema"
        ).respond(200, json=active)
        result = await adas_define_table_schema(
            ctx=None, name="widget_sentiment", body=body
        )
    assert result == active
    assert json.loads(route.calls.last.request.content) == body


async def test_define_table_schema_already_materialized_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/tables/t/schema").respond(
            409, json={"message": "already materialized"}
        )
        result = await adas_define_table_schema(
            ctx=None, name="t", body={"columns": []}
        )
    assert result["error"]["code"] == "conflict"


async def test_define_table_schema_unknown_table_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/tables/nope/schema").respond(
            404, json={"message": "no such table"}
        )
        result = await adas_define_table_schema(
            ctx=None, name="nope", body={"columns": []}
        )
    assert result["error"]["code"] == "not_found"


async def test_define_table_schema_escapes_the_path_segment():
    """A traversal-shaped name must not retarget the request at another path."""
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/tables/..%2Fpipelines%2Fp/schema").respond(
            404, json={"message": "no such table"}
        )
        result = await adas_define_table_schema(
            ctx=None, name="../pipelines/p", body={"columns": []}
        )
    assert result["error"]["code"] == "not_found"


async def test_patch_table_schema_requires_name_and_body():
    assert (await adas_patch_table_schema(ctx=None, name="", body={"drop": ["c"]}))[
        "error"
    ]["code"] == "invalid_argument"
    assert (await adas_patch_table_schema(ctx=None, name="t", body={}))["error"][
        "code"
    ] == "invalid_argument"


async def test_patch_table_schema_patches_the_schema_subpath():
    body = {
        "add": [
            {
                "name": "group_version",
                "source_name": "group_version",
                "type": "long",
                "nullable": True,
            }
        ],
        "update": [{"name": "topics", "description": "Topics, comma-plus-space"}],
    }
    patched = {"name": "conversation_insights", "status": "ACTIVE", "columns": []}
    with respx.mock(assert_all_called=True) as router:
        route = router.patch(
            "http://adas.test/v1/tables/conversation_insights/schema"
        ).respond(200, json=patched)
        result = await adas_patch_table_schema(
            ctx=None, name="conversation_insights", body=body
        )
    assert result == patched
    assert json.loads(route.calls.last.request.content) == body


async def test_patch_table_schema_before_materialization_maps_conflict():
    """A PENDING/FAILED table answers 409 — it materializes via POST, not PATCH."""
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/tables/t/schema").respond(
            409, json={"message": "table is not materialized"}
        )
        result = await adas_patch_table_schema(ctx=None, name="t", body={"drop": ["c"]})
    assert result["error"]["code"] == "conflict"


async def test_patch_table_schema_frozen_key_maps_invalid_argument():
    """Physical keys are frozen after materialization — ADAS answers 422."""
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/tables/t/schema").respond(
            422, json={"message": "ordering_key is not accepted here"}
        )
        result = await adas_patch_table_schema(
            ctx=None, name="t", body={"ordering_key": ["id"]}
        )
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_patch_table_schema_unknown_table_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/tables/nope/schema").respond(
            404, json={"message": "no such table"}
        )
        result = await adas_patch_table_schema(
            ctx=None, name="nope", body={"drop": ["c"]}
        )
    assert result["error"]["code"] == "not_found"


async def test_patch_table_schema_escapes_the_path_segment():
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/tables/..%2Fpipelines%2Fp/schema").respond(
            404, json={"message": "no such table"}
        )
        result = await adas_patch_table_schema(
            ctx=None, name="../pipelines/p", body={"drop": ["c"]}
        )
    assert result["error"]["code"] == "not_found"
