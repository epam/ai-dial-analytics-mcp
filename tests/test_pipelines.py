from __future__ import annotations

import json

import respx

from adas_mcp.tools.pipelines import (
    adas_create_pipeline,
    adas_get_pipeline,
    adas_list_pipelines,
    adas_patch_pipeline,
    adas_preview_pipeline,
    adas_refresh_pipeline,
)


async def test_list_keeps_the_envelope():
    """Pipelines return {pipelines: [...]}, not a bare array — no items wrapping."""

    body = {"pipelines": [{"name": "usage_daily_rollup", "enabled": False}]}
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/pipelines").respond(200, json=body)
        result = await adas_list_pipelines(ctx=None)
    assert result == body
    assert "items" not in result


async def test_list_omits_absent_filters():
    """The unfiltered call is the whole registry, disabled pipelines and both
    kinds included — sending any filter at all would narrow it and hide exactly
    what this listing is for."""

    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines").respond(
            200, json={"pipelines": []}
        )
        await adas_list_pipelines(ctx=None)
    url = str(route.calls.last.request.url)
    for absent in ("target", "kind", "enabled", "updated_since", "view"):
        assert absent not in url


async def test_list_sends_filters_when_given():
    """`kind` goes on the wire as ADAS's lowercase wire code — its @InitBinder
    binds this parameter by PipelineKind.code(), not by Enum.name()."""

    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines").respond(
            200, json={"pipelines": []}
        )
        await adas_list_pipelines(
            ctx=None,
            target="usage_daily",
            kind="aggregate",
            updated_since="2026-07-01T00:00:00Z",
        )
    params = route.calls.last.request.url.params
    assert params["target"] == "usage_daily"
    assert params["kind"] == "aggregate"
    assert params["updated_since"] == "2026-07-01T00:00:00Z"


async def test_list_accepts_any_case_for_kind():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines").respond(
            200, json={"pipelines": []}
        )
        await adas_list_pipelines(ctx=None, kind="Enrich")
    assert route.calls.last.request.url.params["kind"] == "enrich"


async def test_list_sends_view_when_given():
    """`view` goes on the wire as the lowercase code ADAS's @InitBinder binds by.
    Omitted, nothing is sent and ADAS's own default (`source`) applies — the MCP
    layer does not restate a server-side default it would then have to track."""

    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines").respond(
            200, json={"pipelines": []}
        )
        await adas_list_pipelines(
            ctx=None, kind="enrich", enabled=True, view="Compiled"
        )
    assert route.calls.last.request.url.params["view"] == "compiled"


async def test_list_rejects_unknown_view():
    result = await adas_list_pipelines(ctx=None, view="resolved")
    assert result["error"]["code"] == "invalid_argument"
    assert "source" in result["error"]["message"]
    assert "compiled" in result["error"]["message"]


async def test_get_sends_view_when_given():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines/toxicity-live").respond(
            200, json={"name": "toxicity-live"}
        )
        await adas_get_pipeline(ctx=None, name="toxicity-live", view="compiled")
    assert route.calls.last.request.url.params["view"] == "compiled"


async def test_get_omits_view_when_absent():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines/toxicity-live").respond(
            200, json={"name": "toxicity-live"}
        )
        await adas_get_pipeline(ctx=None, name="toxicity-live")
    assert "view" not in str(route.calls.last.request.url)


async def test_get_rejects_unknown_view():
    result = await adas_get_pipeline(ctx=None, name="toxicity-live", view="raw")
    assert result["error"]["code"] == "invalid_argument"


async def test_list_sends_enabled_true_as_lowercase():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines").respond(
            200, json={"pipelines": []}
        )
        await adas_list_pipelines(ctx=None, kind="enrich", enabled=True)
    assert route.calls.last.request.url.params["enabled"] == "true"


async def test_list_sends_enabled_false_rather_than_dropping_it():
    # False is falsy: a param cleaner that filtered on truthiness would drop it and
    # silently answer with the whole registry instead of the disabled pipelines asked
    # for. ADAS binds `enabled` strictly, so it has to arrive lowercase, not "False".
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines").respond(
            200, json={"pipelines": []}
        )
        await adas_list_pipelines(ctx=None, enabled=False)
    assert route.calls.last.request.url.params["enabled"] == "false"


async def test_list_bad_updated_since_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/pipelines").respond(
            400, json={"message": "updated_since must be an ISO-8601 instant"}
        )
        result = await adas_list_pipelines(ctx=None, updated_since="yesterday")
    assert result["error"]["code"] == "invalid_argument"


async def test_list_rejects_unknown_kind_without_calling_backend():
    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines").respond(
            200, json={"pipelines": []}
        )
        result = await adas_list_pipelines(ctx=None, kind="nonsense")
    assert result["error"]["code"] == "invalid_argument"
    assert "aggregate" in result["error"]["message"]
    assert "enrich" in result["error"]["message"]
    # Caught locally, so an unknown kind costs no round trip.
    assert route.call_count == 0


async def test_get_passes_through():
    body = {"name": "p", "enabled": True, "state": {"lag_seconds": 42}}
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/pipelines/p").respond(200, json=body)
        result = await adas_get_pipeline(ctx=None, name="p")
    assert result == body


async def test_get_requires_name():
    result = await adas_get_pipeline(ctx=None, name="")
    assert result["error"]["code"] == "invalid_argument"


async def test_unknown_pipeline_maps_not_found():
    with respx.mock(assert_all_called=False) as router:
        router.get("http://adas.test/v1/pipelines/nope").respond(
            404, json={"message": "unknown pipeline"}
        )
        result = await adas_get_pipeline(ctx=None, name="nope")
    assert result["error"]["code"] == "not_found"


async def test_create_posts_body():
    body = {
        "name": "usage_daily_rollup",
        "kind": "aggregate",
        "target": "usage_daily",
        "inputs": ["dial_usage_log"],
        "group_by": [{"column": "deployment"}],
        "measures": [{"name": "call_count", "fn": "count"}],
        "trigger": {"kind": "schedule", "cron": "0 */10 * * * *"},
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/pipelines").respond(
            201, json={"name": "usage_daily_rollup", "enabled": False}
        )
        result = await adas_create_pipeline(ctx=None, body=body)
    assert json.loads(route.calls.last.request.content) == body
    assert result["enabled"] is False


async def test_create_rejects_empty_body():
    assert (await adas_create_pipeline(ctx=None, body={}))["error"][
        "code"
    ] == "invalid_argument"


async def test_preview_posts_with_no_body():
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/pipelines/p/preview").respond(
            200,
            json={
                "statement": "SELECT ... SETTINGS final = 1",
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "notes": [],
            },
        )
        result = await adas_preview_pipeline(ctx=None, name="p")
    assert not route.calls.last.request.content
    assert result["statement"] == "SELECT ... SETTINGS final = 1"


async def test_preview_requires_name():
    result = await adas_preview_pipeline(ctx=None, name="")
    assert result["error"]["code"] == "invalid_argument"


async def test_patch_uses_patch_verb():
    with respx.mock(assert_all_called=False) as router:
        route = router.patch("http://adas.test/v1/pipelines/p").respond(
            200, json={"name": "p", "enabled": True}
        )
        result = await adas_patch_pipeline(ctx=None, name="p", body={"enabled": True})
    assert route.calls.last.request.method == "PATCH"
    assert json.loads(route.calls.last.request.content) == {"enabled": True}
    assert result["enabled"] is True


async def test_patch_requires_name_and_body():
    assert (await adas_patch_pipeline(ctx=None, name="", body={"enabled": True}))[
        "error"
    ]["code"] == "invalid_argument"
    assert (await adas_patch_pipeline(ctx=None, name="p", body={}))["error"][
        "code"
    ] == "invalid_argument"


async def test_incomplete_declaration_patch_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/pipelines/p").respond(
            422,
            json={
                "error": "pipeline_validation_failed",
                "message": "a declaration patch that leaves the pipeline enabled "
                "must supply inputs, measures, and trigger together",
            },
        )
        result = await adas_patch_pipeline(
            ctx=None, name="p", body={"measures": [], "enabled": True}
        )
    # ADAS uses 422 for declaration failures; it maps to invalid_argument like 400,
    # with the original status preserved.
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_refresh_returns_progress():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines/p/refresh").respond(
            200, json={"rows_written": 128, "has_more": False, "lag_seconds": 5}
        )
        result = await adas_refresh_pipeline(ctx=None, name="p")
    assert result == {"rows_written": 128, "has_more": False, "lag_seconds": 5}


async def test_refresh_disabled_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines/p/refresh").respond(
            409, json={"message": "pipeline is disabled"}
        )
        result = await adas_refresh_pipeline(ctx=None, name="p")
    assert result["error"]["code"] == "conflict"


async def test_refresh_requires_name():
    result = await adas_refresh_pipeline(ctx=None, name="")
    assert result["error"]["code"] == "invalid_argument"


# --- the enrich kind -----------------------------------------------------------


async def test_create_enrich_body_passes_through_unchanged():
    """An enrich declaration is the same POST /v1/pipelines, discriminated by
    `kind`. Both nested blocks, `trigger` and `transform`, travel verbatim
    rather than being flattened into top-level members."""

    body = {
        "name": "conversation-insights-live",
        "kind": "enrich",
        "target": "conversation_insights",
        "inputs": ["dial_usage_log"],
        "trigger": {
            "kind": "group",
            "group_by": "chat_id",
            "ready_when": {"idle": "30m"},
            "member_select": {"prefer_sql": "parent_deployment IS NULL", "limit": 1},
        },
        "transform": {
            "type": "llm",
            "model": "gemini-2.5-flash-lite",
            "request_template": '{"messages": [{"role": "user", "content": "{{members}}"}]}',
            "inputs": {"transcript": {"jsonata": "request_body.messages"}},
            "outputs": {"title": "A short title for the conversation."},
        },
        "advanced": {"rows_per_scan": 1000},
        "enabled": False,
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/pipelines").respond(
            201, json={"name": "conversation-insights-live", "enabled": False}
        )
        result = await adas_create_pipeline(ctx=None, body=body)
    assert json.loads(route.calls.last.request.content) == body
    assert result["name"] == "conversation-insights-live"


async def test_create_cross_kind_field_maps_invalid_argument():
    """A member belonging to the other kind is a 422 naming it, not an ignore."""

    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines").respond(
            422,
            json={
                "message": "measures belongs to the 'aggregate' kind and cannot be "
                "declared on a 'enrich' pipeline"
            },
        )
        result = await adas_create_pipeline(
            ctx=None,
            body={
                "name": "toxicity-live",
                "kind": "enrich",
                "target": "usage_toxicity",
                "measures": [{"name": "n", "fn": "count"}],
                "enabled": True,
            },
        )
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_create_duplicate_target_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines").respond(
            409,
            json={
                "message": "target 'usage_toxicity' is already written by pipeline "
                "'toxicity-live' (one pipeline per target)"
            },
        )
        result = await adas_create_pipeline(
            ctx=None, body={"name": "x", "kind": "enrich", "target": "usage_toxicity"}
        )
    assert result["error"]["code"] == "conflict"


async def test_enrich_names_may_be_kebab_case():
    """A pipeline name is an identity, not a SQL identifier, so hyphens survive
    escaping and reach the path unchanged."""

    with respx.mock(assert_all_called=False) as router:
        route = router.get("http://adas.test/v1/pipelines/toxicity-live").respond(
            200, json={"name": "toxicity-live", "kind": "enrich"}
        )
        result = await adas_get_pipeline(ctx=None, name="toxicity-live")
    assert str(route.calls.last.request.url).endswith("/v1/pipelines/toxicity-live")
    assert result["kind"] == "enrich"


async def test_preview_serves_the_enrich_kind():
    """Both kinds answer in one flat shape: an `llm` preview has no `statement`
    and no `total_rows`, and nothing is wrapped under an `enrich` member."""

    body = {
        "columns": [
            {
                "name": "request_body",
                "role": "input",
                "type": "object",
                "max_bytes": 86000,
            }
        ],
        "rows": [{"request_body": '{"messages":[…'}],
        "notes": [
            {
                "code": "values_truncated",
                "severity": "info",
                "subject": "request_body",
                "message": "1 sampled value(s) of column 'request_body' exceeded ...",
            }
        ],
    }
    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines/toxicity-live/preview").respond(
            200, json=body
        )
        result = await adas_preview_pipeline(ctx=None, name="toxicity-live")
    assert result == body
    assert "statement" not in result
    assert "enrich" not in result


async def test_preview_sends_the_positioning_body():
    """The optional body positions the sample. It is forwarded verbatim: `from`
    is a Python keyword, so the members travel in a dict rather than as named
    parameters and keep ADAS's own spelling."""

    body = {"group_key": "chat-42"}
    with respx.mock(assert_all_called=False) as router:
        route = router.post(
            "http://adas.test/v1/pipelines/conversation-insights-live/preview"
        ).respond(
            200, json={"columns": [], "rows": [], "group_key": "chat-42", "notes": []}
        )
        await adas_preview_pipeline(
            ctx=None, name="conversation-insights-live", body=body
        )
    assert json.loads(route.calls.last.request.content) == body


async def test_preview_sends_no_body_for_an_empty_one():
    """An empty dict is not a body: the aggregate arm refuses ANY body with 422,
    so `{}` must reach ADAS as the absent body it means."""

    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://adas.test/v1/pipelines/p/preview").respond(
            200,
            json={"statement": "SELECT ...", "columns": [], "rows": [], "notes": []},
        )
        await adas_preview_pipeline(ctx=None, name="p", body={})
    assert not route.calls.last.request.content


async def test_preview_wrong_grain_member_maps_invalid_argument():
    """A member belonging to the other grain is refused, not ignored."""

    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines/toxicity-live/preview").respond(
            422,
            json={
                "message": "group_key applies to a 'group'-triggered pipeline; "
                "'toxicity-live' fires at row grain"
            },
        )
        result = await adas_preview_pipeline(
            ctx=None, name="toxicity-live", body={"group_key": "chat-42"}
        )
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_preview_cursor_is_refused_not_dropped():
    """`cursor` is not a preview member. The tool forwards the body
    verbatim, so ADAS's strict binding answers 400 rather than the client
    silently stripping it."""

    body = {"cursor": "abc"}
    with respx.mock(assert_all_called=False) as router:
        route = router.post(
            "http://adas.test/v1/pipelines/turn-feedback-live/preview"
        ).respond(
            400, json={"error": "bad_request", "message": "Unrecognized field 'cursor'"}
        )
        result = await adas_preview_pipeline(
            ctx=None, name="turn-feedback-live", body=body
        )
    assert json.loads(route.calls.last.request.content) == body
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 400


async def test_preview_budget_refusal_keeps_the_preview_code():
    """Both kinds refuse an over-budget preview read with one code."""

    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines/toxicity-live/preview").respond(
            422,
            json={
                "error": "pipeline_preview_budget_exceeded",
                "message": "this pipeline's preview (preview sample) exceeded ...",
            },
        )
        result = await adas_preview_pipeline(ctx=None, name="toxicity-live")
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["details"]["error"] == "pipeline_preview_budget_exceeded"


async def test_refresh_on_an_enrich_pipeline_maps_invalid_argument():
    """Refresh is the aggregate control plane; an enrich pipeline's windows are
    driven by the runner over the data plane this server does not wrap."""

    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines/toxicity-live/refresh").respond(
            422,
            json={
                "message": "pipeline 'toxicity-live' is of kind 'enrich'; refresh "
                "serves the 'aggregate' kind"
            },
        )
        result = await adas_refresh_pipeline(ctx=None, name="toxicity-live")
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422


async def test_patch_may_echo_name_and_kind():
    """A read-modify-write client sends the whole body; `name` and `kind` are
    accepted as long as they match, so they must not be stripped here."""

    body = {
        "name": "toxicity-live",
        "kind": "enrich",
        "target": "usage_toxicity",
        "trigger": {"kind": "schedule"},
        "transform": {
            "type": "llm",
            "model": "gemini-2.5-flash-lite",
            "request_template": '{"messages": [{"role": "user", "content": "{{prompt}}"}]}',
            "inputs": {"prompt": "request_body"},
            "outputs": {"toxicity_score": "How toxic the prompt is."},
        },
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.patch("http://adas.test/v1/pipelines/toxicity-live").respond(
            200, json={"name": "toxicity-live", "generation": 5}
        )
        await adas_patch_pipeline(ctx=None, name="toxicity-live", body=body)
    assert json.loads(route.calls.last.request.content) == body


async def test_declaration_patch_on_an_enabled_aggregate_maps_conflict():
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/pipelines/p").respond(
            409,
            json={
                "message": "pipeline 'p' is enabled; disable it before editing its "
                "declaration"
            },
        )
        result = await adas_patch_pipeline(ctx=None, name="p", body={"target": "t"})
    assert result["error"]["code"] == "conflict"


async def test_create_draft_sends_identity_and_target_only():
    """ADAS stores a disabled draft from name + kind + target alone; the client
    must not demand `enabled`, `inputs`, `trigger`, `measures` or `transform`."""

    for body in (
        {"name": "usage_daily_rollup", "kind": "aggregate", "target": "usage_daily"},
        {"name": "toxicity-live", "kind": "enrich", "target": "usage_toxicity"},
    ):
        with respx.mock(assert_all_called=False) as router:
            route = router.post("http://adas.test/v1/pipelines").respond(
                201, json={**body, "enabled": False, "generation": 1}
            )
            result = await adas_create_pipeline(ctx=None, body=body)
        assert json.loads(route.calls.last.request.content) == body
        assert result["enabled"] is False
        assert "error" not in result


async def test_draft_patch_subset_passes_through_verbatim():
    """A disabled draft save may carry any subset, and `target` is sticky, so
    the client forwards a body without `target` unchanged."""

    body = {"measures": [{"name": "call_count", "fn": "count"}]}
    with respx.mock(assert_all_called=False) as router:
        route = router.patch(
            "http://adas.test/v1/pipelines/usage_daily_rollup"
        ).respond(
            200, json={"name": "usage_daily_rollup", "target": "usage_daily", **body}
        )
        result = await adas_patch_pipeline(
            ctx=None, name="usage_daily_rollup", body=body
        )
    assert json.loads(route.calls.last.request.content) == body
    assert result["target"] == "usage_daily"


async def test_patch_emptying_complete_declaration_maps_invalid_argument():
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/pipelines/p").respond(
            422,
            json={
                "error": "pipeline_validation_failed",
                "message": "pipeline 'p': this patch would leave its complete "
                "declaration incomplete, missing transform, trigger.kind",
            },
        )
        result = await adas_patch_pipeline(
            ctx=None, name="p", body={"advanced": {"rate_rpm": 10}}
        )
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["status"] == 422
    assert result["error"]["details"]["error"] == "pipeline_validation_failed"


async def test_preview_of_draft_reports_gate_failure():
    """Preview of a disabled pipeline runs the arming gate; its refusal is a
    422 surfaced as invalid_argument with ADAS's code kept in details."""

    with respx.mock(assert_all_called=False) as router:
        router.post("http://adas.test/v1/pipelines/p/preview").respond(
            422,
            json={
                "error": "pipeline_validation_failed",
                "message": "trigger.kind is required",
            },
        )
        result = await adas_preview_pipeline(ctx=None, name="p")
    assert result["error"]["code"] == "invalid_argument"
    assert result["error"]["message"] == "trigger.kind is required"
    assert result["error"]["details"]["error"] == "pipeline_validation_failed"


async def test_patch_pipeline_relayout_hold_keeps_the_backend_code():
    """Arming an `llm` pipeline onto a `migrating` target is a transient 503;
    the envelope maps it to `internal_error` but keeps ADAS's own code."""
    with respx.mock(assert_all_called=False) as router:
        router.patch("http://adas.test/v1/pipelines/p").respond(
            503,
            json={
                "status": 503,
                "error": "table_relayout_in_progress",
                "message": "usage_x is being relaid out; retry shortly",
            },
        )
        result = await adas_patch_pipeline(ctx=None, name="p", body={"enabled": True})
    assert result["error"]["code"] == "internal_error"
    assert result["error"]["status"] == 503
    assert result["error"]["details"]["error"] == "table_relayout_in_progress"
