from __future__ import annotations

import json

import respx

from adas_mcp.tools.sql import adas_execute_sql, adas_validate_sql


async def test_execute_wraps_sql_in_an_object():
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/queries/execute-sql").respond(
            200, json={"rows": [{"request_id": "a"}]}
        )
        result = await adas_execute_sql(
            ctx=None, sql="SELECT request_id FROM dial_usage_log LIMIT 1"
        )
    assert json.loads(route.calls.last.request.content) == {
        "sql": "SELECT request_id FROM dial_usage_log LIMIT 1"
    }
    assert result == {"rows": [{"request_id": "a"}]}


async def test_requires_sql():
    assert (await adas_execute_sql(ctx=None, sql=""))["error"][
        "code"
    ] == "invalid_argument"
    assert (await adas_execute_sql(ctx=None, sql="   "))["error"][
        "code"
    ] == "invalid_argument"


async def test_blank_sql_makes_no_http_call():
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/queries/execute-sql").respond(200)
        await adas_execute_sql(ctx=None, sql="  ")
    assert route.call_count == 0


async def test_over_limit_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/queries/execute-sql").respond(
            400, json={"message": "page limit 50000 exceeds the maximum of 1000 rows"}
        )
        result = await adas_execute_sql(
            ctx=None, sql="SELECT request_id FROM dial_usage_log LIMIT 50000"
        )
    assert result["error"]["code"] == "invalid_argument"
    assert "1000" in result["error"]["message"]


async def test_clickhouse_down_surfaces_status():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/queries/execute-sql").respond(
            503, json={"error": "clickhouse_unavailable", "message": "unavailable"}
        )
        result = await adas_execute_sql(ctx=None, sql="SELECT 1 FROM t")
    assert result["error"]["code"] == "internal_error"
    assert result["error"]["status"] == 503


async def test_null_cells_are_preserved():
    body = {"rows": [{"a": 1, "b": None}]}
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/queries/execute-sql").respond(200, json=body)
        result = await adas_execute_sql(ctx=None, sql="SELECT a, b FROM t")
    assert result == body


async def test_validate_wraps_sql_in_an_object():
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/queries/validate-sql").respond(
            200,
            json={"valid": True, "effective_limit": 10, "issues": [], "warnings": []},
        )
        result = await adas_validate_sql(
            ctx=None, sql="SELECT request_id FROM dial_usage_log LIMIT 10"
        )
    assert json.loads(route.calls.last.request.content) == {
        "sql": "SELECT request_id FROM dial_usage_log LIMIT 10"
    }
    assert result["valid"] is True


async def test_validate_requires_sql():
    assert (await adas_validate_sql(ctx=None, sql=""))["error"][
        "code"
    ] == "invalid_argument"


async def test_validate_blank_sql_makes_no_http_call():
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/queries/validate-sql").respond(200)
        await adas_validate_sql(ctx=None, sql="  ")
    assert route.call_count == 0


async def test_invalid_statement_is_not_an_error_envelope():
    """The endpoint answers 200 for an invalid statement, and the verdict must
    reach the caller as a result — not be mistaken for a failed call."""

    body = {
        "valid": False,
        "issues": [
            {
                "code": "unknown_column",
                "message": "column 'nope' is not a field of any entity the statement reads",
                "line": 1,
                "column": 8,
                "remedy": "did you mean 'note'?",
            }
        ],
        "warnings": [],
    }
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/queries/validate-sql").respond(200, json=body)
        result = await adas_validate_sql(
            ctx=None, sql="SELECT nope FROM dial_usage_log"
        )
    assert result == body
    assert "error" not in result
