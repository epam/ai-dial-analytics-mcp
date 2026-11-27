"""Dashboards (`DashboardController`, `/v1/dashboards`).

A dashboard is widgets on a 12-column grid, each running one query, under one
dashboard-wide time intent and period comparison. It stores **authored intent
only** — layout, each widget's query reference or inline body, and how it
renders — and no query result: a client runs every widget through the query
surface under the viewer's own credential, so table grants, sensitive columns,
column policies and row policies apply per viewer. There is deliberately no
execute endpoint.

The list and single read are read-only and always register; create, replace
and delete register behind `ADAS_CAPABILITIES=full`. Authorization reuses the
saved-query privileges and is ADAS's own: reads need `SAVED_QUERY_READ`, a
`private` write `SAVED_QUERY_WRITE`, a `public` write and every visibility
change `SAVED_QUERY_PUBLISH`.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client, path_segment
from ..errors import MissingAuthError, invalid_argument, structured_error

# Members ADAS assigns itself and rejects (422 `validation_error`) in a create
# or replace body. They are dropped before sending, so the document a read
# returned can be edited and sent straight back. `generation` is not among
# them: a replace requires it, and only a create drops it.
_SERVER_ASSIGNED = (
    "id",
    "owner_id",
    "owner_email",
    "created_at",
    "updated_at",
    "permissions",
)


def _request_body(body: dict[str, Any], *, keep_generation: bool) -> dict[str, Any]:
    dropped = _SERVER_ASSIGNED if keep_generation else (*_SERVER_ASSIGNED, "generation")
    return {k: v for k, v in body.items() if k not in dropped}


async def adas_list_dashboards(ctx: Context) -> dict[str, Any]:
    """List every dashboard the caller may see, most recently updated first.

    That is the caller's own `private` dashboards and every `public` one — a
    `private` dashboard is visible to its owner alone, whatever privileges
    another caller holds. Items carry no widgets: read one with
    `adas_get_dashboard` for its full document. Not paginated.

    Returns: `{dashboards: [{id, name, description?, visibility, updated_at,
      permissions: {write, publish}}], permissions: {create_private,
      create_public}}` — an envelope, **not** `items`. Each item's
      `permissions` says what **this caller** may do: `write` — replace and
      delete it at its current visibility; `publish` — change its visibility.
      The top-level `permissions` says which visibility the caller may create.
      The flags report authorization only and never predict body validation.
      In `SECURITY_MODE=none` every flag is `true`.

    Related: adas_get_dashboard, adas_create_dashboard, adas_list_saved_queries.

    Examples:
      (no arguments)
    """

    return await get_client().request(
        "GET", "/v1/dashboards", ctx=ctx, require_auth=False
    )


async def adas_get_dashboard(ctx: Context, dashboard_id: str) -> dict[str, Any]:
    """Get one dashboard's full document, widgets and inline bodies included.

    The document is exactly what was stored. A widget's `saved_query_id` is
    **not** dereferenced: resolve it with `adas_get_saved_query`, which answers
    `not_found` for a saved query that was deleted or the caller may not read.
    Nothing is executed — to see a widget's data, run its `sql` (or the saved
    query's) through `adas_execute_sql` with the widget's `row_limit`.

    Returns: `DashboardDto` `{id, name, description?, visibility, owner_id?,
      owner_email?, time?, compare?, settings?, widgets: [{id, saved_query_id
      | inline_query: {name, query | sql, result_view, chart?}, title?,
      description?, row_limit, view?, value_column?, change_direction?,
      settings?, layout: {x, y, w, h}}], generation, created_at, updated_at,
      permissions: {write, publish}}`; absent optional members are omitted
      rather than `null`. `generation` is the concurrency token
      `adas_replace_dashboard` needs back. `{error: {code: "not_found"}}` for
      an unknown id or one the caller may not see — indistinguishable by
      design.

    Related: adas_list_dashboards, adas_replace_dashboard, adas_get_saved_query.

    Examples:
      dashboard_id="dash_00000000000000000000000001"
    """

    if not dashboard_id:
        return invalid_argument("dashboard_id is required")
    return await get_client().request(
        "GET",
        f"/v1/dashboards/{path_segment(dashboard_id)}",
        ctx=ctx,
        require_auth=False,
    )


_BODY_DOC = """
    The body (`DashboardRequestDto`; call `adas_describe_schema` with
    `type_name="DashboardRequestDto"`, `"WidgetRequestDto"`,
    `"InlineQueryRequestDto"` or `"LayoutRequestDto"` for the authoritative
    field lists — ADAS reads it strictly and answers 400 `bad_request` naming
    any unrecognized member):
      - `name` — required, non-blank, at most 1000 characters;
        `description` — optional, same bound.
      - `visibility` — required: `private` (owner only) or `public` (every
        holder of `SAVED_QUERY_READ`, inline bodies shown unredacted).
      - `time` — optional dashboard-wide time intent, in the saved-query model:
        `{"mode": "relative", "period": "last_7d"}` or
        `{"mode": "absolute", "from": "<instant>", "to": "<instant>"}`.
        Stored as intent, never resolved to instants.
      - `compare` — optional: `off`, `previous-period`, `previous-month`,
        `previous-year` (hyphenated; absent means off).
      - `settings` — optional, any JSON **object** the client keeps (≤ 64 KiB),
        stored and returned verbatim, never interpreted.
      - `widgets` — **required**, ordered, possibly `[]`, at most 100, at most
        1 MiB serialized. Each widget:
          - `id` — client-assigned, non-blank, ≤ 64 characters, unique within
            the dashboard (e.g. `"w1"`).
          - **exactly one** source: `saved_query_id` (stored loosely — never
            checked to exist) **or** `inline_query` `{name, sql | query,
            result_view?, chart?}` — `name` required, exactly one body (`sql` in
            the ADAS SQL dialect, or a structured-query DSL `query`),
            `result_view` `table` (default) or `chart`, `chart` `{type,
            x_field, y_field, legend}` (the saved-query chart model:
            `y_field` is an array of series, a legacy string read back as a
            one-element array; `legend` is `{visible, position}`). An inline query carries **no** `time`, `scope`,
            `tag`, owner or `generation` (400 naming the member): copying a
            saved query in, copy only those four members. The copy is a
            snapshot and does not follow the saved query afterwards.
          - `row_limit` — **required**, a JSON integer 1–1000 (`2.0` and `"5"`
            are refused, not coerced).
          - `layout` — **required** `{x, y, w, h}`, JSON integers on a 12-column
            grid: `0 <= x <= 11`, `y >= 0`, `1 <= w <= 12`, `h >= 1`, and
            `x + w <= 12`. Overlap is allowed.
          - optional: `title`, `description`, `value_column` (≤ 1000
            characters each); `view` — `query` (the query's own), `table`,
            `single_value`, `time_series`, `share`, `heatmap`;
            `change_direction` — `higher_is_better` / `lower_is_better`;
            `settings` — a JSON object as above. `view` and `value_column`
            are stored as given, never checked against the query.

    **Every inline body is validated on every write**, without executing it,
    by the same translation the query surface performs and against the
    **caller's** view of the catalog — check SQL bodies with
    `adas_validate_sql` first. A rejected body answers with the query
    surface's status (usually 400) and its message prefixed `widget <id>: `.
    An enum member naming no token (`view: "pie"`, `visibility: 1`) is 400
    `bad_request`; every other rule above is 422 `validation_error`.

    The server-assigned members (`id`, `owner_id`, `owner_email`,
    `created_at`, `updated_at`) and the response-only `permissions` are
    rejected by ADAS; this tool **drops them before sending**, so a dashboard
    read back can be edited and sent as is.
"""


async def adas_create_dashboard(ctx: Context, body: dict[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(body, dict) or not body:
            return invalid_argument("body must be a non-empty object")
        return await get_client().request(
            "POST",
            "/v1/dashboards",
            json=_request_body(body, keep_generation=False),
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


adas_create_dashboard.__doc__ = (
    """Create a dashboard of query widgets.

    A `private` dashboard needs `SAVED_QUERY_WRITE`, a `public` one
    `SAVED_QUERY_PUBLISH` — `adas_list_dashboards` reports which the caller may
    create. `generation` is dropped from the body here too, so an existing
    dashboard read with `adas_get_dashboard` can be sent back as a copy.
"""
    + _BODY_DOC
    + """
    Returns: the created `DashboardDto` (201) with its `id` (prefixed
      `dash_`), `owner_id`, `generation`, timestamps and `permissions`; an
      inline query's absent `result_view` is filled in as `table`.
      `{error: {code: "invalid_argument"}}` for a validation failure (status
      422 or 400, see above); `permission_denied` for a visibility the caller
      may not create; `internal_error` (500 `principal_unavailable`) for a
      `private` create whose caller identity does not resolve.

    Related: adas_list_dashboards, adas_replace_dashboard, adas_create_saved_query,
      adas_validate_sql.

    Examples:
      body={"name": "Usage overview", "visibility": "private", "time": {"mode": "relative", "period": "last_7d"}, "compare": "previous-period", "widgets": [{"id": "w1", "title": "Calls", "row_limit": 1, "view": "single_value", "value_column": "calls", "change_direction": "higher_is_better", "layout": {"x": 0, "y": 0, "w": 4, "h": 3}, "inline_query": {"name": "Calls", "sql": "SELECT count(*) AS calls FROM dial_usage_log"}}, {"id": "w2", "title": "Calls per deployment", "row_limit": 20, "view": "table", "layout": {"x": 4, "y": 0, "w": 8, "h": 6}, "inline_query": {"name": "Calls per deployment", "sql": "SELECT deployment, count(*) AS calls FROM dial_usage_log GROUP BY deployment ORDER BY calls DESC"}}]}
      body={"name": "Shared costs", "visibility": "public", "widgets": [{"id": "w1", "saved_query_id": "sq_00000000000000000000000001", "row_limit": 100, "layout": {"x": 0, "y": 0, "w": 12, "h": 6}}]}
      body={"name": "Empty draft", "visibility": "private", "widgets": []}
    """
)


async def adas_replace_dashboard(
    ctx: Context, dashboard_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    try:
        if not dashboard_id:
            return invalid_argument("dashboard_id is required")
        if not isinstance(body, dict) or not body:
            return invalid_argument("body must be a non-empty object")
        if body.get("generation") is None:
            return invalid_argument(
                "body.generation is required: send the generation the dashboard "
                "was last read at (adas_get_dashboard returns it)"
            )
        return await get_client().request(
            "PUT",
            f"/v1/dashboards/{path_segment(dashboard_id)}",
            json=_request_body(body, keep_generation=True),
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


adas_replace_dashboard.__doc__ = (
    """Replace a dashboard wholesale, guarded by its `generation`.

    A **whole-document replace**: a member you omit is cleared (`settings`
    included), so read it with `adas_get_dashboard`, edit, and send the whole
    document back. The body **must carry the `generation` you read** (checked
    here, as ADAS answers 422 without it); a stale one is 409
    `dashboard_conflict` and changes nothing — re-read and re-apply the edit,
    do not retry blindly. `id`, owner and `created_at` are preserved;
    `generation` is bumped. Changing `visibility` in either direction needs
    `SAVED_QUERY_PUBLISH` (the dashboard's `permissions.publish`).

    Every inline body is **revalidated** against the caller's view, even when
    only `name` changed: a widget reading a since-deleted table blocks every
    replace that keeps it, although `permissions.write` reads `true`.
"""
    + _BODY_DOC
    + """
    Returns: the updated `DashboardDto`. `{error: {code: "conflict", status:
      409}}` for a stale `generation` (`details.error` is
      `dashboard_conflict`); `not_found` for an unknown id or one the caller
      may not see; `permission_denied` for a dashboard the caller sees but may
      not write; otherwise the same validation errors as
      `adas_create_dashboard`.

    Related: adas_get_dashboard, adas_delete_dashboard.

    Examples:
      dashboard_id="dash_00000000000000000000000001", body={"generation": 3, "name": "Usage overview", "visibility": "public", "widgets": [{"id": "w1", "row_limit": 1, "view": "single_value", "value_column": "calls", "layout": {"x": 0, "y": 0, "w": 4, "h": 3}, "inline_query": {"name": "Calls", "sql": "SELECT count(*) AS calls FROM dial_usage_log"}}]}
    """
)


async def adas_delete_dashboard(ctx: Context, dashboard_id: str) -> dict[str, Any]:
    """Delete a dashboard. Saved queries its widgets reference are untouched.

    Carries no `generation`, but a delete and a replace racing on the same
    dashboard never both succeed: the loser answers 409 `dashboard_conflict`
    (or 404 when the delete committed first). A `private` dashboard needs `SAVED_QUERY_WRITE` and must be
    the caller's own; a `public` one needs `SAVED_QUERY_PUBLISH`.

    Returns: `{ok: true}` (204). `{error: {code: "not_found"}}` for an unknown
      id or one the caller may not see; `permission_denied` for one the caller
      sees but may not write.

    Related: adas_list_dashboards, adas_get_dashboard.

    Examples:
      dashboard_id="dash_00000000000000000000000001"
    """

    if not dashboard_id:
        return invalid_argument("dashboard_id is required")
    try:
        return await get_client().request(
            "DELETE",
            f"/v1/dashboards/{path_segment(dashboard_id)}",
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_list_dashboards")(adas_list_dashboards)
    mcp.tool(name="adas_get_dashboard")(adas_get_dashboard)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_create_dashboard")(adas_create_dashboard)
    mcp.tool(name="adas_replace_dashboard")(adas_replace_dashboard)
    mcp.tool(name="adas_delete_dashboard")(adas_delete_dashboard)
