"""Catalog promotion (`CatalogTransferController`, `/v1/catalog`).

Moves user tables (identity, columns as stored) and pipelines from one
ADAS environment to another as a versioned **bundle**: export on the source,
import on the target. The API is shaped like deployment manager's config
export/import — the export selection in a JSON body, the import as a multipart
upload with `resolution_policy` (and `acknowledge_reused_names`) as query
parameters, and a preview twin for each call — so one admin UI drives both
services alike.

Three of the four tools write nothing and register under `limited`:
`adas_preview_catalog_export`, `adas_export_catalog` and
`adas_preview_catalog_import`. Only `adas_import_catalog` creates anything, so
it alone is `full`-only. The backend authorizes all four on its own: export
needs `REGISTRY_READ` (plus `TABLE_GET` per table, sensitive clearance per
table with a sensitive column, and no column hidden by a column policy), both import calls need `TABLE_CREATE` (plus
`TABLE_MODIFY`, `REGISTRY_ADMIN` and `TABLE_SENSITIVE` as the bundle requires).

What a promotion never does, whatever the tool:
  - **change an existing object.** `SKIP_IF_EXISTS` keeps it as it is and
    `FAIL_IF_EXISTS` refuses the import; `OVERWRITE` is refused (422).
  - **carry or write access.** Access is each environment's own policy: the
    bundle holds no access entries, row policies or column policies, and every
    imported table starts ungranted. Grant each source on the target afterwards,
    one role at a time (`PUT /v1/tables/{name}/access?role={role}`, which this
    server does not wrap); an enrichment follows its source's read access and
    takes only `write`/`modify` of its own.
  - **carry instance settings.** `app-routes` and `sessions-source` belong to
    the instance: an imported `dial_app_route` pipeline previews
    `armable: false` naming `app-routes` until its pair is allowed there
    (`adas_replace_setting`).
  - **write a system table.** `dial_usage_log`, `rate_analytics` and
    `dial_usage_log_payload` travel **by name only**, under
    `required_system_tables`: import checks that each exists on the target as
    an ACTIVE system table, and nothing more.
  - **enable a pipeline.** Every imported pipeline lands disabled; arming is a
    separate `adas_patch_pipeline` with `{"enabled": true}`.
  - **carry runner state or data.** Aggregates rebuild from zero once armed,
    and a data-sync backfill is a separate step.

The full operator sequence is ADAS's `docs/catalog-promotion.md`.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client
from ..errors import MissingAuthError, invalid_argument, structured_error

_COMPONENT_TYPES = ("table", "pipeline")


def _export_body(components: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Validate the component selection and build the `ExportRequest` body."""

    if components is None:
        return {}
    if not isinstance(components, list):
        return invalid_argument("components must be a list of {type, name} objects")
    for index, component in enumerate(components):
        if not isinstance(component, dict) or not component.get("name"):
            return invalid_argument(
                f"components[{index}] must be an object with a name"
            )
        if component.get("type") not in _COMPONENT_TYPES:
            return invalid_argument(
                f"components[{index}].type must be one of {', '.join(_COMPONENT_TYPES)}"
            )
    return {"components": components}


async def adas_preview_catalog_export(
    ctx: Context, components: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """List what a catalog export would carry, and why, without the bundle itself.

    **Writes nothing.** Takes the same selection as `adas_export_catalog` and
    resolves the same dependency closure, so call this first: it shows what
    the closure pulled in before you fetch a bundle that can run to hundreds of
    kilobytes.

    `components` lists the objects to export as `{"type": "table" | "pipeline",
    "name": "..."}`. Omitted or empty, it means **everything exportable**: every
    ACTIVE user table and every pipeline that reads no OTLP landing table.

    The closure runs in both directions: a table pulls in its `source_table`
    and every pipeline that targets it; a pipeline pulls in its inputs and its
    target; both pull in every enrichment a pipeline reads, including one named
    only as an `"<enrichment>.<column>"` token inside a filter or measure.

    Returns: `{objects: [...], required_system_tables: [...], skipped: [...]}`.
      `objects` holds the tables and pipelines, in dependency order, each
      `{type, name, description, reason}`; `reason` is `selected` or the dependency
      that pulled the object in (e.g. `source_table of usage_sentiment`).
      `required_system_tables` are the system tables the bundle would require on
      the target rather than carry, each with the `reason` it is needed. `skipped` lists what an export of everything leaves out, with
      `reason` `otlp`, `pending`, `failed`, `unresolvable` or
      `dependency_skipped`.

    Errors (ADAS's own code is in `details.error`):
      - `not_found` (404) — a named table that does not exist or that the
        caller may not get (`not_found`), or a named pipeline that does not
        exist (`unknown_pipeline`).
      - `invalid_argument` (422 `catalog_export_invalid`) — a selection that
        names, or whose closure reaches, a system table, an OTLP landing table
        or a pipeline reading one, or a PENDING or FAILED table.
      - `permission_denied` (403 `column_not_entitled`) — a bundle table has
        a column a column policy hides from the caller (checked first, so it
        wins); (403 `sensitive_column_not_entitled`) — a bundle table has a
        sensitive column the caller may not see. Columns are never silently
        dropped. Exporting everything includes the enrichments of every
        source the caller may get, so either can come from one of those.

    Related: adas_export_catalog, adas_preview_catalog_import.

    Examples:
      (no arguments — preview an export of everything)
      components=[{"type": "table", "name": "usage_sentiment"}]
      components=[{"type": "pipeline", "name": "turns_rollup"}, {"type": "table", "name": "turns"}]
    """

    body = _export_body(components)
    if "error" in body:
        return body
    try:
        return await get_client().request(
            "POST", "/v1/catalog/export/preview", json=body, ctx=ctx, require_auth=False
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_export_catalog(
    ctx: Context, components: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Export a catalog bundle: the selected tables and pipelines with their dependency closure.

    **Writes nothing.** The selection and the closure are exactly those of
    `adas_preview_catalog_export`; preview first when you are not sure what the
    closure will add. Omitted or empty `components` exports everything
    exportable — about 200 KB on a real environment — so select what you mean
    to promote.

    The returned bundle is meant to be passed **unchanged** as `bundle` to
    `adas_preview_catalog_import` / `adas_import_catalog` on the target
    environment. ADAS binds it strictly: an unknown or misspelled member at any
    depth is a 400, so do not add, rename or annotate fields. Edit only values
    you mean to change.

    Bundle shape (`format: "adas-catalog"`, `format_version: 3`; version 3
    adds a transform's `route` and `advanced.input_token_budget`, and an
    older ADAS refuses it with `catalog_bundle_version_unsupported`, while
    version 1 and 2 bundles still import here):
      - `tables[]` — `{table, schema}` in request shape. Columns travel
        exactly as stored, provenance columns included. No grants. An
        enrichment ADAS laid out aligned travels without `partition_by`, its
        hidden alignment column or `layout`: import's define-schema re-derives
        the layout from the target's source (an adopted, visible alignment
        column travels as an ordinary column and is adopted again).
      - `pipelines[]` — `{pipeline, exported_enabled}`: the `view=source`
        projection without `generation`, `state`, `enabled` and timestamps.
        `exported_enabled` is informational; import always creates the
        pipeline disabled.
      - `required_system_tables[]` — the names of the system tables the closure
        reaches, e.g. `["dial_usage_log"]`. Import checks they exist and never
        writes them.
      - `skipped[]` and `exported_at`.

    A bundle imports into the same or a newer ADAS; an older one refuses a
    newer `format_version` with 422 `catalog_bundle_version_unsupported`.

    Returns: the bundle object. Errors are those of `adas_preview_catalog_export`.

    Related: adas_preview_catalog_export, adas_preview_catalog_import,
      adas_import_catalog.

    Examples:
      components=[{"type": "table", "name": "usage_sentiment"}]
      components=[{"type": "pipeline", "name": "turns_rollup"}]
    """

    body = _export_body(components)
    if "error" in body:
        return body
    try:
        return await get_client().request(
            "POST", "/v1/catalog/export", json=body, ctx=ctx, require_auth=False
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def _import_request(
    bundle: dict[str, Any],
    resolution_policy: str,
    acknowledge_reused_names: bool,
) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, dict[str, Any] | None]:
    """Validate the import arguments and build the multipart part and query.

    Returns `((files, params), None)` on success or `(None, error_envelope)`.
    The bundle is re-serialized as the only part, `file`; both options are
    query parameters. `acknowledge_reused_names` is sent only when true, since
    ADAS defaults it to false.
    """

    if not isinstance(bundle, dict) or not bundle:
        return None, invalid_argument(
            "bundle is required: pass the object adas_export_catalog returned"
        )
    if not resolution_policy:
        return None, invalid_argument(
            "resolution_policy is required: FAIL_IF_EXISTS or SKIP_IF_EXISTS"
        )
    files: dict[str, Any] = {
        "file": ("bundle.json", json.dumps(bundle).encode("utf-8"), "application/json")
    }
    params: dict[str, Any] = {"resolution_policy": resolution_policy}
    if acknowledge_reused_names:
        params["acknowledge_reused_names"] = "true"
    return (files, params), None


_IMPORT_ARGS_DOC = """
    Arguments:
      - `bundle` — the object `adas_export_catalog` returned, **unchanged**. It
        is bound strictly: an unknown member at any depth is a 400.
      - `resolution_policy` — required; what to do with a bundle object that
        already exists on the target (compared by name):
          - `FAIL_IF_EXISTS` — any existing object refuses the import (409
            `catalog_import_conflict`, nothing written).
          - `SKIP_IF_EXISTS` — existing objects are kept unchanged and the rest
            is imported; later objects are validated against the *existing*
            one, so a mismatch shows up as a pipeline with `armable: false`.
            Re-running a bundle after a partial failure under this policy
            converges.
          - `OVERWRITE` — refused (422 `resolution_policy_unsupported`):
            import never changes an existing object.
      - `acknowledge_reused_names` — optional, default false; accept creating a
        pipeline whose
        name the target once used for a pipeline that was enabled and then
        deleted. The Enrichment Runner keeps that pipeline's scan cursor, dead
        letters and spend counters by name, so a recreated enrich pipeline
        resumes from the old cursor against an empty target and silently skips
        history. Without it such a bundle is refused; the preview flags the
        entry `reused_name: true`.

    Each entry under `tables` and `pipelines` is
    `{name, import_action, prev, next, differs, metadata_only, problems[]}`:
      - `import_action` — `CREATE` (new), `SKIP` (exists under
        `SKIP_IF_EXISTS`), or `FAIL` (exists under `FAIL_IF_EXISTS`, or fails
        validation; read `problems`).
      - `prev` / `next` — the target's current object in bundle shape (null
        when new) and the bundle's object; `differs` and
        `metadata_only` (only descriptions, display names or tags differ)
        compare them. A skipped object that differs is **not** updated.
        `prev` omits the sensitive columns the caller may not see and the
        columns a column policy hides from it, while `differs` still compares
        the full objects — so it can be `true` over a column you cannot see.
      - pipelines also carry `armable`, `arm_problems[]` and `reused_name`.
        A column a pipeline reads that the target's system table lacks shows
        up here, as `armable: false`, not as a system-table failure; so does
        a `dial_app_route` pipeline whose `(model, route)` pair the target's
        `app-routes` setting does not allow, and an `llm` declaration whose
        response schema does not compose.
    Each entry under `required_system_tables` is `{name, import_action,
    problems[]}`: `SKIP` when the target has it as an ACTIVE system table,
    `FAIL` otherwise.
    The top level carries `env_specific[]`: values that usually differ between
    environments (`transform.model`, `advanced.rate_rpm`, cron triggers,
    absolute date literals in filters, table `ttl`). **Nothing checks `transform.model`
    against the target** — review this list before arming.
"""


async def adas_preview_catalog_import(
    ctx: Context,
    bundle: dict[str, Any],
    resolution_policy: str,
    acknowledge_reused_names: bool = False,
) -> dict[str, Any]:
    """Validate a catalog bundle against this environment and report what an import would do.

    **Writes nothing.** Runs the import's own validation pass — the real create
    services inside a rolled-back transaction, then the checks enabling each
    pipeline would run — so a clean preview means `adas_import_catalog` with
    the same arguments will pass validation. Always run it before importing.

    Unlike the import, it answers **200 whenever the bundle parses**, problems
    included: read `import_action`, `problems` and `validation_errors` (bundle
    level: limits, a name listed twice) rather than relying on an error.
    {args}
    Returns: `{required_system_tables: [...], tables: [...], pipelines: [...],
      env_specific: [...], validation_errors: [...]}`.
      Errors are about the request itself: `invalid_argument` 400 (malformed
      bundle, unknown field — a bundle carrying `access` included — or a
      missing `resolution_policy`),
      422 `catalog_bundle_version_unsupported` (a bundle from a newer ADAS) or
      `resolution_policy_unsupported`, `permission_denied` 403, and
      `http_error` 413 `payload_too_large` (over `CATALOG_IMPORT_MAX_FILE_SIZE`,
      20 MB by default).

    Related: adas_export_catalog, adas_import_catalog.

    Examples:
      bundle={...}, resolution_policy="SKIP_IF_EXISTS"
      bundle={...}, resolution_policy="FAIL_IF_EXISTS"
    """

    request, error = _import_request(
        bundle, resolution_policy, acknowledge_reused_names
    )
    if error:
        return error
    files, params = request
    try:
        return await get_client().request(
            "POST",
            "/v1/catalog/import/preview",
            files=files,
            params=params,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_import_catalog(
    ctx: Context,
    bundle: dict[str, Any],
    resolution_policy: str,
    acknowledge_reused_names: bool = False,
) -> dict[str, Any]:
    """Import a catalog bundle: create its tables and pipelines on this environment, without grants.

    **Writes.** Runs the same validation pass as `adas_preview_catalog_import`
    and, only if it finds no problem, creates each object in dependency order
    as its own step: source tables, then enrichment tables, then `enrich`
    pipelines, then `aggregate` ones. **Every table is created with no grants**
    and **every pipeline disabled**, whatever `exported_enabled` says. Preview
    first, with the same arguments.

    If an object fails part-way, the import deletes again what it created, in
    reverse order. That is best effort: `outcome` says whether it worked, and
    re-running the bundle with `SKIP_IF_EXISTS` once the cause is fixed
    converges.
    {args}
    After a successful import (ADAS's `docs/catalog-promotion.md`):
      1. review `env_specific` — a model must exist on the target;
      2. grant access on each imported source table with this environment's
         role names (`PUT /v1/tables/{name}/access?role={role}`, not wrapped
         here) — until then only callers holding the global privileges can see
         the tables. An enrichment follows its source's read access; grant it
         only `write`/`modify`. Allow each `dial_app_route` pipeline's pair
         with `adas_replace_setting` (`name="app-routes"`) before arming it;
      3. arm enrichment-producing pipelines before the aggregates that read
         them: `adas_preview_pipeline`, then `adas_patch_pipeline` with
         `{"enabled": true}`;
      4. expect aggregates to rebuild from zero (about one minute per 100k
         filter-matching rows), and run any data-sync backfill separately —
         runner state does not travel.

    Returns: the entries above plus `import_id`, `outcome` (`completed`,
      `rolled_back`, `rollback_failed`) and per entry a `status` (`created`,
      `skipped`, `rolled_back`, `rollback_failed`, `failed`). A mid-apply
      failure is still a 200 — read `outcome`.
      Errors (nothing written): `conflict` 409 `catalog_import_conflict` (an
      object exists under `FAIL_IF_EXISTS`); `invalid_argument` 422
      `catalog_import_invalid` (the validation pass found a problem — the
      message is a summary, call `adas_preview_catalog_import` for the
      details), 422 `catalog_bundle_version_unsupported` or
      `resolution_policy_unsupported`, or 400 for a malformed request;
      `permission_denied` 403; `http_error` 413 `payload_too_large`.

    Related: adas_preview_catalog_import, adas_export_catalog,
      adas_patch_pipeline, adas_preview_pipeline.

    Examples:
      bundle={...}, resolution_policy="SKIP_IF_EXISTS"
      bundle={...}, resolution_policy="FAIL_IF_EXISTS"
      bundle={...}, resolution_policy="SKIP_IF_EXISTS", acknowledge_reused_names=True
    """

    request, error = _import_request(
        bundle, resolution_policy, acknowledge_reused_names
    )
    if error:
        return error
    files, params = request
    try:
        return await get_client().request(
            "POST",
            "/v1/catalog/import",
            files=files,
            params=params,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


# The two import docstrings share their argument and entry description.
# Docstrings are dedented at compile time from Python 3.13 on and not before, so
# both sides are normalized with cleandoc before the section is spliced in.
for _tool in (adas_preview_catalog_import, adas_import_catalog):
    _tool.__doc__ = inspect.cleandoc(_tool.__doc__).replace(
        "{args}", "\n" + inspect.cleandoc(_IMPORT_ARGS_DOC) + "\n"
    )


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_preview_catalog_export")(adas_preview_catalog_export)
    mcp.tool(name="adas_export_catalog")(adas_export_catalog)
    mcp.tool(name="adas_preview_catalog_import")(adas_preview_catalog_import)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_import_catalog")(adas_import_catalog)
