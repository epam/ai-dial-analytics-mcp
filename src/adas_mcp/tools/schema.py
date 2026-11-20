"""Schema introspection over a bundled snapshot of ADAS's OpenAPI document.

The spec is committed at `src/adas_mcp/swagger/adas-openapi.json` rather than
fetched from the backend's `/v3/api-docs`, so schema lookups work with the
backend unreachable or `API_DOCS_ENABLED=false`, and the shapes an agent is
told about cannot drift mid-session. Refreshing it is a manual step — see
`docs/development.md`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..errors import structured_error

_SPEC_FILENAME = "adas-openapi.json"


def _candidate_paths() -> list[Path]:
    """Where the bundled spec may live: installed package first, then repo."""

    paths: list[Path] = []
    try:
        pkg_root = resources.files("adas_mcp")
        paths.append(Path(str(pkg_root / "swagger" / _SPEC_FILENAME)))
    except (ModuleNotFoundError, FileNotFoundError):
        pass
    repo_root = Path(__file__).resolve().parents[3]
    paths.append(repo_root / "src" / "adas_mcp" / "swagger" / _SPEC_FILENAME)
    return paths


@lru_cache(maxsize=1)
def _load_spec() -> dict[str, Any]:
    for path in _candidate_paths():
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(f"Could not locate bundled swagger/{_SPEC_FILENAME}")


async def adas_describe_schema(type_name: str | None = None) -> dict[str, Any]:
    """Return the JSON Schema for an ADAS API type, or list all type names.

    Reads the OpenAPI document bundled with this server (a committed snapshot of
    ADAS's `/v3/api-docs`, not a live fetch) and looks in `components.schemas`.
    Two modes:
      - `type_name=None` (default): returns `{type_names: [...]}` listing every
        component schema name. Use this to discover which DTOs exist.
      - `type_name="<name>"`: returns `{type_name, schema}` with
        `components.schemas.<name>` verbatim.

    Call this before composing a request body for any write tool — it is the
    authoritative field list for `CreatePipelineRequest`, `PatchPipelineRequest`,
    `CreateTableRequest`, `DefineSchemaRequest`, `PatchSchemaRequest`,
    `SavedQueryRequestDto`, `DashboardRequestDto` (with its
    `WidgetRequestDto`, `InlineQueryRequestDto` and `LayoutRequestDto`),
    `IngestFailureRetryRequest`, `AppRoutesSettingRequestDto` and
    `SessionsSourceSettingRequestDto`, including which fields are required and which enum values are accepted.
    An `enrich` pipeline's definition lives in the nested `transform` block of
    the pipeline request — `CreateTransformRequest` on `POST`,
    `PatchTransformRequest` on `PATCH`, which additionally accepts
    `output_vars` — so look those up too when declaring one. ADAS reads request bodies
    **strictly** — an unrecognized field is a 400 naming it — so look a shape
    up rather than guessing at a member.

    Because the spec is a snapshot, it reflects the ADAS build this server was
    packaged against; a much newer backend may have types it does not list, and
    it lists types whose endpoints this server does not wrap as tools — table
    access (`GET`/`PUT`/`DELETE /v1/tables/{name}/access`, role-keyed),
    the audit activity feed
    (`/v1/activities`) and the `revision` snapshot reads, table update/delete
    (`PUT`/`DELETE /v1/tables/{name}`), row writes (`POST
    /v1/tables/{name}/rows`), the OTLP sinks (`/v1/otel/**`), the pipeline
    data plane (`POST /v1/pipelines/{name}/scan` and friends), and the
    structured-query `execute`/`translate` endpoints.

    Returns:
      - List mode: `{"type_names": ["TableDto", ...]}`.
      - Lookup mode: `{"type_name": "<name>", "schema": {...}}`.
      - `{error: {...}}` if the name is unknown, or the bundled spec is missing
        or unreadable.

    Related: adas_create_pipeline, adas_patch_pipeline, adas_create_table,
      adas_create_saved_query, adas_create_dashboard,
      adas_describe_entity, adas_get_table.

    Examples:
      (no arguments)
      type_name="CreatePipelineRequest"
      type_name="TableDto"
    """

    try:
        spec = _load_spec()
    except (OSError, ValueError) as exc:
        # OSError covers the missing/unreadable file, ValueError a corrupt one
        # (json.JSONDecodeError). Tools never raise across the MCP boundary, so
        # both come back as an envelope.
        return structured_error(
            "internal_error", f"could not load the bundled OpenAPI spec: {exc}"
        )
    schemas = spec.get("components", {}).get("schemas", {})
    if type_name is None:
        return {"type_names": sorted(schemas.keys())}
    schema = schemas.get(type_name)
    if schema is None:
        return structured_error(
            "not_found",
            f"No component schema named {type_name!r}",
            available_hint=sorted(schemas.keys())[:20],
        )
    return {"type_name": type_name, "schema": schema}


def reset_cache_for_tests() -> None:
    _load_spec.cache_clear()


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_describe_schema")(adas_describe_schema)
