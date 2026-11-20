from __future__ import annotations

import json

import respx

from adas_mcp.tools.sync_failures import (
    adas_list_data_sync_failures,
    adas_retry_data_sync_failures,
)


async def test_list_passes_through():
    body = {
        "failures": [
            {
                "kind": "blob",
                "subject": "logs/2026/05/26/a.log.gz",
                "day": "2026-05-26",
                "state": "parked",
                "failure_class": "deterministic",
                "reason": "mapping_error",
                "attempts": 1,
            }
        ],
        "truncated": False,
        "total": 1,
        "by_state": {"pending": 0, "parked": 1},
        "retry_sweep": {"enabled": True, "max_per_sweep": 50},
    }
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/data-sync/failures").respond(200, json=body)
        result = await adas_list_data_sync_failures(ctx=None)
    assert result == body


async def test_list_sends_from_and_to_wire_names_and_normalizes_enums():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/data-sync/failures").respond(
            200, json={"failures": [], "truncated": False, "total": 0}
        )
        await adas_list_data_sync_failures(
            ctx=None,
            state=" Parked ",
            kind="RECORDS",
            date_from="2026-05-26",
            date_to="2026-05-27",
            backfill_run_id="00000000-0000-4000-8000-000000000001",
            limit=10,
        )
    params = dict(route.calls.last.request.url.params)
    assert params == {
        "state": "parked",
        "kind": "records",
        "from": "2026-05-26",
        "to": "2026-05-27",
        "backfill_run_id": "00000000-0000-4000-8000-000000000001",
        "limit": "10",
    }


async def test_list_rejects_unknown_state_kind_and_limit_locally():
    bad = [{"state": "resolved"}, {"kind": "file"}, {"limit": 0}, {"limit": 1001}]
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/data-sync/failures")
        for kwargs in bad:
            result = await adas_list_data_sync_failures(ctx=None, **kwargs)
            assert result["error"]["code"] == "invalid_argument", kwargs
    assert not route.called


async def test_list_bad_request_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/data-sync/failures").respond(
            400, json={"error": "bad_request", "message": "from is after to"}
        )
        result = await adas_list_data_sync_failures(
            ctx=None, date_from="2026-05-27", date_to="2026-05-26"
        )
    assert result["error"]["code"] == "invalid_argument"


async def test_retry_sends_body_verbatim():
    body = {"targets": [{"kind": "records", "subject": "logs/a.log.gz"}]}
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/data-sync/failures/retry").respond(
            200, json={"requeued": 1, "retry_sweep": {"enabled": True}}
        )
        result = await adas_retry_data_sync_failures(ctx=None, body=body)
    assert json.loads(route.calls.last.request.content) == body
    assert result["requeued"] == 1


async def test_retry_refuses_empty_body():
    result = await adas_retry_data_sync_failures(ctx=None, body={})
    assert result["error"]["code"] == "invalid_argument"


async def test_retry_forbidden_maps_permission_denied():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/data-sync/failures/retry").respond(
            403, json={"error": "forbidden", "message": "DATA_SYNC_OPERATE required"}
        )
        result = await adas_retry_data_sync_failures(ctx=None, body={"state": "parked"})
    assert result["error"]["code"] == "permission_denied"
