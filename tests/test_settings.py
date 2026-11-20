from __future__ import annotations

import json

import respx

from adas_mcp.tools.settings import adas_get_setting, adas_replace_setting


async def test_get_passes_through():
    body = {
        "routes": [{"deployment": "insights-app", "route": "insights"}],
        "updated_at": "2026-01-01T00:00:00Z",
        "updated_by": None,
    }
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/settings/app-routes").respond(200, json=body)
        result = await adas_get_setting(ctx=None, name="App-Routes")
    assert result == body


async def test_get_rejects_unknown_name_locally():
    for name in ("", "budgets", "../tables/x"):
        result = await adas_get_setting(ctx=None, name=name)
        assert result["error"]["code"] == "invalid_argument"


async def test_never_written_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/settings/sessions-source").respond(
            404, json={"error": "setting_not_found", "message": "not set"}
        )
        result = await adas_get_setting(ctx=None, name="sessions-source")
    assert result["error"]["code"] == "not_found"


async def test_replace_drops_server_stamped_members():
    """A read-back document round-trips: updated_at/updated_by are ADAS's."""

    read_back = {
        "routes": [{"deployment": "insights-app", "route": "insights"}],
        "updated_at": "2026-01-01T00:00:00Z",
        "updated_by": "admin",
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.put("http://adas.test/v1/settings/app-routes").respond(
            200, json=read_back
        )
        await adas_replace_setting(ctx=None, name="app-routes", body=read_back)
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"routes": [{"deployment": "insights-app", "route": "insights"}]}


async def test_replace_requires_body():
    result = await adas_replace_setting(ctx=None, name="app-routes", body={})
    assert result["error"]["code"] == "invalid_argument"


async def test_route_in_use_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.put("http://adas.test/v1/settings/app-routes").respond(
            409, json={"error": "app_route_in_use", "message": "intents-live"}
        )
        result = await adas_replace_setting(
            ctx=None, name="app-routes", body={"routes": []}
        )
    assert result["error"]["code"] == "conflict"
    assert result["error"]["status"] == 409
