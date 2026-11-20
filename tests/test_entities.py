from __future__ import annotations

import pytest
import respx

from adas_mcp.client import reset_client_for_tests
from adas_mcp.tools.entities import (
    adas_describe_entity,
    adas_list_entities,
    adas_list_query_functions,
    adas_list_sql_functions,
)


async def test_discovery_works_without_credentials(monkeypatch: pytest.MonkeyPatch):
    # ADAS default SECURITY_MODE=none needs no creds — discovery tools must not
    # hard-fail locally when none are configured.
    monkeypatch.delenv("ADAS_API_KEY", raising=False)
    monkeypatch.delenv("ADAS_USER_JWT", raising=False)
    reset_client_for_tests()
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/queries/entities").respond(
            200, json=[{"name": "dial_usage_log"}]
        )
        result = await adas_list_entities(ctx=None)
    assert result == {"items": [{"name": "dial_usage_log"}]}
    assert "Authorization" not in route.calls.last.request.headers
    assert "Api-Key" not in route.calls.last.request.headers


async def test_list_entities_wraps_bare_list():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/queries/entities").respond(
            200, json=[{"name": "dial_usage_log"}, {"name": "rate_analytics"}]
        )
        result = await adas_list_entities(ctx=None)
    assert result == {"items": [{"name": "dial_usage_log"}, {"name": "rate_analytics"}]}


async def test_describe_entity_requires_name():
    result = await adas_describe_entity(ctx=None, name="")
    assert result["error"]["code"] == "invalid_argument"


async def test_describe_entity_passes_through():
    body = {"entity": "dial_usage_log", "fields": [{"name": "id", "type": "uuid"}]}
    with respx.mock(assert_all_called=False) as router:
        router.get(
            "http://adas.test/v1/queries/entities/schema/dial_usage_log"
        ).respond(200, json=body)
        result = await adas_describe_entity(ctx=None, name="dial_usage_log")
    assert result == body


async def test_list_query_functions_wraps_bare_list():
    body = [
        {
            "name": "date_trunc",
            "group": "scalar",
            "signature": "date_trunc(unit, timestamp)",
            "returns": "timestamp",
            "distinct_supported": False,
            "args": [
                {
                    "name": "unit",
                    "kind": "string_literal",
                    "constraints": {"allowed_values": ["day", "hour"]},
                }
            ],
        }
    ]
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/queries/functions").respond(200, json=body)
        result = await adas_list_query_functions(ctx=None)
    assert result == {"items": body}


async def test_describe_entity_unknown_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/queries/entities/schema/nope").respond(
            404, json={"message": "unknown entity"}
        )
        result = await adas_describe_entity(ctx=None, name="nope")
    assert result["error"]["code"] == "not_found"
    assert result["error"]["message"] == "unknown entity"


async def test_list_sql_functions_preserves_envelope():
    """`/sql-functions` answers an object envelope, not a bare array — it must
    pass through as `{functions: [...]}`, not be re-wrapped in `items`."""

    body = {
        "functions": [
            {
                "name": "length",
                "form": "function",
                "aliases": ["char_length", "character_length"],
                "group": "scalar",
                "signature": "length(value)",
                "returns": "integer",
                "distinct_supported": False,
                "deterministic": True,
                "description": "Length of a string.",
                "dsl_function": "length",
                "example": "SELECT length(deployment) AS n FROM dial_usage_log LIMIT 10",
                "args": [{"name": "value", "kind": "expression"}],
            },
            {
                "name": "coalesce",
                "form": "function",
                "aliases": [],
                "dsl_function": None,
            },
        ]
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/queries/sql-functions").respond(
            200, json=body
        )
        result = await adas_list_sql_functions(ctx=None)
    assert route.called
    assert result == body


async def test_list_sql_functions_forbidden_maps_permission_denied():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/queries/sql-functions").respond(
            403, json={"message": "forbidden"}
        )
        result = await adas_list_sql_functions(ctx=None)
    assert result["error"]["code"] == "permission_denied"


def test_list_sql_functions_is_discoverable_from_sql():
    """The first docstring line is the MCP tool summary many clients show; it
    must say this is the catalog SQL passed to the SQL tools may use."""

    summary = adas_list_sql_functions.__doc__.strip().splitlines()[0]
    assert "adas_execute_sql" in summary
    assert "adas_validate_sql" in summary
    assert "SQL" in summary


def test_list_query_functions_steers_sql_callers_away():
    """The DSL catalog lists names SQL refuses (`plus`, ...) — its summary must
    not advertise it for SQL, and its body must name the SQL catalog."""

    doc = adas_list_query_functions.__doc__
    summary = doc.strip().splitlines()[0]
    assert "adas_execute_sql" not in summary
    assert "adas_list_sql_functions" in doc


def test_sql_tools_point_at_the_sql_catalog():
    from adas_mcp.tools.sql import adas_execute_sql, adas_validate_sql

    for fn in (adas_execute_sql, adas_validate_sql):
        assert "adas_list_sql_functions" in fn.__doc__
