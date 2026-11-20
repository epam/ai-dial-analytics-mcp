"""adas_describe_schema reads the bundled spec — no HTTP, no ctx."""

from __future__ import annotations

import json
from pathlib import Path

import respx

from adas_mcp.tools import schema as schema_module
from adas_mcp.tools.pipelines import _PIPELINE_KINDS, _PIPELINE_VIEWS
from adas_mcp.tools.schema import _load_spec, adas_describe_schema

_BUNDLED = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "adas_mcp"
    / "swagger"
    / "adas-openapi.json"
)


def test_bundled_spec_is_present_and_parses():
    assert _BUNDLED.is_file(), f"bundled spec missing at {_BUNDLED}"
    spec = json.loads(_BUNDLED.read_text(encoding="utf-8"))
    assert spec["components"]["schemas"], "bundled spec has no component schemas"
    assert spec["paths"], "bundled spec has no paths"


def test_bundled_spec_carries_the_table_layout():
    spec = json.loads(_BUNDLED.read_text(encoding="utf-8"))
    layout = spec["components"]["schemas"]["TableDto"]["properties"]["layout"]
    assert layout["enum"] == ["aligned", "migrating", "unsupported"]
    assert layout["readOnly"] is True


async def test_list_type_names_comes_from_the_bundle():
    result = await adas_describe_schema()
    names = result["type_names"]
    assert names == sorted(names), "type names must be sorted"
    # Types the write tools tell agents to look up.
    for expected in (
        "CreatePipelineRequest",
        "PatchPipelineRequest",
        # The enrich declaration's nested transform block, one record per verb.
        "CreateTransformRequest",
        "PatchTransformRequest",
        "SqlQueryRequestDto",
        "TableDto",
    ):
        assert expected in names, f"{expected} absent from the bundled spec"


async def test_lookup_by_type_name():
    spec = json.loads(_BUNDLED.read_text(encoding="utf-8"))
    result = await adas_describe_schema(type_name="SqlQueryRequestDto")
    assert result == {
        "type_name": "SqlQueryRequestDto",
        "schema": spec["components"]["schemas"]["SqlQueryRequestDto"],
    }


async def test_unknown_type_name_returns_not_found():
    result = await adas_describe_schema(type_name="Nope")
    assert result["error"]["code"] == "not_found"
    assert result["error"]["available_hint"], "expected a hint listing real names"


async def test_makes_no_http_call():
    # No routes are registered, and respx's assert_all_mocked defaults to True —
    # so any outbound request at all would raise here.
    with respx.mock(assert_all_called=False):
        result = await adas_describe_schema(type_name="TableDto")
    assert result["type_name"] == "TableDto"


async def test_spec_is_parsed_once():
    _load_spec.cache_clear()
    await adas_describe_schema()
    await adas_describe_schema(type_name="TableDto")
    assert _load_spec.cache_info().hits >= 1
    assert _load_spec.cache_info().misses == 1


async def test_missing_spec_maps_to_internal_error(monkeypatch):
    monkeypatch.setattr(schema_module, "_candidate_paths", lambda: [Path("/nope.json")])
    schema_module.reset_cache_for_tests()
    result = await adas_describe_schema()
    assert result["error"]["code"] == "internal_error"


async def test_corrupt_spec_maps_to_internal_error(monkeypatch, tmp_path):
    """A truncated snapshot must come back as an envelope — tools never raise
    across the MCP boundary."""

    broken = tmp_path / "adas-openapi.json"
    broken.write_text('{"components": {"schemas": ', encoding="utf-8")
    monkeypatch.setattr(schema_module, "_candidate_paths", lambda: [broken])
    schema_module.reset_cache_for_tests()
    result = await adas_describe_schema(type_name="TableDto")
    assert result["error"]["code"] == "internal_error"


def test_client_side_enums_match_the_bundled_spec():
    """The client rejects unknown `kind` / `view` values before spending a round
    trip, which means it keeps its own copy of two of ADAS's enums, and a copy can
    drift. This is the one drift the repo can detect locally.

    Values only — the enums' *order* is ADAS's presentation concern, and the client
    never publishes it."""

    spec = json.loads(_BUNDLED.read_text(encoding="utf-8"))

    published_kinds = spec["components"]["schemas"]["PipelineDto"]["properties"][
        "kind"
    ]["enum"]
    assert set(_PIPELINE_KINDS) == set(published_kinds), (
        f"client kinds {_PIPELINE_KINDS} vs published {published_kinds}"
    )

    view = next(
        p
        for p in spec["paths"]["/v1/pipelines"]["get"]["parameters"]
        if p["name"] == "view"
    )
    assert set(_PIPELINE_VIEWS) == set(view["schema"]["enum"]), (
        f"client views {_PIPELINE_VIEWS} vs published {view['schema']['enum']}"
    )
