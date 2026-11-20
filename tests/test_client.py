from __future__ import annotations

import httpx
import pytest
import respx
from httpx import Response

from adas_mcp.client import get_client, path_segment, reset_client_for_tests
from adas_mcp.errors import MissingAuthError


async def test_admin_api_key_header_used():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/tables").respond(200, json={})
        result = await get_client().request("GET", "/v1/tables", ctx=None)
    assert result == {}
    request = route.calls.last.request
    assert request.headers["Api-Key"] == "test-admin-key"
    assert "Authorization" not in request.headers
    assert request.headers["X-Correlation-ID"]


async def test_jwt_takes_precedence_over_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADAS_USER_JWT", "fake-jwt")
    reset_client_for_tests()
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/tables").respond(200, json={})
        await get_client().request("GET", "/v1/tables", ctx=None)
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer fake-jwt"
    assert "Api-Key" not in request.headers


def _ctx_with_headers(**headers: str):
    from types import SimpleNamespace

    request = SimpleNamespace(headers=httpx.Headers(headers))
    request_context = SimpleNamespace(meta=None, request=request)
    return SimpleNamespace(request_context=request_context)


async def test_forwarded_per_request_key_used():
    ctx = _ctx_with_headers(**{"Api-Key": "per-request-key"})
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/tables").respond(200, json={})
        await get_client().request("GET", "/v1/tables", ctx=ctx)
    request = route.calls.last.request
    assert request.headers["Api-Key"] == "per-request-key"
    assert "Authorization" not in request.headers


async def test_forwarded_jwt_takes_precedence_over_forwarded_key():
    ctx = _ctx_with_headers(
        Authorization="Bearer forwarded-jwt", **{"Api-Key": "per-request-key"}
    )
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/tables").respond(200, json={})
        await get_client().request("GET", "/v1/tables", ctx=ctx)
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer forwarded-jwt"
    assert "Api-Key" not in request.headers


async def test_missing_auth_raises_when_required(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADAS_API_KEY", raising=False)
    monkeypatch.delenv("ADAS_USER_JWT", raising=False)
    reset_client_for_tests()
    with pytest.raises(MissingAuthError):
        await get_client().request("GET", "/v1/tables", ctx=None)


async def test_missing_auth_allowed_when_not_required(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADAS_API_KEY", raising=False)
    monkeypatch.delenv("ADAS_USER_JWT", raising=False)
    reset_client_for_tests()
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/queries/entities").respond(200, json={})
        result = await get_client().request(
            "GET", "/v1/queries/entities", ctx=None, require_auth=False
        )
    assert result == {}


async def test_status_mapping_404():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables/x").respond(
            404, json={"message": "missing", "status": 404}
        )
        result = await get_client().request("GET", "/v1/tables/x", ctx=None)
    assert result["error"]["code"] == "not_found"
    assert result["error"]["status"] == 404
    assert result["error"]["message"] == "missing"


async def test_status_mapping_500():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables").respond(500, text="oops")
        result = await get_client().request("GET", "/v1/tables", ctx=None)
    assert result["error"]["code"] == "internal_error"
    assert result["error"]["status"] == 500


async def test_204_returns_ok():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables").mock(return_value=Response(204))
        result = await get_client().request("GET", "/v1/tables", ctx=None)
    assert result == {"ok": True}


async def test_list_response_wrapped():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/queries/entities").respond(
            200, json=[{"name": "a"}, {"name": "b"}]
        )
        result = await get_client().request("GET", "/v1/queries/entities", ctx=None)
    assert result == {"items": [{"name": "a"}, {"name": "b"}]}


async def test_transport_error_envelope():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/tables").mock(
            side_effect=httpx.ConnectError("nope")
        )
        result = await get_client().request("GET", "/v1/tables", ctx=None)
    assert result["error"]["code"] == "internal_error"
    assert "transport error" in result["error"]["message"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("dial_usage_log", "dial_usage_log"),
        (
            "0f9c1a2b-3d4e-5f60-8712-9a0b1c2d3e4f",
            "0f9c1a2b-3d4e-5f60-8712-9a0b1c2d3e4f",
        ),
        (2, "2"),
        ("../pipelines/p", "..%2Fpipelines%2Fp"),
        ("x?target=y", "x%3Ftarget%3Dy"),
        ("x#frag", "x%23frag"),
        ("a b", "a%20b"),
        ("..", "%2E%2E"),
        (".", "%2E"),
    ],
)
def test_path_segment_escapes_separators_and_leaves_names_alone(value, expected):
    assert path_segment(value) == expected


async def test_path_traversal_cannot_retarget_another_endpoint():
    """A caller-supplied name must not escape its own endpoint: httpx resolves
    dot segments when it merges the URL, so `../tables/dial_usage_log` would
    otherwise turn a saved-query DELETE into a table DELETE."""

    from adas_mcp.tools.saved_queries import adas_delete_saved_query

    with respx.mock(assert_all_called=False) as router:
        tables = router.delete("http://adas.test/v1/tables/dial_usage_log").respond(204)
        saved = router.delete(
            url__startswith="http://adas.test/v1/saved-queries/"
        ).respond(404, json={"message": "unknown saved query"})
        result = await adas_delete_saved_query(
            ctx=None, saved_query_id="../tables/dial_usage_log"
        )
    assert tables.call_count == 0, "traversal escaped the saved-queries endpoint"
    assert saved.call_count == 1
    assert str(saved.calls.last.request.url).endswith(
        "/v1/saved-queries/..%2Ftables%2Fdial_usage_log"
    )
    assert result["error"]["code"] == "not_found"


async def test_query_params_drop_none():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/tables").respond(200, json={})
        await get_client().request("GET", "/v1/tables", params={"type": None}, ctx=None)
    assert "type" not in route.calls.last.request.url.params
