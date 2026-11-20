"""Instance settings (`SettingsController`).

Per-instance configuration an administrator writes once and every analyst
reads, one document per setting name. The set of names is closed:
  - `app-routes` — the allowlist of `(deployment, route)` pairs a
    `dial_app_route` enrichment pipeline may call. An absent setting (or an
    empty list) allows no route.
  - `sessions-source` — which catalog source table holds session-level rows,
    and which of its flat fields play the session roles a sessions view needs.

Reads need `SETTINGS_READ` and are not filtered by table privileges; writes
need `SETTINGS_WRITE`. The write registers behind `ADAS_CAPABILITIES=full`.
Settings are instance configuration: they do not travel in a catalog bundle.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client, path_segment
from ..errors import MissingAuthError, invalid_argument, structured_error

_SETTING_NAMES = ("app-routes", "sessions-source")

# Response-only members ADAS stamps on every write and refuses (400) in a
# request body. Dropped before sending so a setting read back can be edited and
# replaced as is.
_SERVER_ASSIGNED = ("updated_at", "updated_by")


def _setting_name(name: str) -> tuple[str, dict[str, Any] | None]:
    normalized = (name or "").strip().lower()
    if normalized not in _SETTING_NAMES:
        return name, invalid_argument(
            f"name must be one of {', '.join(_SETTING_NAMES)}; got {name!r}"
        )
    return normalized, None


async def adas_get_setting(ctx: Context, name: str) -> dict[str, Any]:
    """Read one instance setting, exactly as last written.

    `name` is one of:
      - `app-routes` — `{routes: [{deployment, route}], updated_at,
        updated_by}`: the DIAL application routes an `enrich` pipeline with
        `transform.preset: "dial_app_route"` may call, in the order written.
        Check it before declaring a route pipeline: arming one whose
        `(model, route)` pair is not listed is refused.
      - `sessions-source` — `{base_table, roles: {session_id, time, project,
        title, cost, turns}, updated_at, updated_by}`: the source table holding
        session-level rows and the flat field bound to each role (an unset
        role reads `null`).
    `updated_by` is the last writer's principal, `null` when security is off.
    A setting is **not revalidated on read**: a later catalog change (a
    dropped column, a retired table) leaves it as written.

    Returns: the stored setting. `{error: {code: "not_found"}}` when the
      setting has never been written (ADAS's `setting_not_found`) — for
      `app-routes` that means no route is allowed. `invalid_argument` for any
      other `name` (checked here).

    Related: adas_replace_setting, adas_create_pipeline, adas_describe_entity.

    Examples:
      name="app-routes"
      name="sessions-source"
    """

    name, rejection = _setting_name(name)
    if rejection is not None:
        return rejection
    return await get_client().request(
        "GET",
        f"/v1/settings/{path_segment(name)}",
        ctx=ctx,
        require_auth=False,
    )


async def adas_replace_setting(
    ctx: Context, name: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Create or replace one instance setting, whole.

    A **whole-document replace** with last-writer-wins; there is no partial
    update, so to add one route read the setting, append, and send the full
    list back. `updated_at`/`updated_by` are stamped by ADAS: this tool drops
    them from `body` before sending (ADAS rejects them with 400), so a read-back
    document can be edited and replaced as is. Any other unknown member is a
    400 naming it. Rewriting the same value still succeeds and refreshes the
    stamps.

    `name="app-routes"` — `body={"routes": [{"deployment", "route"}]}`
    (`AppRoutesSettingRequestDto`). `routes` is required; an empty list is
    valid and allows no route. At most 100 pairs, no duplicates (compared
    exactly and case-sensitively). `deployment` is a DIAL application id —
    non-blank, ≤ 256 characters, `/` allowed but not leading/trailing or as an
    empty/`.`/`..` segment, and no whitespace, `\\`, `?`, `#` or `%`. `route`
    follows the `transform.route` grammar (1–256 characters of
    `/`-separated `[A-Za-z0-9._~-]` segments). ADAS does not check pairs
    against DIAL. **Dropping a pair an enabled pipeline calls is refused**
    with 409 `app_route_in_use` naming the pipelines — disable them first; a
    disabled pipeline does not block it (but can no longer be armed).

    `name="sessions-source"` — `body={"base_table", "roles": {"session_id",
    "time", "project"?, "title"?, "cost"?, "turns"?}}`
    (`SessionsSourceSettingRequestDto`). `base_table` is an `ACTIVE` source
    table; each role is a flat field name as `adas_describe_entity` reports
    it (`<enrichment>.<column>` for an enrichment column). Type fit:
    `session_id`/`project`/`title` string, `time` timestamp, `cost`
    decimal/integer/long, `turns` integer/long. A bound field may not be
    `array`, projection-only or `sensitive`. Validated against the catalog as
    the writing caller may see it.

    Returns: the stored setting with the new `updated_at` and `updated_by`.
      `{error: {code: "invalid_argument"}}` for an unknown `name` or an empty
      `body` (checked here), a 400 for an unknown member, or a 422
      `validation_error` naming the offending member (`routes[i]`,
      `base_table`, `roles.<role>`), storing nothing; `{error: {code:
      "conflict", status: 409}}` with `details.error: "app_route_in_use"`.

    Related: adas_get_setting, adas_patch_pipeline.

    Examples:
      name="app-routes", body={"routes": [{"deployment": "insights-app", "route": "insights"}, {"deployment": "triage-app", "route": "v1/classify"}]}
      name="sessions-source", body={"base_table": "sessions_rollup", "roles": {"session_id": "session_id", "time": "started_at", "cost": "total_cost"}}
    """

    name, rejection = _setting_name(name)
    if rejection is not None:
        return rejection
    if not isinstance(body, dict) or not body:
        return invalid_argument("body must be a non-empty object")
    payload = {k: v for k, v in body.items() if k not in _SERVER_ASSIGNED}
    try:
        return await get_client().request(
            "PUT",
            f"/v1/settings/{path_segment(name)}",
            json=payload,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_get_setting")(adas_get_setting)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_replace_setting")(adas_replace_setting)
