"""Pipeline registry and control plane (`PipelineController`, `PipelineDataController`).

One registry, two kinds. An `aggregate` pipeline is an in-DB rollup ADAS
refreshes on a cron; an `enrich` pipeline carries its own **`transform`** — a
model call or a SQL derivation — writing a target enrichment, and is driven by
the Enrichment Runner (except its Class-1 `sql` case, which ADAS materializes
itself). Both kinds are addressed by `name`, and `adas_create_pipeline` with
`body={"kind": "enrich", ...}` is what registers an enrichment.

An `aggregate` is declared in exactly one of two modes: **form mode**
(`group_by`, `measures`, `filter`) or **SQL mode** (one `sql` member holding an
`INSERT INTO <target> (...) SELECT ...` that carries the keys, the measures and
the `WHERE` itself). `inputs` and `trigger` belong to both. See
`adas_create_pipeline` for what the statement may contain.

An `enrich` pipeline carries the whole definition of what it computes in its
own nested `transform` block; there is no separate registry to name or read,
so a definition is read from its pipeline.

**Every** `/v1/pipelines` endpoint — the GETs included — is
`FULL_ADMIN` on the backend: declaring a pipeline means declaring what a table
people read as fact will contain, and reading one means reading a declaration
that names the columns it reads, `sensitive` ones included. The reads still
register under `limited` — the
capability toggle is a local guardrail, not the authorization; under
`SECURITY_MODE=oidc` a non-admin caller gets `permission_denied`.

The pipeline *data plane* (`POST /v1/pipelines/{name}/scan`, `/group-rows`,
`/materialize`, `/rows`) is deliberately not wrapped: it is the Enrichment
Runner's machine-to-machine API — cursor paging and batched row write-back —
not an agent-facing surface. Those four paths are the only `/v1/pipelines`
endpoints reachable without `FULL_ADMIN`, by any identity holding the target
enrichment's WRITE grant. Constraints there worth knowing even unwrapped: the
two *writing* paths, `/rows` and `/materialize`, refuse a **disabled** pipeline
with **409** `pipeline_conflict` (a disabled pipeline writes to its target by no
route); `/scan` and `/group-rows` stay open to a disabled pipeline but, like the
writing paths, refuse an **incomplete** declaration with 422
`pipeline_validation_failed` naming every absent member; `/materialize`
re-checks each output's result-type fit and the row-grain rule. A row's
provenance column is `pipeline_generation`. For a row-trigger pipeline
whose target is **aligned** with its source (`layout: aligned` on
`adas_get_table`), every `/scan` row carries `__partition_v`, the source row's
partition value, and every `/rows` row must return it unchanged (422
`validation_error` without it). Both writing paths answer **503**
`table_relayout_in_progress` while ADAS relays the target out (a minute or
two): retry the same request shortly.

**Drafts.** A pipeline can be stored **disabled with an incomplete — or
complete but not yet valid — declaration**: registration needs only `name`,
`kind` and `target`, and the kind's validation gate runs only when a write
leaves the pipeline **enabled** (arming) or when a disabled pipeline is
previewed. So the authoring loop is: register a draft → patch it while disabled
→ `adas_preview_pipeline` (reports the first problem arming would) → enable.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client, path_segment
from ..errors import MissingAuthError, invalid_argument, structured_error

# The wire codes ADAS publishes for `kind`. `external` is declared but not
# implemented — it binds fine as a *filter* (and matches nothing), while a
# request body carrying it is refused with 422 naming the kinds that work.
# `GET /v1/pipelines` binds this parameter by the lowercase code, so that is
# what goes on the wire; unknown values are rejected here rather than spent on a
# round trip. Ordered as ADAS publishes them, with the unimplemented kind last,
# so this tuple and the rejection message it renders agree with the server.
_PIPELINE_KINDS = ("aggregate", "enrich", "external")

# The two projections of one stored pipeline. They carry the SAME field names
# and differ only in what is resolved: `source` (the default) is the authored
# declaration, `compiled` fills in the derived values the Enrichment Runner
# needs. ADAS binds this by the lowercase wire code and answers 400 naming the
# accepted values for anything else, which is checked here rather than spent on
# a round trip.
_PIPELINE_VIEWS = ("source", "compiled")


def _normalized_view(view: str | None) -> tuple[str | None, dict[str, Any] | None]:
    """The wire spelling of `view`, or a rejection envelope.

    Both pipeline reads take the parameter and both normalise it identically, so
    this lives once: ADAS answers a generic 400 that does not say which value was
    wrong, and the point of checking here is the better message.

    Returns `(view, None)` when acceptable — `None` passes through untouched, so an
    omitted parameter stays omitted and ADAS applies its own default — or
    `(view, envelope)` when not.
    """

    if view is None:
        return None, None
    normalized = view.strip().lower()
    if normalized not in _PIPELINE_VIEWS:
        return view, invalid_argument(
            f"view must be one of {', '.join(_PIPELINE_VIEWS)}; got {view!r}"
        )
    return normalized, None


async def adas_list_pipelines(
    ctx: Context,
    target: str | None = None,
    kind: str | None = None,
    enabled: bool | None = None,
    updated_since: str | None = None,
    view: str | None = None,
) -> dict[str, Any]:
    """List registered pipelines — the whole registry by default, both kinds.

    Every filter is optional and they are applied together. Unfiltered this is
    **every registered pipeline, disabled ones included**, each carrying its own
    `enabled` flag: a pipeline an operator switched off is visible here rather
    than reachable only by name. That is how you tell "never registered" apart
    from "registered but not running", the usual question when a table stops
    getting new rows.

    Filters:
      - `target` — only the pipeline writing this table (there is at most one).
      - `kind` — `aggregate` or `enrich` (`external` is declared but not
        implemented, so it matches nothing).
      - `enabled` — pass a real boolean or omit it entirely. ADAS accepts only
        `true` or `false` on the wire and rejects everything else with 400 — an
        empty value included, so there is no "unset" spelling other than leaving
        it out.
      - `updated_since` — ISO-8601 lower bound on `updated_at`.
      - `view` — which projection of each pipeline to return, `source` (the
        default) or `compiled`. See below.

    Order is `updated_at` then `name`, stable across calls over an unchanged
    registry.

    ## `view` — two projections, one vocabulary

    Both projections carry the **same field names**; they differ in what is
    resolved, never in what anything is called. There is deliberately no second
    spelling of any field.

      - `view="source"` (**default**) — the declaration as authored: the
        `transform` block (including `transform.inputs` and, where the
        declaration carries them, `transform.output_vars` and a stored
        `transform.response_schema`), `advanced`, and the *stored*
        `inputs`, which is **absent** when the pipeline declared none (an
        `enrich` pipeline that follows its target enrichment's own parent).
        Every other **undeclared** member of a draft is likewise absent rather
        than `[]`/`{}` — `group_by`, `measures`, `trigger` — so absent means
        "not declared yet" and `[]` means "declared empty".
        Nothing is resolved, so this costs no extra lookups. This is the one to
        read when the question is "what was declared", and the only one a
        read-modify-write client should read: the authored members round-trip
        straight back as the next patch.
      - `view="compiled"` — the same declaration with the derived values filled
        in. The derived members are **exactly five**: `response_schema` (the
        schema the model is held to — the declaration's stored one, or one
        composed from the target's columns and `transform.outputs`),
        `grain_key` derived from the target enrichment, `version_column` (the
        physical name of the input's version column, absent when the input
        declares no scan metadata), the resolved `outputs` mapping
        (`[{name, column, jsonata?, source?}]`), and `inputs` **resolved** (so a
        declared input and an inherited one are indistinguishable here).

    **`transform.response_schema` is served on `view="source"`
    only**: on `compiled` the same fact is the derived `response_schema`, and a
    multi-kilobyte schema does not cross the feed twice. The transform itself
    is authored, not derived, so it appears in **both** projections.

    **None of the five derived members is accepted on a write** — each is a
    binding 400 naming the field — so a `view="compiled"` body sent back
    unchanged is refused rather than laundering a composed schema into a stored
    one.

    `kind="enrich"&view="compiled"` — unfiltered by `enabled` — is the
    Enrichment Runner's sync feed, and the runner asks for `compiled`
    explicitly. It executes only entries carrying `enabled: true` and holds a
    stopped one's work, so a stopped pipeline (listed, `enabled: false`) and a
    deleted one (absent) stay distinguishable. **A compiled listing
    omits every entry whose declaration is incomplete** (a draft), so a
    registered draft is visible on `source` but not on `compiled`.

    **`view="compiled"` requires `kind="enrich"` and is refused with 400
    `bad_request` without it.** Only the `enrich` kind resolves anything, so a
    cross-kind compiled listing has nothing to compile for the rest. Pair the
    two, or ask for `source`.

    An unknown `view` is rejected here, naming the two accepted values — ADAS
    binds the parameter itself and answers a generic 400 that does not say which
    value was wrong.

    Returns: `{pipelines: [PipelineDto]}` — an envelope, **not** `items`. Each
      entry carries the shared declaration (`name`, `kind`, `target`, `inputs`,
      `filter`, `trigger`, `enabled`, `generation`, `created_at`, `updated_at`),
      the members of its own kind, and a read-only `state`. Members belonging to
      the other kind are simply absent.
      `{error: {code: "invalid_argument"}}` if `updated_since` is not a valid
      ISO-8601 instant or `view` is not one of `source` / `compiled`, and
      `{error: {code: "bad_request"}}` from ADAS for `view="compiled"` without
      `kind="enrich"`.

    Reading `state` — the run-position half is **ADAS-owned, so it is absent
    for `enrich` entirely**: `cursor_version`, `cursor_identity`, `lag_seconds`,
    `last_run_at`, `next_run_at`, `last_error` and `has_more` all describe runs
    ADAS drives, and an enrich pipeline's are driven by the Enrichment Runner,
    which holds that position itself. Absence there says "this kind has no such
    position", not "nothing has run" — do not read a missing `last_error` as a
    healthy enrichment.

    For an `aggregate` pipeline those members do report: `lag_seconds` is
    measured against the present at read time and is expected to be a
    **sawtooth, not a level** — its floor is roughly the safety lag plus the run
    duration plus ingest lag, and its ceiling is one cron period above that. It
    is not "time since the last run". Read it alongside `last_run_at`,
    `last_error` and `has_more` to tell a stall from a healthy cadence, and put
    any alert threshold above one cron period.

    The freshness half is reported for **both** kinds:
      - `clamp` — `{enrichment, cursor_version, cursor_identity}` naming what
        held the last window short of its input's available cursor, absent when
        nothing did. Read it as progress, not error: a pipeline reading an
        enrichment is only as fresh as that enrichment. A clamp that persists
        for more than a few enrichment cadences does mean the upstream has
        stopped advancing.
      - `unclamped_reads` — `[{enrichment, reason}]`, usually empty: enrichments
        read with no freshness bound at all. `no_pipeline`, `ingest_written`,
        `foreign_axis` and `undeclared_writer` are benign — `ingest_written`
        means data-sync writes the enrichment beside its source and flushes it
        first, so an absent value is final rather than late; `undeclared_writer`
        means the pipeline targeting it is a disabled draft with an incomplete
        declaration, which writes nothing, so the read is admitted unclamped
        until that draft is armed. `group_trigger` and `not_class1` are refused
        when this pipeline is armed, so they only appear when the upstream
        changed shape afterwards and need an operator.
      - `rebuild_required` — `{enrichment, rederived_at}`, and an
        **instruction**, not progress: an enrichment this pipeline reads had its
        history re-derived after this output was last built, and no future run
        corrects it. Rebuild deliberately.
      - `materialized_through_version` / `_identity` — how far ADAS has
        materialized this pipeline's target, Class-1 (`transform.type: sql`)
        `enrich` pipelines only; it is what every dependent rollup's clamp
        reads.
      - `drained_at` — when ADAS last probed and found nothing left matching the
        `filter`.

    For change detection: `generation` is the **declaration's revision**,
    bumped once per accepted re-declaration (an enable whose arming gate
    re-derives a stored member counts as one) and **not** by a patch that only
    flips `enabled`; `updated_at` moves on every accepted change, a stop or
    restart included. Compare `generation` to detect a re-declaration, and
    `updated_at` to detect any change.

    Related: adas_get_pipeline, adas_create_pipeline, adas_list_tables,
      adas_get_table.

    Examples:
      (no arguments)
      target="usage_daily"
      kind="aggregate"
      kind="enrich", enabled=True
      enabled=False
      kind="enrich", updated_since="2026-07-01T00:00:00Z"
      kind="enrich", view="compiled"
    """

    if kind is not None:
        normalized = kind.strip().lower()
        if normalized not in _PIPELINE_KINDS:
            return invalid_argument(
                f"kind must be one of {', '.join(_PIPELINE_KINDS)}; got {kind!r}"
            )
        kind = normalized
    view, rejection = _normalized_view(view)
    if rejection is not None:
        return rejection
    return await get_client().request(
        "GET",
        "/v1/pipelines",
        params={
            "target": target,
            "kind": kind,
            "enabled": enabled,
            "updated_since": updated_since,
            "view": view,
        },
        ctx=ctx,
        require_auth=False,
    )


async def adas_get_pipeline(
    ctx: Context, name: str, view: str | None = None
) -> dict[str, Any]:
    """Get one pipeline's declaration plus its read-only runtime state.

    Works for either kind, enabled or not. An `aggregate` pipeline carries
    `group_by` and `measures` (form mode) or `sql` (SQL mode, returned
    verbatim, comments and layout included); an `enrich` pipeline carries `transform`,
    `trigger` and `advanced`. Members of the other kind are absent rather than
    empty.

    `view` selects the projection, `source` (the default) or `compiled` — the
    same field names either way, differing only in what is resolved. `compiled`
    adds, for an `enrich` pipeline, exactly five derived members:
    `response_schema` (the schema the model is held to, stored or composed),
    the resolved `outputs` mapping, the resolved `inputs`, `grain_key` and
    `version_column`. The `transform` block is **authored**, so it is in both
    projections — except `transform.response_schema`, which is served on
    `source` only, because on `compiled` the derived `response_schema` already
    carries that fact. `source` echoes the declaration as authored, with
    `inputs` absent when the pipeline declared none. See `adas_list_pipelines`
    for the full description of both.

    **`compiled` resolves only for the `enrich` kind, and a compiled read of any
    other kind is refused with 422 `pipeline_validation_failed`** naming the
    pipeline's kind. It is not downgraded to the declaration: answering with the
    unresolved object under the compiled name is indistinguishable, to a
    consumer, from an `enrich` pipeline that genuinely has no `response_schema`,
    no `grain_key` and no `outputs`. Use `view="source"` to read an `aggregate`
    pipeline's declaration. A compiled read of an `enrich` pipeline whose
    declaration is **incomplete** (a draft) is likewise a 422
    `pipeline_validation_failed`, naming every absent member in one message;
    read a draft with `view="source"`, where undeclared members are absent.

    `state` is likewise per kind. Its whole run-position half —
    `cursor_version`, `cursor_identity`, `lag_seconds`, `last_run_at`,
    `next_run_at`, `last_error` and `has_more` — is ADAS-owned and so **absent
    for `enrich`**, whose scan position is held by the Enrichment Runner. An
    absent member says "this kind has no such position", where a zero would read
    as a real one; in particular a missing `last_error` on an enrich pipeline is
    not evidence that it is healthy. The freshness half (`clamp`,
    `unclamped_reads`, `rebuild_required`, `drained_at`) is reported for both.
    `materialized_through_version` appears only for a Class-1
    (`transform.type: sql`) enrich pipeline, and is what every dependent
    rollup's freshness clamp reads.

    Returns: a `PipelineDto` (see `adas_list_pipelines` for the field list and
      for how to read `state`). `{error: {code: "not_found"}}` for an unknown
      name; `invalid_argument` if `view` is not one of `source` / `compiled`;
      `{error: {code: "invalid_argument", status: 422}}` (ADAS's
      `pipeline_validation_failed`) for `view="compiled"` against a pipeline
      whose kind is not `enrich`, or whose declaration is incomplete.

    Related: adas_list_pipelines, adas_preview_pipeline, adas_patch_pipeline.

    Examples:
      name="usage_daily_rollup"
      name="toxicity-live"
      name="toxicity-live", view="compiled"
    """

    if not name:
        return invalid_argument("name is required")
    view, rejection = _normalized_view(view)
    if rejection is not None:
        return rejection
    return await get_client().request(
        "GET",
        f"/v1/pipelines/{path_segment(name)}",
        params={"view": view},
        ctx=ctx,
        require_auth=False,
    )


async def adas_preview_pipeline(
    ctx: Context, name: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Dry-run a pipeline: what it would produce, without writing anything.

    Serves **both kinds in one flat shape**, carrying only what a preview alone
    can know. The declaration itself (`group_by`, `measures`, `transform`, the
    resolved `outputs`/`inputs`) is **not echoed**: read it with
    `adas_get_pipeline` (`view="compiled"` for an `enrich` pipeline's derived
    mapping). **Writes nothing**, calls no model, and is allowed on a disabled
    pipeline: this is the checkpoint between declaring a pipeline and letting
    it write.

    **On a disabled pipeline, preview is the author's diagnostic.** Drafts are
    stored ungated, so previewing one runs the full arming gate — the trigger's
    own completeness (a `trigger.kind`, a `cron` on an `aggregate`
    `schedule`), then the kind's declaration gate — and reports **the same first problem enabling it would**,
    as 422 `pipeline_validation_failed` (one problem per call: fix it and
    preview again). A preview that passes means arming will pass. By state:
      - `aggregate` — the full gate runs in **both** states.
      - `enrich` — the full gate runs only while **disabled**; an **enabled**
        pipeline was gated when it was armed, so its preview checks only the
        template placeholders, the transform's presence and type, and that the
        response schema still composes — never upstream drift.
    A disabled preview samples through the output mapping its gate just
    derived, so a draft whose stored mapping is stale still previews whenever
    arming would succeed.

    ## The response

    `{statement?, query?, columns, rows, total_rows?, group_key?, notes}` —
    `columns`, `rows` and `notes` are always present:
      - `statement` — the declaration as **one portable `SELECT` in the ADAS
        SQL dialect**, accepted unchanged by `adas_execute_sql` (and
        `adas_validate_sql`): it reads the catalog entity and its exposed
        fields (an enrichment column by its exposed name, with no join) and
        names each output by its target column — no `SETTINGS`, no `__`
        helper alias, no physical name. It computes what the declaration
        computes, ordered by the keys (`aggregate`) or the grain key
        (Class-1) rather than newest first, so its rows need not be the
        sampled ones, and running it does not reproduce `total_rows`. It is
        **not** the physical statement that produced `rows` (that is not
        returned), nor the refresh `INSERT`: the next window is runtime
        state, read it from `adas_get_pipeline`'s `state`. Present for
        `aggregate` and Class-1 (`transform.type: sql`) unless `notes`
        carries `statement_not_portable`; absent for `llm`.
      - `query` — the same query as a structured-query DSL document, the body
        `POST /v1/queries/execute` accepts verbatim (and the form an
        `inline_query.query` on a dashboard widget or a saved query's `query`
        takes). Present exactly when `statement` is.
      - `columns` — `[{name, role, type, target_type?, max_bytes?}]`, one per
        sampled column. `role` is `key` / `measure` (aggregate), `grain_key` /
        `output` (Class-1) or `grain_key` / `input` (`llm`). `type` is the
        value's type as sampled; `target_type` the column it is written to
        (present for `key`, `measure`, `output`). `max_bytes` is the widest
        sampled `string`/`object`/`array`/`map` value **before** the cap — on
        an `input` column, a hint at what one row costs the model.
      - `rows` — the sample, **newest first** (an aggregate's groups by their
        latest input row; Class-1 and row-grain `llm` rows by version; a
        group's members keep the runner's assembly order). Every cell is
        capped at **2048 bytes**: a longer string ends in `…`, and a longer
        object/array/map becomes its cut JSON *text* — the `values_truncated`
        note names each such column, so parse only the others. An `llm`
        sample carries only the bound inputs (plus the grain key on row-grain);
        filter, cursor and identity columns are not returned.
      - `total_rows` (`aggregate` only) — the number of groups over the whole
        input, i.e. the rows the target holds once caught up; `0` when empty.
        A count far above what you expect means a wrong group key (a very wrong
        one is refused by the preview budget instead, the same signal).
      - `group_key` (`group` pipelines only) — the group sampled: the one you
        named, or the one preview chose.
      - `notes` — `[{code, severity, subject?, message}]`, `severity` `info` or
        `warning`; branch on `code`, not the message. The closed set:
        `no_matching_rows`, `held_by_enrichment` (an enrichment the pipeline
        reads has no usable position, or every matching row lies past it — an
        enabled run would write **nothing** yet), `enrichment_behind` (matching
        rows lie past an enrichment's position; the message gives the gap —
        a few minutes is cadence, days is a stalled enrichment),
        `no_group_found`, `target_type_narrowing` (e.g. `count` into an
        `integer` column), `value_out_of_target_range` (a sampled value the
        target cannot hold — wrapped/clamped silently, or a decimal overflow
        that fails the refresh), `group_cut_by_fetch_limit` (the runner would
        deliver fewer members than the group has), `output_without_description`
        (one per `llm` output the model is told nothing about),
        `unconstrained_response`, `values_truncated`,
        `statement_not_portable` (the declaration has no form the query API
        accepts, so `statement` and `query` are both absent).

    ## kind="aggregate"

    Takes **no body**: the sample is unwindowed by design (it answers "are
    these the right numbers" over the entire input, which a window obscures),
    and any body is refused (422) rather than ignored.

    ## kind="enrich"

    **No model is called and no prompt is rendered.** The template renderer and
    the group-input assembler live in the Enrichment Runner, and ADAS has no
    dry-run surface for the rendered request. It previews the **stored**
    declaration and accepts no candidate `transform`: patch while disabled →
    preview → enable. Samples are five rows (five members on `group`), read
    through the governed path the pipeline itself uses, so they show only what
    the pipeline may read.

    The optional `body` positions that sample, and each member is refused on
    the wrong grain rather than ignored:
      - `{"group_key": "..."}` — the group to sample, `group` pipelines only.
        **Optional**: without it preview samples the group of the newest
        matching row with a non-empty key (first over the last 24 hours, then
        unbounded), under the same enrichment clamp the runner's group marking
        applies, and reports the key it chose; `no_group_found` when there is
        none.
      - `{"from": "...", "to": "..."}` — row-grain and Class-1 only. `from`
        (exclusive) and `to` (inclusive) bound the input's version column, and
        `to` is always clamped by the safety-lag horizon.
      - There is no `cursor` member (a seek-past cursor means nothing under a
        newest-first order): a body carrying it is refused, as is any unknown
        member.

    Returns: the shape above.
      `{error: {code: "not_found"}}` for an unknown name;
      `{error: {code: "permission_denied", status: 403}}` when the caller may
      not read a `sensitive` column the declaration reads
      (`sensitive_column_not_entitled`), a column policy hides one from it
      (`column_not_entitled`), it is row-confined, or it lacks the target's
      grant;
      `{error: {code: "invalid_argument", status: 400}}` for an unknown body
      member such as `cursor`;
      `{error: {code: "invalid_argument", status: 422}}` when a body member
      belongs to the other grain, when any body is sent to an `aggregate`
      preview, or when a gated preview (see above) finds the declaration
      incomplete or invalid — `details.error` is then
      `pipeline_validation_failed` and the message names the problem — or when
      a read exceeds the preview budget (`details.error`
      `pipeline_preview_budget_exceeded`, on both kinds; the message names the
      read and the remedy: a narrower `from`/`to`, fewer heavy columns, or a
      raised budget).

    Related: adas_get_pipeline, adas_create_pipeline, adas_patch_pipeline,
      adas_refresh_pipeline.

    Examples:
      name="usage_daily_rollup"
      name="usage-client-identity-live"
      name="conversation-insights-live"
      name="conversation-insights-live", body={"group_key": "chat-42"}
      name="turn-feedback-live", body={"from": "2026-07-01T00:00:00Z", "to": "2026-07-02T00:00:00Z"}
    """

    if not name:
        return invalid_argument("name is required")
    if body is not None and not isinstance(body, dict):
        return invalid_argument("body must be an object")
    try:
        return await get_client().request(
            "POST",
            f"/v1/pipelines/{path_segment(name)}/preview",
            json=body or None,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_create_pipeline(ctx: Context, body: dict[str, Any]) -> dict[str, Any]:
    """Register a pipeline: an `aggregate` rollup, or an `enrich` transform.

    `body` is a `CreatePipelineRequest` — call `adas_describe_schema` with
    `type_name="CreatePipelineRequest"` for the exact shape. **One flat body
    serves both kinds**, with the per-kind members simply optional, and three
    rules make that safe:

      - `name`, `kind` and `target` are the **only** required members. `name`
        is an **identity**, not a SQL identifier: `^[a-z][a-z0-9_-]{0,63}$`, so
        both `snake_case` and `kebab-case` are accepted. It is immutable, and it
        is the `{name}` in every other pipeline call. Every table named
        (`target`, `inputs`) must exist in the catalog (422 otherwise).
      - **A member belonging to the other kind is rejected with 422**, naming
        it, never ignored. Aggregate-only: `group_by`, `measures`, `sql`. Enrich-only:
        `transform` and `advanced` — and a nested member is named
        **`<block>.<member>`** in the refusal (`transform.request_template`,
        `advanced.rows_per_scan`), not just the block, so you are told which field
        you misplaced rather than which block.
      - Request bodies are read **strictly**: an unrecognized field is a 400
        naming it. Do not invent members — look the shape up.

    Note the two statuses: a member belonging to *another kind* is a 422, a
    member belonging to *no kind* is a binding 400. `transform.output_vars` is
    refused on **`POST` only** — see the `transform` block below.

    One pipeline per `target`: registering a second one for a table already
    written by another is a 409.

    ## Drafts — validation runs when the pipeline is armed, not when stored

    A registration that leaves the pipeline **disabled** is stored as a
    **draft** and the kind's validation gate does **not** run: the body may be
    as small as `{"name", "kind", "target"}`, and a complete body that would
    fail validation (a measure whose type does not fit its column, group keys
    that do not match the `ordering_key`, an output naming no target column) is
    **accepted too**. The gate — one problem per refusal, 422
    `pipeline_validation_failed` — runs when a write leaves the pipeline
    **enabled** and on `adas_preview_pipeline` of a disabled pipeline, which
    reports the same first problem arming would. So the rules below marked
    "required" or "must" are what **arming** asks; a draft may lack them.
    Build a pipeline up with `adas_patch_pipeline` while it is disabled, then
    preview, then enable. Two refusals stay unconditional on every write: a
    malformed body (400 naming the field) and an internal contradiction (a
    `member_select` on a non-`group` trigger, a non-`schedule` trigger on an
    `aggregate`).

    ## kind="aggregate" — an in-DB rollup

    Always lands **disabled**: the lifecycle is **register → preview → enable**,
    so `adas_preview_pipeline` always sits between declaring a rollup and
    letting it write. `enabled: true` on a registration is **refused** (422)
    naming the patch endpoint rather than silently ignored; `false` and an
    absent `enabled` are both accepted. Because it always registers disabled,
    an `aggregate` registration is **never gated** — a 201 does not mean the
    declaration is valid; the preview is what tells you. Enable it with
    `adas_patch_pipeline` and `body={"enabled": true}`.

      - `target` must be a materialized `source` table written `upsert_by_key`
        whose `ordering_key` equals this pipeline's group-key output names.
      - `inputs` — required to arm in form mode; v1 reads exactly one source
        table. Optional in SQL mode (see below).
      - `measures` — required to arm in form mode (at least one).
      - `group_by` — **optional**: omit it and the keys are derived from the
        target's `ordering_key`, when every key column is an input column of the
        same name. The derived keys are expanded into the stored declaration and
        echoed back, and `adas_get_pipeline` is where to check what they
        resolved to. Declared, each key is either a `column` or a `trunc` (a
        fixed-width time bucket: `hour`/`day`/`week`/`month`, UTC-aligned, week
        starting Monday). `as` names the target column and is required for a
        `trunc`.
      - `measures` — `{name, fn, column?, where?, distinct?}`: one aggregate
        function over one column each, never an expression. `fn` comes from the
        DSL catalog's aggregate group (`adas_list_query_functions`: `count`,
        `sum`, `avg`, `min`, `max`, `group_uniq_array`) — DSL names, not SQL
        spellings; `column` may be omitted only for `count`.
        `group_uniq_array` binds only to an `array` target column — it collects
        the group's distinct non-null values, sorted.
      - `measures[].where` — an optional row-membership predicate in the same
        bounded grammar as `filter`, scoping *one* measure to part of its group
        (e.g. `sum(total_price) where status = 'ok'`). It selects rows; it does
        not change the one-function-over-one-column rule. A measure carrying a
        `where` can yield NULL for a group nothing matches, so its target column
        must be nullable.
      - `measures[].distinct` — when true, deduplicates the input column's
        values before the function is applied (`count(DISTINCT trace_id)`).
        Accepted only for a function the catalog marks `distinct_supported`
        (`count`, `sum`, `avg`); rejected for `min`/`max`, and rejected as
        redundant for `group_uniq_array`. It requires a `column`, and composes
        with `where` — the predicate picks the rows, `distinct` deduplicates the
        values on them. A predicate is **not** a substitute for it: `where`
        selects rows by a property each row carries alone, so it cannot express
        "one row per distinct entity".
      - `filter` is required when a group key reads a nullable input column: a
        NULL key cannot be represented in the target, so declaring e.g.
        `col IS NOT NULL` is how you state the NULLs are accounted for.
      - `trigger` — only kind `schedule`, with a 6-field Spring cron. Its kind
        may be omitted (it can only be `schedule`), but the cron is required
        to arm.

    ### SQL mode — `sql` in place of `group_by`, `measures` and `filter`

    `sql` is one `INSERT INTO <target> (<col>, ...) SELECT ...` statement. It is
    **mutually exclusive** with the form members: `sql` beside `group_by`,
    `measures` or `filter` is a 422 on every write, and a blank `sql` is a 422
    (`sql must not be blank`) on every write, armed or not. ADAS stores the
    text **verbatim** but never runs it: it parses the statement, applies every
    form-mode gate to the parsed block, and executes its own rendering. Complete
    (armable) means a `trigger.cron` plus an `sql` from which an input table can
    be read; `measures`, `group_by` and `inputs` are not asked. What arming and
    preview refuse (422 `pipeline_validation_failed` naming the clause, column
    or function, unless noted):
      - **Shape** — exactly one statement (trailing `;` tolerated); the
        `INSERT` target equals `target`; the column list names every target
        column except `_updated_at`, once each, mapped to the select list **by
        position**. No backticks (double-quote identifiers), no `SETTINGS`, no
        `FORMAT`.
      - **One input, read once** — `FROM` names the input; `inputs` may be
        omitted (derived from `FROM`) or must equal it (422 naming both — after
        changing `FROM`, drop or update an echoed `inputs`). Enrichment columns
        only as `"<enrichment>.<column>"` via the implicit grain join. Refused:
        any other table (an enrichment table named directly included), a second
        reference to the input (a self-join or a CTE read twice), `JOIN`,
        `UNION`/`INTERSECT`/`EXCEPT`, an expression subquery (scalar, `IN`,
        `EXISTS`), `SELECT *`, `FINAL`.
      - **One aggregating block** — a `GROUP BY`, and no `HAVING`, `DISTINCT`,
        `ORDER BY`, `LIMIT`/`OFFSET` or window function. CTEs/derived tables
        that only filter or compute columns are fine; one that aggregates,
        dedups or sorts below the top level is refused.
      - **Group keys** — equal the target's `ordering_key` as a set; each reads
        the input row, is non-nullable (wrap one in `coalesce(key, <default>)`),
        has the target column's type, and calls no `array_join`; at most one
        time bucket, `date_trunc(<unit>, t)` or `date_bin(1, <unit>, t)` over
        `hour`/`day`/`week`/`month`, in UTC (no time-zone argument).
      - **Functions and types** — only functions the shared query function
        catalog lists, none non-deterministic; measure
        expressions held to the form path's type/nullability rules per target
        column. `count(*)`, not `count()`; `min`/`max(DISTINCT x)` are accepted
        here though form mode refuses `distinct` for them.
      - **Governance** — resolved at your read entitlement: a `sensitive`
        column you may not see is **403** `sensitive_column_not_entitled` (also
        when the statement cannot be planned but its text mentions one), and a
        column a column policy hides from you is **403**
        `column_not_entitled` (which wins when a column is both). A
        target column whose expression reads a `sensitive` column must itself be
        `sensitive` (lineage from the parsed expressions; a column read only in
        a `CASE WHEN` condition inside an aggregate's arguments is a row filter
        and exempt). An enrichment read must be clampable: one written by a
        `group` pipeline, or by a pipeline ADAS does not execute, is a 422.
    A disabled write stores an `sql` that does not yet parse or plan; arming it
    is then a 422 naming `sql`.

    ## kind="enrich" — a transform writing a target enrichment

    `enabled` is **optional**, and absent means `false`: the pipeline is stored
    as an ungated draft. `enabled: true` at registration is allowed and **runs
    the full gate**, because that write arms the pipeline — so a registration
    that is meant to run immediately must be complete and valid.
    `adas_preview_pipeline` works on an `enrich` pipeline either way, and on a
    disabled one it runs the same gate arming would, so it is the call to make
    before enabling.

      - `target` must be an `ACTIVE` table of kind `enrichment`. The grain is
        **derived from it** and must not be supplied.
      - `inputs` — **optional**, and its absence is meaningful: omitted, the
        pipeline reads its target enrichment's own parent source. A declared
        input is validated more strictly — it must carry the target's grain key
        and declare scan metadata — and it is what every governed read resolves
        against: `filter`, `trigger.ready_when.signal`, `transform.inputs` and
        `trigger.member_select` are all checked against *its* columns. Note the
        name collision this member has with `transform.inputs`, and that the
        two are different JSON types: **top-level `inputs` is the array of
        source table names**, `transform.inputs` is the **object** mapping
        variable name → what the transform reads. A member sent at the wrong
        level is a binding 400 rather than a silent accept.
      - `transform` — required to arm, and the whole definition of what this
        pipeline computes; there is no separate registration step. Editing what
        an enrichment computes is one `adas_patch_pipeline`.
        Members: `type`, `model`, `preset`, `route`, `params`,
        `request_template`, `inputs`, `outputs`.
          - `type` is **required** and is never inferred: it selects the
            *grammar* of an `outputs` value, and both grammars are "a string"
            on the wire, so a `sql` transform that lost its `type` would have
            its expressions read as prose and composed into a response schema.
            A missing `type` on an `enrich` declaration is a 422 naming it.
          - `type: "llm"` — `model` (a DIAL deployment: a model, or an
            application under `dial_app_route`) and `request_template` are both
            **required and non-blank** (422 otherwise). The template is a JSON
            request body with `{{placeholder}}` substitutions, not a bare
            prompt. `{{name}}` escapes the value into a JSON string;
            `{{name:json}}` renders it **unescaped, as a JSON literal** in value
            position (`{"members": {{members:json}}}`), for either preset and
            either grain — the suffix is exactly `:json`, and
            `{{members:json}}` satisfies the group rule that `{{members}}`
            appear. Its *placeholders* are checked only at enable and at
            preview, not on every write, so a declaration whose template is
            written before its inputs are bound can be saved — but a transform
            carrying no template at all cannot, because a wholesale `transform`
            patch omitting it would leave an enabled pipeline armed with no
            prompt. `params` is overlaid on the rendered body's top-level
            object, replacing a member of the same name.
          - `preset` — how the deployment is called, optional:
            `chat_completion` (the default) POSTs to the deployment's chat
            completions endpoint with the composed schema as
            `response_format`; **`dial_app_route`** POSTs the rendered body to
            `/v1/deployments/{model}/route/{route}` and reads the **whole
            response body** as the output — no chat envelope, no
            `response_format`, no repair retry, but the output is still
            validated against the compiled `response_schema`.
          - `route` — the path under the application's `route/` prefix,
            **required for `dial_app_route`** and refused for
            `chat_completion` (or an omitted preset) and for `sql`. 1–256
            characters, `/`-separated segments of `[A-Za-z0-9._~-]`, no leading
            or trailing `/`, no `.`/`..` segment, no `?`, `#` or `%`. Stored as
            authored, case included; a blank one is stored as absent. Arming
            also needs the `(model, route)` pair on the instance's
            **`app-routes` allowlist** (read it with `adas_get_setting`,
            `name="app-routes"`) — an unlisted pair is a 422, and with the
            setting absent no route is allowed. A `dial_app_route` transform
            may not carry the legacy `output_vars`.
          - `type: "sql"` — a Class-1 derivation ADAS materializes itself.
            `preset`, `model`, `route`, `params`, `request_template` **and**
            `response_schema` must all be absent (422 naming the member), and
            it declares no `transform.inputs`: there is no request to render.
          - `transform.inputs` — what the transform receives, an object
            **keyed by variable name**. Each value is either a bare string (a
            column of the read source's entity, under either spelling, or an
            enrichment column under its qualified flat name) or an object
            `{column}` / `{jsonata}` — exactly one of the two. At **row** grain
            each key must appear as a `{{placeholder}}` in `request_template`;
            at **group** grain the keys become fields of each member object
            inside `{{members}}` and appear in no placeholder.
          - `transform.outputs` — what the transform produces, an object
            **keyed by the target column** that receives each output, and the
            single declaration of it. For `llm` a value is either a bare
            string (the prose the model is told about that field) or an object
            `{prose?, values?, jsonata?, source?}` with **at most one** of `values` /
            `jsonata`: `values` declares a domain for a field whose target
            column cannot carry one (a column that already declares
            `enum_values` and also receives `values` is a 422 naming both; a
            `null` member of `values` is a 422), and
            `jsonata` transforms the model's response (the identity expression
            — just the output's own name — is a 422, since an absent `jsonata`
            already means a direct lookup by name). Under `dial_app_route`,
            `jsonata` is refused and **`source`** takes its place: a JSONata
            expression over the route's response body yielding this output's
            value, usually a dotted path such as `intent_counts.check`; absent
            (or blank), the output takes the response member of its own name,
            and a `source` equal to that name is refused as the identity.
            `source` is refused under `chat_completion`. The runner assembles
            one object from every output's value and validates it. For `sql` a value is a bare
            string: one scalar SQL expression over the read source's declared
            columns, parsed by the SQL front end, so its functions and
            operators come from `adas_list_sql_functions`. The target column is matched by its **exposed** name, not
            its `source_name`; a key matching only a `source_name` is a 422
            naming the spelling it expected.
          - **`prose` is optional for an `llm` output**, defaulting to the
            target column's `description`. Absent, `null`, `{}` and a blank
            string all mean *default it* — one fact, one meaning, so none of
            the spellings is refused in favour of another. Composition takes
            an output's type, enum domain and enum order from the same column
            too. Two consequences worth knowing: a `description`-touching
            metadata patch on an `llm` target bumps the pipeline's
            `generation`, because the description is a live prompt input; and
            an output with neither prose nor a column description composes a
            property with no description at all, which `adas_preview_pipeline`
            reports as a note rather than refusing. **`sql` outputs are
            unaffected** — their string is an expression, not prose, cannot be
            defaulted from anything, and stays required.
          - **Declaration order in `outputs` is load-bearing.** It becomes the
            composed response schema's property order, which under
            `response_format: {type: json_schema, strict: true}` is the model's
            *generation* order — so an output whose prose tells the model to
            derive it from its siblings must be declared after them. Send the
            members in the order you want them generated.
          - **Composition differs by preset.** Under `chat_completion` every
            output is required and an output bound to an `object` or `map`
            column is a 422 (strict mode cannot describe an open object).
            Under `dial_app_route` nullability follows the bound column (an
            output on a nullable column may be null or absent), an `object`
            column composes `{"type": "object"}` and a `map` column a
            string-valued object — always required, so a `source` that filters
            members must yield `{}` rather than nothing. The arming gate
            composes the schema, so any composition refusal is a 422 at
            enable.
          - Two further members can be stored on a declaration:
            `transform.output_vars` (an alternative output shape) and
            `transform.response_schema` (a pinned schema).
            `output_vars` is **refused on `POST`** with 400 naming it — a new
            declaration authors `outputs` — while `response_schema` **is**
            accepted here, because pinning a schema byte for byte is a
            legitimate thing for a new declaration to do, and while it is
            present it wins over composition. A declaration carrying both
            `outputs` and `output_vars` is refused with 422.
      - `trigger` — required to arm. `trigger.kind` is `schedule` (row grain,
        cron-driven) or `group`. `trigger.cron` is only valid with `schedule`;
        on `enrich` it is optional — omitted, it is stored and echoed as every
        minute at a second derived from the name (`S * * * * *`). A group
        pipeline's mark-scan cadence is runner config, not declared here. Any
        re-declaration bumps `generation`, which on a `sql` transform discards
        its materialized position — changing its cron is not free.
        `trigger.group_by` and `trigger.ready_when` are required for (and only valid with) `group`, and
        `trigger.group_by` must equal the target enrichment's grain key. Note
        this `group_by` is the trigger's grain — a single column name — not the
        aggregate kind's list of key parts; that name collision is why it nests
        under `trigger`.
      - `trigger.ready_when` needs at least one of `signal`, `idle`, or
        `max_staleness` — a declaration with none marks a group dirty forever,
        so it would silently never evaluate.
      - `trigger.member_select` — group trigger only, and optional:
        `{prefer_sql, order_by, limit}`, the policy deciding *which* of a
        group's members bear on its label. `prefer_sql` is a preference, not a
        second membership filter — when no member of a group satisfies it, every
        member becomes a candidate. `order_by` ranks the candidates
        most-interesting first (always tie-broken by the source's total order
        descending, so an unchanged group re-assembles identically), and `limit`
        lowers the assembly's effective row cap rather than sitting beside it.
        Members are selected from inexpensive columns *before* any bound body
        column is read, so the assembly's cost follows the members returned
        rather than the group's size. Omitted, assembly keeps the newest members
        by the source's total order.
      - **There is no `output_bindings`.** Where each output lands is *derived*
        on every declaration write (best-effort on a draft) from
        `transform.outputs` matched against the target's columns, which is why
        an output is keyed by the target column that receives it. A pipeline
        declaring no output that resolves to a column on the target is refused
        with 422 naming the output and the target when it is armed or a
        disabled one is previewed. Read
        the resolved mapping back as the top-level `outputs`
        (`[{name, column, jsonata?, source?}]`) on `view="compiled"`.
      - `advanced` — the runner-interpreted execution knobs, nested rather than
        flat, **every member optional**, and an omitted one means the runner's
        own configured default rather than a value written here:
        `rows_per_scan` (upper bound on rows one scan tick may
        claim), `rows_per_call` (rows evaluated per work item), `rate_rpm`
        (per-pipeline model-call ceiling per minute), and `sample_fraction`
        (fraction of eligible rows to evaluate, strictly in (0, 1]; a value
        above 1 is refused rather than read as a percentage, and 0 is refused
        naming `enabled: false`, which is how a pipeline that evaluates nothing
        is declared), and `input_token_budget` (**`group` trigger only**,
        refused on any other trigger even on a draft: the approximate token
        ceiling the runner trims a group's assembled input to — `0` turns
        trimming off, a positive value is this pipeline's ceiling, absent is
        the runner's default, negative is a 422).

    ### An aligned target

    An enrichment ADAS laid out **aligned** with its source (`layout: aligned`
    on `adas_get_table`, or `migrating` while ADAS relays an existing one out)
    can only be written by a row-trigger pipeline reading the target's own
    source. Arming an `enrich` pipeline onto one — either transform type — is
    refused with 422 `pipeline_validation_failed` when it declares another read
    source in `inputs` or uses the `group` trigger. Arming an `llm` pipeline
    onto a `migrating` target is additionally refused with **503**
    `table_relayout_in_progress` until the relayout finishes; a `sql` pipeline
    of its own source is accepted.

    Returns: the created `PipelineDto`, in the `source` projection (a write
      always answers what was authored, with undeclared members absent) —
      `enabled: false` for `aggregate`, the flag you sent (default `false`) for
      `enrich`. A write response may also carry `notes`:
      advisory, never a rejection, and present only here and on PATCH. Read
      them — they name costs the declaration passed validation with, such as a
      `json_extract_string_ci` over an `object` column, which reads one stream
      per JSON path per part.
      `{error: {code: "invalid_argument"}}` when a named table does not exist,
      when a cross-kind member is present, when `enabled: true` is sent on an
      `aggregate` body, or when an `enrich` body sent with `enabled: true`
      fails the gate (all 422, with the original status preserved in the
      envelope); `conflict` on a duplicate name or an already-written target;
      an armed `dial_app_route` transform whose `(model, route)` pair is not
      on the `app-routes` allowlist is one of those gate failures;
      `{error: {code: "internal_error", status: 503}}` with
      `details.error: "table_relayout_in_progress"` when arming an `llm`
      pipeline onto a `migrating` target — transient, retry once the relayout
      finishes.

    Related: adas_describe_schema, adas_preview_pipeline, adas_patch_pipeline,
      adas_get_table, adas_list_pipelines.

    Examples:
      body={"name": "usage_daily_rollup", "kind": "aggregate", "target": "usage_daily"}
      body={"name": "toxicity-live", "kind": "enrich", "target": "usage_toxicity"}
      body={"name": "usage_daily_rollup", "kind": "aggregate", "target": "usage_daily", "inputs": ["dial_usage_log"], "group_by": [{"trunc": {"column": "request_time", "unit": "day"}, "as": "day"}, {"column": "deployment"}], "measures": [{"name": "call_count", "fn": "count"}, {"name": "total_tokens", "fn": "sum", "column": "total_tokens"}], "trigger": {"kind": "schedule", "cron": "0 */10 * * * *"}}
      body={"name": "chat_daily", "kind": "aggregate", "target": "chat_daily", "inputs": ["dial_usage_log"], "group_by": [{"trunc": {"column": "request_time", "unit": "day"}, "as": "day"}], "measures": [{"name": "chat_count", "fn": "count", "column": "chat_id", "distinct": True}, {"name": "error_price", "fn": "sum", "column": "total_price", "where": "status <> 'ok'"}, {"name": "models", "fn": "group_uniq_array", "column": "deployment"}], "trigger": {"kind": "schedule", "cron": "0 */10 * * * *"}}
      body={"name": "usage_daily_sql", "kind": "aggregate", "target": "usage_daily", "sql": "INSERT INTO usage_daily (day, deployment, tokens) SELECT date_trunc('day', request_time), deployment, sum(total_tokens) FROM dial_usage_log GROUP BY 1, 2", "trigger": {"kind": "schedule", "cron": "0 */10 * * * *"}}
      body={"name": "toxicity-live", "kind": "enrich", "target": "usage_toxicity", "trigger": {"kind": "schedule"}, "transform": {"type": "llm", "model": "gemini-2.5-flash-lite", "request_template": "{\"messages\": [{\"role\": \"user\", \"content\": \"Rate: {{prompt}}\"}]}", "inputs": {"prompt": "request_body"}, "outputs": {"toxicity_score": "How toxic the prompt is, 0.0 to 1.0.", "risk_level": {"prose": "Overall risk band.", "values": ["low", "medium", "high"]}}}, "enabled": True}
      body={"name": "req-model-live", "kind": "enrich", "target": "usage_request_model", "trigger": {"kind": "schedule"}, "transform": {"type": "sql", "outputs": {"model": "json_extract_string(request_body, 'model')"}}, "enabled": True}
      body={"name": "conversation-insights-live", "kind": "enrich", "target": "conversation_insights", "inputs": ["dial_usage_log"], "trigger": {"kind": "group", "group_by": "chat_id", "ready_when": {"idle": "30m"}, "member_select": {"prefer_sql": "parent_deployment IS NULL", "order_by": [{"column": "number_request_messages", "direction": "desc"}], "limit": 1}}, "transform": {"type": "llm", "model": "gemini-2.5-flash-lite", "request_template": "{\"messages\": [{\"role\": \"user\", \"content\": \"Summarize: {{members}}\"}]}", "inputs": {"transcript": {"jsonata": "request_body.messages"}}, "outputs": {"title": "A short title for the conversation.", "topics": {}}}, "advanced": {"rows_per_scan": 1000, "rate_rpm": 75}, "enabled": False}
      body={"name": "conversation-intents-live", "kind": "enrich", "target": "conversation_intents", "inputs": ["dial_usage_log"], "trigger": {"kind": "group", "group_by": "chat_id", "ready_when": {"idle": "30m"}}, "transform": {"type": "llm", "preset": "dial_app_route", "model": "insights-app", "route": "insights", "request_template": "{\"members\": {{members:json}}}", "inputs": {"prompt": "request_body"}, "outputs": {"title": {"source": "summary.title"}, "intent_counts": {"source": "analysis.intent_counts"}}}, "advanced": {"input_token_budget": 8000}, "enabled": False}
    """

    if not isinstance(body, dict) or not body:
        return invalid_argument("body must be a non-empty object")
    try:
        return await get_client().request(
            "POST", "/v1/pipelines", json=body, ctx=ctx, require_auth=False
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_patch_pipeline(
    ctx: Context, name: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Enable/disable a pipeline, and/or re-declare it. Either kind.

    Two distinct uses, and mixing them up is the common failure:

      - **Flip it** — send only `{"enabled": true}` or `{"enabled": false}`.
        `enabled` is the pipeline's **single run control** for both kinds:
        disabling stops it and keeps its progress (the Enrichment Runner holds
        a stopped `enrich` pipeline's queued work), and a flag-only patch does
        **not** bump `generation` or reset the cursor — only `updated_at`
        moves. Enabling **arms** the pipeline, so it runs the full validation
        gate over the stored declaration (a draft is refused here with 422
        `pipeline_validation_failed` naming its first problem — preview first)
        and arms an ADAS-driven schedule.
      - **Re-declare it** — sending any declaration member makes it a
        declaration patch. Which members count: `inputs`, `filter`,
        `group_by`, `measures`, `sql`, `trigger`, `transform`, `advanced`, and
        a `target` that **differs** from the stored one. A declaration patch
        **replaces** every member except `target` (and an `aggregate`'s `sql`,
        below) — an omitted member is
        *cleared*, not merged (an `advanced` you do not resend is dropped) — and
        **resets the pipeline's cursor**: the next run recomputes from the start
        of history.

    **`target` is sticky.** Omit it and the stored target is kept; a different
    value repoints the pipeline. A blank `target` counts as omitted, and a
    `target` equal to the stored one is not a declaration on its own — beside
    `{"enabled": true}` it is still just an enable.

    How much of the declaration a patch must carry depends on whether the
    pipeline is **enabled after** the patch (the patch's own `enabled`, else the
    stored flag):
      - **Leaves it enabled** (re-declaring an enabled `enrich` pipeline, or
        re-declaring and enabling in one call) — the **whole declaration for
        the kind** must be supplied, and the gate runs over it:
          - `aggregate` — form mode: `inputs`, `measures` and `trigger`
            together (`group_by` stays optional, derived from the target's
            `ordering_key`); SQL mode: `sql` (sent, or the stored one kept)
            and `trigger`.
          - `enrich` — `transform` and `trigger` together. `advanced` is **not**
            required.
      - **Leaves it disabled** — a draft save; the gate does not run. While the
        stored declaration is still **incomplete**, the patch may supply any
        subset (it still replaces: what you omit is cleared). Once the stored
        declaration is **complete** (every member its kind authors), a disabled
        patch may replace it whole but **not leave it incomplete**: that is a
        422 naming the members it would drop. This is what stops a one-knob
        patch like `{"advanced": {...}}` on a stopped (disabled) pipeline from
        erasing its transform, trigger and inputs. To start a complete
        declaration over, register a new pipeline (deleting one is not exposed
        through this server).

    **An `enrich` pipeline that declares its own read source must restate
    `inputs` on every declaration patch**, enabled or not (422 otherwise) —
    omitting it would silently repoint the pipeline at its target's parent.
    The exception: when the declared source *is* the target's parent, an
    omitted `inputs` keeps it. Send `inputs: []` to drop the declared source
    deliberately.

    **An `aggregate` switches modes by what the patch carries**, never by
    what it omits: `sql` stores SQL mode and clears `group_by`/`measures`/
    `filter`; any of those three stores form mode and clears `sql`; a patch
    carrying neither (only `trigger`, `inputs`, `target`) keeps the stored mode
    and **keeps the stored `sql`** unchanged — the one replace-semantics
    exception besides `target`, so a new cron does not erase the statement. To
    clear a statement, send a form-mode declaration.
    A mode switch is a re-declaration like any other (409 while enabled; on a
    disabled complete pipeline it may not leave the declaration incomplete), and
    any patch carrying `sql` bumps `generation` and resets the cursor even if
    the text is unchanged. A patch's own `sql` is taken as sent, even one that
    does not parse yet.

    **`transform` replaces the stored one wholesale**, and that is the
    rule with teeth here: a member you omit is *cleared*, not retained. Two
    cases to get right on a declaration carrying `response_schema` or
    `output_vars` — omitting
    `transform.response_schema` clears the stored schema and switches the
    pipeline to a freshly composed one, which changes what a live prompt
    looks like (property order is generation order under `strict: true`);
    and omitting `transform.output_vars` on a declaration that has no
    `outputs` leaves it with no outputs at all. Both members are served on
    `view="source"` and accepted here precisely so a read-modify-write
    round-trips them unchanged, so read `source`, edit, send it back.
    (`output_vars` is accepted on `PATCH` and refused on `POST`; a body
    carrying both `outputs` and `output_vars` is a 422.)

    Whether a declaration patch is allowed while the pipeline is **enabled**
    depends on the kind:
      - `aggregate` — refused with 409. Its target holds windowed results
        replaced by key, so an edit mid-flight leaves rows computed two
        different ways with nothing recording which came from which. Disable it,
        patch, re-enable.
      - `enrich` — **permitted**. Its target is a grain-keyed replace-by-grain
        table where new rows computing the new way and old rows keeping their
        values is already how it behaves.

    `name` and `kind` are **accepted but not patchable** — send them if you are
    doing a read-modify-write of the whole body, but they must equal what the
    pipeline already has (422 otherwise). A pipeline's name is its identity, and
    a different transform over the same target is a different pipeline.
    Unrecognized members are a 400 naming the field.

    Aggregate `measures` take the same `{name, fn, column?, where?, distinct?}`
    shape `adas_create_pipeline` documents (and `group_by` is optional there
    too); the enrich `trigger`, `transform` and `advanced` vocabulary is
    documented there as well, including the `llm`/`sql` member split and
    `prose` being optional. The five derived members of `view="compiled"`
    (`response_schema`, `grain_key`, `version_column`, `outputs`, resolved
    `inputs`) are each a 400 naming the field — which is what stops a compiled
    body being posted back as a declaration.

    Returns: the updated `PipelineDto`, in the `source` projection — read it
      back with `adas_get_pipeline` with `view="compiled"` to see what the new
      declaration resolved to. May carry advisory `notes`, as POST does; they
      never mean the patch was rejected. Note `notes` is **response-only**, so
      echoing a write response straight back as a PATCH body is refused: send
      only the members you mean to change. `{error: {code: "not_found"}}` for an
      unknown name; `invalid_argument` (422) for an incomplete declaration
      patch that leaves the pipeline enabled, a gate failure on arming, a
      disabled patch that would leave a complete declaration incomplete, an
      omitted `inputs` on an `enrich` pipeline that declares a read source other
      than its target's parent, a
      cross-kind member, or a changed `name`/`kind`; `conflict` when
      re-declaring an enabled `aggregate` pipeline or when the new `target` is
      already written by another pipeline. Arming onto an aligned target follows
      `adas_create_pipeline`'s rules: 422 for another read source or a `group`
      trigger, 503 `table_relayout_in_progress` for an `llm` pipeline onto a
      `migrating` one.

    Related: adas_get_pipeline, adas_preview_pipeline, adas_create_pipeline.

    Examples:
      name="usage_daily_rollup", body={"enabled": True}
      name="toxicity-live", body={"enabled": False}
      name="usage_daily_rollup", body={"inputs": ["dial_usage_log"], "measures": [{"name": "call_count", "fn": "count"}]}
      name="usage_daily_sql", body={"trigger": {"kind": "schedule", "cron": "0 0 * * * *"}}
      name="usage_daily_rollup", body={"target": "usage_daily", "inputs": ["dial_usage_log"], "group_by": [{"column": "deployment"}], "measures": [{"name": "call_count", "fn": "count"}], "trigger": {"kind": "schedule", "cron": "0 */5 * * * *"}}
      name="toxicity-live", body={"target": "usage_toxicity", "trigger": {"kind": "schedule"}, "transform": {"type": "llm", "model": "gemini-2.5-flash-lite", "request_template": "{\"messages\": [{\"role\": \"user\", \"content\": \"Rate: {{prompt}}\"}]}", "inputs": {"prompt": "request_body"}, "outputs": {"toxicity_score": "How toxic the prompt is, 0.0 to 1.0."}}}
    """

    if not name:
        return invalid_argument("name is required")
    if not isinstance(body, dict) or not body:
        return invalid_argument("body must be a non-empty object")
    try:
        return await get_client().request(
            "PATCH",
            f"/v1/pipelines/{path_segment(name)}",
            json=body,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


async def adas_refresh_pipeline(ctx: Context, name: str) -> dict[str, Any]:
    """Refresh an aggregate pipeline now: run one bounded window immediately.

    **Aggregate pipelines only** — an `enrich` pipeline answers 422 here. Its
    windows are driven by the Enrichment Runner over the data plane this server
    does not wrap (`scan` / `materialize`), not by an operator call.

    Probes the window end, then recomputes in full every group key that window
    touched. **Idempotent** — repeating it writes the same values, which the
    target collapses.

    Requires the pipeline to be **enabled** (409 otherwise): a disabled pipeline
    never writes, which is what makes the `adas_preview_pipeline` gate
    meaningful.

    Drain a backlog by refreshing until `has_more` is false.

    Returns: `{rows_written, has_more, lag_seconds}`. `rows_written` counts
      group-key rows — one per recomputed key, not per input row.
      `{error: {code: "not_found"}}` for an unknown name;
      `{error: {code: "conflict"}}` when the pipeline is disabled;
      `{error: {code: "invalid_argument", status: 422}}` on an `enrich`
      pipeline.

    Related: adas_preview_pipeline, adas_patch_pipeline, adas_get_pipeline.

    Examples:
      name="usage_daily_rollup"
    """

    if not name:
        return invalid_argument("name is required")
    try:
        return await get_client().request(
            "POST",
            f"/v1/pipelines/{path_segment(name)}/refresh",
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_list_pipelines")(adas_list_pipelines)
    mcp.tool(name="adas_get_pipeline")(adas_get_pipeline)
    mcp.tool(name="adas_preview_pipeline")(adas_preview_pipeline)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_create_pipeline")(adas_create_pipeline)
    mcp.tool(name="adas_patch_pipeline")(adas_patch_pipeline)
    mcp.tool(name="adas_refresh_pipeline")(adas_refresh_pipeline)
