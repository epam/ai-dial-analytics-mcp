from __future__ import annotations

import json

import respx

from adas_mcp.tools.saved_queries import (
    adas_create_saved_query,
    adas_delete_saved_query,
    adas_get_saved_query,
    adas_list_saved_queries,
    adas_replace_saved_query,
)

SQ_ID = "sq_00000000000000000000000001"
SAVED = {
    "id": SQ_ID,
    "name": "Calls per deployment",
    "scope": "personal",
    "owner_id": "user-1",
    "owner_email": "user@example.com",
    "source": ["dial_usage_log"],
    "sql": "SELECT deployment, count(*) AS calls FROM dial_usage_log GROUP BY deployment",
    "time": {"mode": "relative", "period": "last_7d"},
    "result_view": "table",
    "generation": 2,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
}


async def test_list_preserves_envelope_and_sends_scope():
    body = {"saved_queries": [SAVED]}
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/saved-queries").respond(200, json=body)
        result = await adas_list_saved_queries(ctx=None, scope=" Common ")
    assert result == body
    assert route.calls.last.request.url.params["scope"] == "common"


async def test_list_omits_scope_by_default():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/saved-queries").respond(
            200, json={"saved_queries": []}
        )
        await adas_list_saved_queries(ctx=None)
    assert "scope" not in route.calls.last.request.url.params


async def test_list_rejects_unknown_scope_without_http():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/saved-queries")
        result = await adas_list_saved_queries(ctx=None, scope="everything")
    assert result["error"]["code"] == "invalid_argument"
    assert not route.called


async def test_get_passes_through():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"http://adas.test/v1/saved-queries/{SQ_ID}").respond(
            200, json=SAVED
        )
        result = await adas_get_saved_query(ctx=None, saved_query_id=SQ_ID)
    assert result == SAVED


async def test_get_requires_id():
    result = await adas_get_saved_query(ctx=None, saved_query_id="")
    assert result["error"]["code"] == "invalid_argument"


async def test_get_unknown_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/saved-queries/sq_gone").respond(
            404,
            json={
                "error": "not_found",
                "message": "saved query 'sq_gone' was not found",
            },
        )
        result = await adas_get_saved_query(ctx=None, saved_query_id="sq_gone")
    assert result["error"]["code"] == "not_found"


async def test_get_escapes_the_id():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/saved-queries/..%2Fdashboards").respond(
            404, json={}
        )
        await adas_get_saved_query(ctx=None, saved_query_id="../dashboards")
    assert route.called


async def test_create_drops_server_assigned_members():
    """A saved query read back can be sent as a create body (a copy)."""

    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/saved-queries").respond(
            201, json=SAVED
        )
        result = await adas_create_saved_query(ctx=None, body=SAVED)
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "name": SAVED["name"],
        "scope": "personal",
        "sql": SAVED["sql"],
        "time": SAVED["time"],
        "result_view": "table",
    }
    assert result["id"] == SQ_ID


async def test_create_requires_body():
    assert (await adas_create_saved_query(ctx=None, body={}))["error"][
        "code"
    ] == "invalid_argument"


async def test_create_validation_failure_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/saved-queries").respond(
            422,
            json={
                "error": "validation_error",
                "message": "exactly one of query or sql",
            },
        )
        result = await adas_create_saved_query(ctx=None, body={"name": "q"})
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_replace_puts_read_back_object_without_server_members():
    with respx.mock(assert_all_called=False) as router:
        route = router.put(f"http://adas.test/v1/saved-queries/{SQ_ID}").respond(
            200, json={**SAVED, "generation": 3}
        )
        result = await adas_replace_saved_query(
            ctx=None, saved_query_id=SQ_ID, body={**SAVED, "name": "Renamed"}
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent["name"] == "Renamed"
    assert not {"id", "generation", "source", "owner_id", "created_at"} & sent.keys()
    assert result["generation"] == 3


async def test_replace_requires_id_and_body():
    assert (
        await adas_replace_saved_query(ctx=None, saved_query_id="", body={"name": "q"})
    )["error"]["code"] == "invalid_argument"
    assert (await adas_replace_saved_query(ctx=None, saved_query_id=SQ_ID, body={}))[
        "error"
    ]["code"] == "invalid_argument"


async def test_delete_returns_ok():
    with respx.mock(assert_all_called=False) as router:
        router.delete(f"http://adas.test/v1/saved-queries/{SQ_ID}").respond(204)
        result = await adas_delete_saved_query(ctx=None, saved_query_id=SQ_ID)
    assert result == {"ok": True}


async def test_delete_requires_id():
    result = await adas_delete_saved_query(ctx=None, saved_query_id="")
    assert result["error"]["code"] == "invalid_argument"


async def test_replace_keeps_chart_legend():
    """Stripping server members is top-level only: a nested chart legend survives."""

    chart = {
        "type": "line",
        "x_field": "day",
        "y_field": ["cost"],
        "legend": {"visible": False, "position": "left"},
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.put(f"http://adas.test/v1/saved-queries/{SQ_ID}").respond(
            200, json={**SAVED, "chart": chart}
        )
        await adas_replace_saved_query(
            ctx=None, saved_query_id=SQ_ID, body={**SAVED, "chart": chart}
        )
    assert json.loads(route.calls.last.request.content)["chart"] == chart
