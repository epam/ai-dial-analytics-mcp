from __future__ import annotations

import json

import respx

from adas_mcp.tools.backfill import (
    adas_get_backfill_status,
    adas_list_backfill_runs,
    adas_trigger_backfill,
)


async def test_status_passes_through():
    body = {
        "runId": "r1",
        "from": "2026-05-26",
        "to": "2026-06-24",
        "daysTotal": 30,
        "state": "running",
        "daysCompleted": 4,
    }
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/data-sync/backfill/r1").respond(200, json=body)
        result = await adas_get_backfill_status(ctx=None, run_id="r1")
    assert result == body


async def test_status_requires_run_id():
    result = await adas_get_backfill_status(ctx=None, run_id="")
    assert result["error"]["code"] == "invalid_argument"


async def test_unknown_run_id_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/data-sync/backfill/gone").respond(
            404, json={"message": "no backfill run with that id"}
        )
        result = await adas_get_backfill_status(ctx=None, run_id="gone")
    assert result["error"]["code"] == "not_found"


async def test_trigger_sends_from_and_to_wire_names():
    """The params are date_from/date_to; the wire fields must be from/to."""

    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/data-sync/backfill").respond(
            202,
            json={"runId": "r1", "from": "2026-05-26", "to": "2026-05-27", "days": 2},
        )
        result = await adas_trigger_backfill(
            ctx=None, date_from="2026-05-26", date_to="2026-05-27"
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"from": "2026-05-26", "to": "2026-05-27"}
    assert result["runId"] == "r1"


async def test_trigger_requires_both_dates():
    assert (await adas_trigger_backfill(ctx=None, date_from="", date_to="2026-05-27"))[
        "error"
    ]["code"] == "invalid_argument"
    assert (await adas_trigger_backfill(ctx=None, date_from="2026-05-26", date_to=""))[
        "error"
    ]["code"] == "invalid_argument"


async def test_trigger_while_running_maps_conflict():
    """The MCP code comes from the status; ADAS's own code survives in details."""

    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/data-sync/backfill").respond(
            409,
            json={
                "status": 409,
                "error": "backfill_conflict",
                "message": "A backfill run is already in progress",
                "path": "/v1/data-sync/backfill",
                "method": "POST",
            },
        )
        result = await adas_trigger_backfill(
            ctx=None, date_from="2026-05-26", date_to="2026-05-27"
        )
    assert result["error"]["code"] == "conflict"
    assert result["error"]["status"] == 409
    assert result["error"]["message"] == "A backfill run is already in progress"
    assert result["error"]["details"]["error"] == "backfill_conflict"


async def test_trigger_invalid_range_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/data-sync/backfill").respond(
            400, json={"message": "from must not be after to"}
        )
        result = await adas_trigger_backfill(
            ctx=None, date_from="2026-06-24", date_to="2026-05-26"
        )
    assert result["error"]["code"] == "invalid_argument"


async def test_status_passes_failed_dates_through():
    body = {
        "runId": "r2",
        "from": "2026-05-26",
        "to": "2026-05-30",
        "daysTotal": 5,
        "state": "failed",
        "daysCompleted": 2,
        "daysFailed": 1,
        "failedDates": ["2026-05-28"],
        "finishedAt": "2026-06-01T00:00:00Z",
    }
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/data-sync/backfill/r2").respond(200, json=body)
        result = await adas_get_backfill_status(ctx=None, run_id="r2")
    assert result["failedDates"] == ["2026-05-28"]


async def test_list_runs_omits_state_when_not_given():
    body = {
        "runs": [
            {"runId": "r2", "state": "completed"},
            {"runId": "r1", "state": "failed"},
        ]
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/data-sync/backfill").respond(
            200, json=body
        )
        result = await adas_list_backfill_runs(ctx=None)
    assert "state" not in route.calls.last.request.url.params
    # The envelope is ADAS's own and passes through unchanged (not re-wrapped as items).
    assert result == body


async def test_list_runs_sends_normalized_state():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/data-sync/backfill").respond(
            200, json={"runs": []}
        )
        result = await adas_list_backfill_runs(ctx=None, state=" Running ")
    assert route.calls.last.request.url.params["state"] == "running"
    assert result == {"runs": []}


async def test_list_runs_rejects_unknown_state_before_http():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/data-sync/backfill").respond(
            200, json={"runs": []}
        )
        unknown = await adas_list_backfill_runs(ctx=None, state="pending")
        empty = await adas_list_backfill_runs(ctx=None, state="")
    assert unknown["error"]["code"] == "invalid_argument"
    assert "running, completed, failed" in unknown["error"]["message"]
    assert empty["error"]["code"] == "invalid_argument"
    assert not route.called


async def test_list_runs_upstream_bad_request_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/data-sync/backfill").respond(
            400,
            json={
                "error": "bad_request",
                "message": "state must be one of [running, completed, failed]",
            },
        )
        result = await adas_list_backfill_runs(ctx=None, state="failed")
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 400
