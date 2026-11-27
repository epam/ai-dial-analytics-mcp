from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default

import respx

from adas_mcp.tools.catalog import (
    adas_export_catalog,
    adas_import_catalog,
    adas_preview_catalog_export,
    adas_preview_catalog_import,
)

BUNDLE = {
    "format": "adas-catalog",
    "format_version": 4,
    "tables": [{"table": {"name": "t", "type": "source"}}],
    "pipelines": [],
    "access": {"t": {"analytics-viewers": {"privileges": ["get", "read"]}}},
}


def _parts(request) -> dict[str, tuple[str, bytes]]:
    """Split a multipart request into `{name: (content_type, payload)}`."""

    raw = (
        b"Content-Type: "
        + request.headers["content-type"].encode()
        + b"\r\n\r\n"
        + request.content
    )
    message = BytesParser(policy=default).parsebytes(raw)
    return {
        part.get_param("name", header="content-disposition"): (
            part.get_content_type(),
            part.get_payload(decode=True),
        )
        for part in message.iter_parts()
    }


async def test_export_preview_passes_through():
    body = {
        "objects": [
            {"type": "table", "name": "t", "description": None, "reason": "selected"}
        ],
        "required_system_tables": [],
        "skipped": [],
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/export/preview").respond(
            200, json=body
        )
        result = await adas_preview_catalog_export(
            ctx=None, components=[{"type": "table", "name": "t"}]
        )
    assert result == body
    assert json.loads(route.calls.last.request.content) == {
        "components": [{"type": "table", "name": "t"}]
    }


async def test_export_without_components_sends_empty_body():
    """No selection means everything; ADAS reads `{}` as an absent list."""

    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/export").respond(
            200, json=BUNDLE
        )
        result = await adas_export_catalog(ctx=None)
    assert result == BUNDLE
    assert json.loads(route.calls.last.request.content) == {}


async def test_export_sends_include_access_and_selectors():
    """Selectors pass through as given; ADAS owns their rules."""

    components = [
        {"type": "pipeline", "name": "p", "dependencies": False},
        {"type": "table", "name": "t", "access_only": True, "roles": ["r"]},
    ]
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/export").respond(
            200, json=BUNDLE
        )
        await adas_export_catalog(ctx=None, components=components, include_access=True)
    assert json.loads(route.calls.last.request.content) == {
        "components": components,
        "include_access": True,
    }


async def test_export_preview_with_access_only_sends_the_flag():
    preview = {
        "objects": [],
        "required_system_tables": [],
        "skipped": [],
        "access": [{"table": "t", "roles": ["r"], "left_behind": []}],
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/export/preview").respond(
            200, json=preview
        )
        result = await adas_preview_catalog_export(ctx=None, include_access=True)
    assert result == preview
    assert json.loads(route.calls.last.request.content) == {"include_access": True}


async def test_export_omits_include_access_by_default():
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/export").respond(
            200, json=BUNDLE
        )
        await adas_export_catalog(ctx=None, components=[{"type": "table", "name": "t"}])
    assert json.loads(route.calls.last.request.content) == {
        "components": [{"type": "table", "name": "t"}]
    }


async def test_export_rejects_unknown_component_type():
    result = await adas_export_catalog(
        ctx=None, components=[{"type": "saved_query", "name": "q"}]
    )
    assert result["error"]["code"] == "invalid_argument"


async def test_export_rejects_component_without_name():
    result = await adas_preview_catalog_export(ctx=None, components=[{"type": "table"}])
    assert result["error"]["code"] == "invalid_argument"


async def test_unknown_pipeline_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/catalog/export").respond(
            404,
            json={
                "status": 404,
                "error": "unknown_pipeline",
                "message": "no pipeline p",
            },
        )
        result = await adas_export_catalog(
            ctx=None, components=[{"type": "pipeline", "name": "p"}]
        )
    assert result["error"]["code"] == "not_found"
    assert result["error"]["details"]["error"] == "unknown_pipeline"


async def test_import_preview_sends_bundle_as_the_only_part():
    """The bundle is the `file` part; the policy is a query parameter."""

    preview = {"tables": [{"name": "t", "import_action": "CREATE"}], "pipelines": []}
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/import/preview").respond(
            200, json=preview
        )
        result = await adas_preview_catalog_import(
            ctx=None, bundle=BUNDLE, resolution_policy="SKIP_IF_EXISTS"
        )
    assert result == preview
    request = route.calls.last.request
    assert dict(request.url.params) == {"resolution_policy": "SKIP_IF_EXISTS"}
    parts = _parts(request)
    assert set(parts) == {"file"}
    assert parts["file"][0] == "application/json"
    assert json.loads(parts["file"][1]) == BUNDLE


async def test_import_sends_acknowledgement_as_query_parameter():
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/import").respond(
            200, json={"import_id": "i1", "outcome": "completed"}
        )
        result = await adas_import_catalog(
            ctx=None,
            bundle=BUNDLE,
            resolution_policy="SKIP_IF_EXISTS",
            acknowledge_reused_names=True,
        )
    assert result["outcome"] == "completed"
    request = route.calls.last.request
    assert request.url.params["acknowledge_reused_names"] == "true"
    assert set(_parts(request)) == {"file"}


async def test_import_omits_acknowledgement_by_default():
    """ADAS defaults it to false, so the parameter is sent only when true."""

    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/catalog/import").respond(
            200, json={"import_id": "i1", "outcome": "completed"}
        )
        await adas_import_catalog(
            ctx=None, bundle=BUNDLE, resolution_policy="FAIL_IF_EXISTS"
        )
    assert "acknowledge_reused_names" not in route.calls.last.request.url.params
    assert "include_access" not in route.calls.last.request.url.params


async def test_import_sends_include_access_as_query_parameter():
    for tool, path in (
        (adas_preview_catalog_import, "/v1/catalog/import/preview"),
        (adas_import_catalog, "/v1/catalog/import"),
    ):
        with respx.mock(assert_all_called=False) as router:
            route = router.post(f"http://adas.test{path}").respond(
                200, json={"access": []}
            )
            await tool(
                ctx=None,
                bundle=BUNDLE,
                resolution_policy="SKIP_IF_EXISTS",
                include_access=True,
            )
        request = route.calls.last.request
        assert dict(request.url.params) == {
            "resolution_policy": "SKIP_IF_EXISTS",
            "include_access": "true",
        }
        assert json.loads(_parts(request)["file"][1]) == BUNDLE


async def test_import_requires_bundle_and_policy():
    assert (
        await adas_import_catalog(
            ctx=None, bundle={}, resolution_policy="SKIP_IF_EXISTS"
        )
    )["error"]["code"] == "invalid_argument"
    assert (
        await adas_preview_catalog_import(ctx=None, bundle=BUNDLE, resolution_policy="")
    )["error"]["code"] == "invalid_argument"


async def test_import_conflict_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/catalog/import").respond(
            409,
            json={
                "status": 409,
                "error": "catalog_import_conflict",
                "message": "2 objects already exist; see /import/preview",
                "path": "/v1/catalog/import",
                "method": "POST",
            },
        )
        result = await adas_import_catalog(
            ctx=None, bundle=BUNDLE, resolution_policy="FAIL_IF_EXISTS"
        )
    assert result["error"]["code"] == "conflict"
    assert result["error"]["details"]["error"] == "catalog_import_conflict"


async def test_import_invalid_maps_invalid_argument_and_keeps_status():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/catalog/import").respond(
            422,
            json={
                "status": 422,
                "error": "catalog_import_invalid",
                "message": "see preview",
            },
        )
        result = await adas_import_catalog(
            ctx=None, bundle=BUNDLE, resolution_policy="SKIP_IF_EXISTS"
        )
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_import_docstrings_carry_shared_argument_section():
    for tool in (adas_preview_catalog_import, adas_import_catalog):
        assert "SKIP_IF_EXISTS" in tool.__doc__
        assert "include_access" in tool.__doc__
        assert "{args}" not in tool.__doc__


async def test_export_docstrings_carry_shared_argument_section():
    for tool in (adas_preview_catalog_export, adas_export_catalog):
        assert "access_only" in tool.__doc__
        assert "{args}" not in tool.__doc__
