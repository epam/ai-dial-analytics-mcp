from __future__ import annotations

import json

import respx

from adas_mcp.tools.dashboards import (
    adas_create_dashboard,
    adas_delete_dashboard,
    adas_get_dashboard,
    adas_list_dashboards,
    adas_replace_dashboard,
)

DASH_ID = "dash_00000000000000000000000001"
WIDGET = {
    "id": "w1",
    "row_limit": 10,
    "layout": {"x": 0, "y": 0, "w": 6, "h": 4},
    "inline_query": {
        "name": "Calls",
        "sql": "SELECT count(*) AS calls FROM dial_usage_log",
        "result_view": "table",
    },
}
DASHBOARD = {
    "id": DASH_ID,
    "name": "Usage overview",
    "visibility": "private",
    "owner_id": "user-1",
    "owner_email": "user@example.com",
    "time": {"mode": "relative", "period": "last_7d"},
    "compare": "previous-period",
    "widgets": [WIDGET],
    "generation": 3,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
    "permissions": {"write": True, "publish": False},
}


async def test_list_preserves_envelope():
    body = {
        "dashboards": [
            {
                "id": DASH_ID,
                "name": "Usage overview",
                "visibility": "private",
                "updated_at": "2026-01-02T00:00:00Z",
                "permissions": {"write": True, "publish": False},
            }
        ],
        "permissions": {"create_private": True, "create_public": False},
    }
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/dashboards").respond(200, json=body)
        result = await adas_list_dashboards(ctx=None)
    assert result == body


async def test_get_passes_through():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"http://adas.test/v1/dashboards/{DASH_ID}").respond(
            200, json=DASHBOARD
        )
        result = await adas_get_dashboard(ctx=None, dashboard_id=DASH_ID)
    assert result == DASHBOARD


async def test_get_requires_id():
    result = await adas_get_dashboard(ctx=None, dashboard_id="")
    assert result["error"]["code"] == "invalid_argument"


async def test_get_unknown_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/dashboards/dash_gone").respond(
            404, json={"error": "not_found", "message": "dashboard not found"}
        )
        result = await adas_get_dashboard(ctx=None, dashboard_id="dash_gone")
    assert result["error"]["code"] == "not_found"


async def test_get_escapes_the_id():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/dashboards/..%2Fpipelines").respond(
            404, json={}
        )
        await adas_get_dashboard(ctx=None, dashboard_id="../pipelines")
    assert route.called


async def test_create_drops_server_members_and_generation():
    """A dashboard read back can be sent as a create body (a copy)."""

    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/dashboards").respond(
            201, json=DASHBOARD
        )
        result = await adas_create_dashboard(ctx=None, body=DASHBOARD)
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "name": "Usage overview",
        "visibility": "private",
        "time": {"mode": "relative", "period": "last_7d"},
        "compare": "previous-period",
        "widgets": [WIDGET],
    }
    assert result["id"] == DASH_ID


async def test_create_requires_body():
    assert (await adas_create_dashboard(ctx=None, body={}))["error"][
        "code"
    ] == "invalid_argument"


async def test_create_widget_rejection_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/dashboards").respond(
            400,
            json={"error": "bad_request", "message": "widget w1: unknown entity"},
        )
        result = await adas_create_dashboard(
            ctx=None, body={"name": "d", "visibility": "private", "widgets": [WIDGET]}
        )
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 400


async def test_replace_keeps_generation_and_drops_server_members():
    with respx.mock(assert_all_called=False) as router:
        route = router.put(f"http://adas.test/v1/dashboards/{DASH_ID}").respond(
            200, json={**DASHBOARD, "generation": 4}
        )
        result = await adas_replace_dashboard(
            ctx=None, dashboard_id=DASH_ID, body={**DASHBOARD, "name": "Renamed"}
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent["generation"] == 3
    assert sent["name"] == "Renamed"
    assert (
        not {
            "id",
            "owner_id",
            "owner_email",
            "created_at",
            "updated_at",
            "permissions",
        }
        & sent.keys()
    )
    assert result["generation"] == 4


async def test_replace_requires_generation_without_http():
    with respx.mock(assert_all_called=False) as router:
        route = router.put(f"http://adas.test/v1/dashboards/{DASH_ID}")
        result = await adas_replace_dashboard(
            ctx=None,
            dashboard_id=DASH_ID,
            body={"name": "d", "visibility": "private", "widgets": []},
        )
    assert result["error"]["code"] == "invalid_argument"
    assert "generation" in result["error"]["message"]
    assert not route.called


async def test_replace_requires_id_and_body():
    assert (
        await adas_replace_dashboard(ctx=None, dashboard_id="", body={"generation": 1})
    )["error"]["code"] == "invalid_argument"
    assert (await adas_replace_dashboard(ctx=None, dashboard_id=DASH_ID, body={}))[
        "error"
    ]["code"] == "invalid_argument"


async def test_stale_generation_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.put(f"http://adas.test/v1/dashboards/{DASH_ID}").respond(
            409,
            json={
                "status": 409,
                "error": "dashboard_conflict",
                "message": "the dashboard changed since generation 3",
            },
        )
        result = await adas_replace_dashboard(
            ctx=None, dashboard_id=DASH_ID, body=DASHBOARD
        )
    assert result["error"]["code"] == "conflict"
    assert result["error"]["details"]["error"] == "dashboard_conflict"


async def test_delete_returns_ok():
    with respx.mock(assert_all_called=False) as router:
        router.delete(f"http://adas.test/v1/dashboards/{DASH_ID}").respond(204)
        result = await adas_delete_dashboard(ctx=None, dashboard_id=DASH_ID)
    assert result == {"ok": True}


async def test_delete_requires_id():
    result = await adas_delete_dashboard(ctx=None, dashboard_id="")
    assert result["error"]["code"] == "invalid_argument"


async def test_replace_keeps_inline_chart_legend():
    """A widget's inline chart legend passes through the server-member strip."""

    chart = {"type": "bar", "x_field": "day", "y_field": ["n"], "legend": {}}
    widget = {**WIDGET, "inline_query": {**WIDGET["inline_query"], "chart": chart}}
    with respx.mock(assert_all_called=False) as router:
        route = router.put(f"http://adas.test/v1/dashboards/{DASH_ID}").respond(
            200, json=DASHBOARD
        )
        await adas_replace_dashboard(
            ctx=None, dashboard_id=DASH_ID, body={**DASHBOARD, "widgets": [widget]}
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent["widgets"][0]["inline_query"]["chart"] == chart
