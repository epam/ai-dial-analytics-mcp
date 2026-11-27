"""Saved queries (`SavedQueryController`, `/v1/saved-queries`).

A saved query is named, reusable Query Builder state: a structured `query` or a
`sql` body plus the author's time intent and how they last rendered it. It
stores no run outcome and does not execute itself — a client runs the stored
body through the query surface. The list and single read are read-only and
always register; create, replace and delete register behind
`ADAS_CAPABILITIES=full`.

Authorization is by **scope**, not by URL, and is ADAS's own: `personal` rows
are visible to their owner alone, `common` rows to every holder of
`SAVED_QUERY_READ`, and writing `common` needs `SAVED_QUERY_PUBLISH`. A saved
query the caller may not see answers 404, never 403.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client, path_segment
from ..errors import MissingAuthError, invalid_argument, structured_error

_SCOPES = ("personal", "common")

# Members ADAS assigns itself and rejects (422 `validation_error`) in a create
# or replace body. They are dropped before sending, so the object a read
# returned can be edited and sent straight back.
_SERVER_ASSIGNED = (
    "id",
    "owner_id",
    "owner_email",
    "source",
    "generation",
    "created_at",
    "updated_at",
)


def _request_body(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _SERVER_ASSIGNED}


async def adas_list_saved_queries(
    ctx: Context, scope: str | None = None
) -> dict[str, Any]:
    """List the saved queries the caller may see at one scope.

    Each entry is the **full** saved query — body, `time` and `chart`
    included — byte-for-byte what `adas_get_saved_query` returns for it, most
    recently updated first. The list is not paginated.

    Args:
      scope: `personal` (the default — the caller's own saved queries) or
        `common` (the shared ones). Case-insensitive.

    Returns: `{saved_queries: [SavedQueryDto]}` — an envelope, **not**
      `items`. Each entry is `{id, name, description?, tag?, scope, owner_id?,
      owner_email?, source, query | sql, time?, result_view, chart?,
      generation, created_at, updated_at}`; null members are omitted. `source`
      is every entity the body reads, derived by ADAS and sorted. `owner_email`
      is for display only and often absent. `{error: {code:
      "invalid_argument"}}` for any other `scope` (checked here; ADAS answers
      422 `validation_error`). Under security, an authenticated caller whose
      principal does not resolve owns nothing and sees an empty `personal`
      list.

    Related: adas_get_saved_query, adas_create_saved_query, adas_list_dashboards.

    Examples:
      (no arguments — the caller's personal saved queries)
      scope="common"
    """

    if scope is not None:
        normalized = scope.strip().lower()
        if normalized not in _SCOPES:
            return invalid_argument(
                f"scope must be one of {', '.join(_SCOPES)}; got {scope!r}"
            )
        scope = normalized
    return await get_client().request(
        "GET",
        "/v1/saved-queries",
        params={"scope": scope},
        ctx=ctx,
        require_auth=False,
    )


async def adas_get_saved_query(ctx: Context, saved_query_id: str) -> dict[str, Any]:
    """Get one saved query, body included, exactly as persisted.

    This is how a dashboard widget's `saved_query_id` is resolved: the
    dashboard stores only the reference, never the content, and a reference
    to a saved query that was deleted or that the caller may not read is
    simply `not_found` here.

    A saved query does not run itself. To run it, take its `sql` to
    `adas_execute_sql` (check it with `adas_validate_sql` first); a structured
    `query` body is the DSL `POST /v1/queries/execute` takes, which this server
    does not wrap.

    Returns: `SavedQueryDto` `{id, name, description?, tag?, scope, owner_id?,
      owner_email?, source, query | sql, time?, result_view, chart?,
      generation, created_at, updated_at}`. `time` is the author's time-picker
      intent (`{mode: "relative", period}` or `{mode: "absolute", from, to}`),
      never resolved to instants and never applied to the body by ADAS. A
      `common` row's body is returned unredacted to every reader. `{error:
      {code: "not_found"}}` for an unknown id or one the caller may not see —
      the two are indistinguishable by design.

    Related: adas_list_saved_queries, adas_replace_saved_query, adas_get_dashboard.

    Examples:
      saved_query_id="sq_00000000000000000000000001"
    """

    if not saved_query_id:
        return invalid_argument("saved_query_id is required")
    return await get_client().request(
        "GET",
        f"/v1/saved-queries/{path_segment(saved_query_id)}",
        ctx=ctx,
        require_auth=False,
    )


_BODY_DOC = """
    The body (`SavedQueryRequestDto`; call `adas_describe_schema` with
    `type_name="SavedQueryRequestDto"` for the authoritative field list —
    ADAS reads it strictly and answers 400 `bad_request` naming any
    unrecognized member):
      - `name` — required, non-blank (stored trimmed).
      - exactly one body: `sql` (an ADAS SQL `SELECT`, the dialect
        `adas_execute_sql` runs — check it with `adas_validate_sql`) **or**
        `query` (a structured-query DSL document, `type_name="StructuredQuery"`;
        `adas_preview_pipeline` returns one as `query`). Both, or neither, is a
        422. A blank `sql` counts as absent.
      - `scope` — `personal` (default) or `common`; `common` needs
        `SAVED_QUERY_PUBLISH`.
      - `description`, `tag` (a single label) — optional.
      - `time` — optional time-picker intent: `null`,
        `{"mode": "relative", "period": "last_7d"}` (a lowercase
        alphanumeric/underscore token ADAS bounds but never interprets) or
        `{"mode": "absolute", "from": "<instant>", "to": "<instant>"}` with
        `from` not after `to`. Any other shape is a 422. `time` is **not**
        applied to the body: a body that must move with the clock carries its
        own window (`now() - INTERVAL '7' DAY` in SQL).
      - `result_view` — `table` (default) or `chart`; `chart` —
        `{type, x_field, y_field, legend}`, stored as sent
        except that `y_field` is an **array** of result-column names — one
        element is a single-series chart, several are series plotted against
        the one `x_field`, in the order given. A legacy single string is
        accepted and returned as a one-element array; an object, or an array
        holding an object or an empty array, is a 400. `legend` is
        `{visible, position}`: `visible` a boolean, `position` a string the
        client knows as `above`, `right`, `below` or `left` (stored as sent,
        never checked; an unknown value renders as `below`). A `legend` that
        is neither an object nor null, whose `visible` or `position` is an
        object or an array, or that carries any other member, is a 400. A
        replace whose chart omits `legend` clears it. Members are optional and
        never checked against the catalog (they name result columns).

    The body is validated **without executing it**, by the same translation
    the query surface performs, against the **caller's** view of the catalog:
    a body naming an entity or column the caller cannot resolve is refused with
    the status and message the query surface gives (usually 400), and does not
    reveal whether the entity is absent or merely invisible.

    The server-assigned members (`id`, `owner_id`, `owner_email`, `source`,
    `generation`, `created_at`, `updated_at`) are rejected by ADAS; this tool
    **drops them before sending**, so a saved query read back can be edited and
    sent as is.
"""


async def adas_create_saved_query(ctx: Context, body: dict[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(body, dict) or not body:
            return invalid_argument("body must be a non-empty object")
        return await get_client().request(
            "POST",
            "/v1/saved-queries",
            json=_request_body(body),
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


adas_create_saved_query.__doc__ = (
    """Create a saved query — a named, reusable query body.

    Saving a query is how a dashboard widget can reference it by
    `saved_query_id` (a reference ADAS stores loosely: it does not check the
    saved query exists, and a widget can instead hold the body itself as an
    `inline_query`).
"""
    + _BODY_DOC
    + """
    Returns: the created `SavedQueryDto` (201) with its `id` (prefixed `sq_`),
      the derived `source`, `generation`, `created_at` and `updated_at`.
      `{error: {code: "invalid_argument"}}` (422 `validation_error`) for a
      blank name, two bodies or none, a malformed `time`, or a body over the
      size cap; `invalid_argument` (400 `bad_request`) for a body the
      translation rejects or an unknown member; `permission_denied` for a
      `common` write without `SAVED_QUERY_PUBLISH`.

    Related: adas_list_saved_queries, adas_validate_sql, adas_create_dashboard.

    Examples:
      body={"name": "Calls per deployment", "sql": "SELECT deployment, count(*) AS calls FROM dial_usage_log WHERE request_time >= now() - INTERVAL '7' DAY GROUP BY deployment ORDER BY calls DESC", "time": {"mode": "relative", "period": "last_7d"}}
      body={"name": "Daily cost", "scope": "common", "tag": "Cost", "sql": "SELECT date_trunc('day', request_time) AS day, sum(total_price) AS cost FROM dial_usage_log GROUP BY day ORDER BY day", "result_view": "chart", "chart": {"type": "line", "x_field": "day", "y_field": ["cost"], "legend": {"visible": True, "position": "below"}}}
    """
)


async def adas_replace_saved_query(
    ctx: Context, saved_query_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    try:
        if not saved_query_id:
            return invalid_argument("saved_query_id is required")
        if not isinstance(body, dict) or not body:
            return invalid_argument("body must be a non-empty object")
        return await get_client().request(
            "PUT",
            f"/v1/saved-queries/{path_segment(saved_query_id)}",
            json=_request_body(body),
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


adas_replace_saved_query.__doc__ = (
    """Replace a saved query wholesale.

    A **full replace**, not a merge: a member you omit is cleared. Read it with
    `adas_get_saved_query`, edit, and send the whole object back. `id`,
    `owner_id`, `owner_email` and `created_at` are preserved; `generation` is
    bumped and `updated_at` refreshed. There is no concurrency check — the last
    replace wins. Changing `scope` needs the write permission for both the
    stored and the new scope (so `SAVED_QUERY_PUBLISH` either way).

    Dashboards referencing this saved query are not touched, and a widget that
    copied it as an `inline_query` does not follow the change.
"""
    + _BODY_DOC
    + """
    Returns: the updated `SavedQueryDto`. `{error: {code: "not_found"}}` for an
      unknown id or one the caller may not see; otherwise the same errors as
      `adas_create_saved_query`.

    Related: adas_get_saved_query, adas_delete_saved_query.

    Examples:
      saved_query_id="sq_00000000000000000000000001", body={"name": "Calls per deployment (30d)", "sql": "SELECT deployment, count(*) AS calls FROM dial_usage_log WHERE request_time >= now() - INTERVAL '30' DAY GROUP BY deployment", "time": {"mode": "relative", "period": "last_30d"}}
    """
)


async def adas_delete_saved_query(ctx: Context, saved_query_id: str) -> dict[str, Any]:
    """Delete a saved query.

    Dashboards referencing it are left unchanged: their widgets keep the
    `saved_query_id`, which then resolves to `not_found` for every viewer.

    Returns: `{ok: true}` (204). `{error: {code: "not_found"}}` for an unknown
      id or one the caller may not see; `permission_denied` for a `common` one
      without `SAVED_QUERY_PUBLISH`.

    Related: adas_list_saved_queries, adas_list_dashboards.

    Examples:
      saved_query_id="sq_00000000000000000000000001"
    """

    if not saved_query_id:
        return invalid_argument("saved_query_id is required")
    try:
        return await get_client().request(
            "DELETE",
            f"/v1/saved-queries/{path_segment(saved_query_id)}",
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_list_saved_queries")(adas_list_saved_queries)
    mcp.tool(name="adas_get_saved_query")(adas_get_saved_query)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_create_saved_query")(adas_create_saved_query)
    mcp.tool(name="adas_replace_saved_query")(adas_replace_saved_query)
    mcp.tool(name="adas_delete_saved_query")(adas_delete_saved_query)
