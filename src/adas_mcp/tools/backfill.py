"""Data-sync backfill control plane (`DataSyncBackfillController`).

What a run failed to ingest is recorded in the durable failure ledger
(`tools/sync_failures.py`), not only in the run's in-memory status.

The two reads (`GET /{runId}` and the bare `GET` listing) need `REGISTRY_READ`
on the backend; the trigger needs `DATA_SYNC_OPERATE` and registers behind
`ADAS_CAPABILITIES=full` here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client, path_segment
from ..errors import MissingAuthError, invalid_argument, structured_error


async def adas_get_backfill_status(ctx: Context, run_id: str) -> dict[str, Any]:
    """Get the live status of a data-sync backfill run.

    Run state is held **in memory only**, in a runtime cache of the 5 most
    recently started runs per ADAS instance: a `run_id` returns `not_found`
    once 5 newer runs have started (a refused trigger never started, so it
    evicts nothing), after an ADAS restart or redeploy, or when
    the request lands on a different instance (a rolling-deploy overlap). An
    interrupted backfill does not resume on its own — re-trigger the affected
    range. Lost the run id? `adas_list_backfill_runs` lists the retained runs.

    Counts advance incrementally while a day is still being ingested, so
    progress is visible within a long day rather than only at day boundaries.

    Returns: `{runId, from, to, daysTotal, state, currentDate, daysCompleted,
      daysFailed, failedDates, failedBlobs, filesProcessed, rowsInserted,
      rowsSkipped, rowsDropped, startedAt, finishedAt}`. `state` is lowercase `running`,
      `completed` or `failed`; `currentDate` is set only while running and
      `finishedAt` only once terminal. `failedDates` lists the failed days as
      ISO `yyyy-MM-dd` dates in ascending order and is **omitted** (not empty)
      when no day failed; it names every day that was attempted and failed —
      including the day a run aborted on and a day refused for lack of disk
      space — and never an unattempted day. Re-run those dates, not the range.

      `failedBlobs` (always present, `0` when none) counts blobs that failed
      to read or map, or whose rows ClickHouse refused, across all days so
      far. Failures are isolated **per blob**: a bad blob does not stop its
      day, but a day with any failed blob is still counted in `daysFailed`
      and named in `failedDates`, because it is incomplete. The status does
      not say *which* blobs failed — the durable failure ledger does, and
      outlives this in-memory run: `adas_list_data_sync_failures` with
      `backfill_run_id` set to this run's id. Those failures are retried by
      the retry sweep or requeued with `adas_retry_data_sync_failures`, so
      re-running a whole day is not the only remedy.

      Per-day failures are isolated too: a failing day is counted and the rest of
      the range still runs. But a run can still **end early** — ClickHouse disk
      exhaustion stops it at the refused day, and an unexpected fault aborts it
      on the day in progress — and then it reports `failed` with the days after
      it unattempted, so `daysCompleted + daysFailed <= daysTotal` (the sum can
      equal the total when it stopped on the last day; the `failed` state alone
      carries the early stop). `completed` means the whole range was worked
      through, possibly with some `failedDates`; a run that worked through its
      range with **every** day failed reports `failed` instead, its counts
      summing to `daysTotal` with `daysCompleted` 0. Never read counts summing
      to `daysTotal` as proof that a `failed` run covered its range.
      `{error: {code: "not_found"}}` for an unknown (or evicted) run id.

    Related: adas_list_backfill_runs, adas_trigger_backfill,
      adas_list_data_sync_failures.

    Examples:
      run_id="f1d2c3b4-0000-4444-8888-aaaabbbbcccc"
    """

    if not run_id:
        return invalid_argument("run_id is required")
    return await get_client().request(
        "GET",
        f"/v1/data-sync/backfill/{path_segment(run_id)}",
        ctx=ctx,
        require_auth=False,
    )


# The wire codes ADAS binds `state` by — the lowercase spelling the status
# object reports. ADAS answers 400 for anything else (empty included), which is
# checked here rather than spent on a round trip.
_BACKFILL_STATES = ("running", "completed", "failed")


async def adas_list_backfill_runs(
    ctx: Context, state: str | None = None
) -> dict[str, Any]:
    """List the backfill runs this ADAS instance retains, newest started first.

    The way to find a run without its id — above all, whether one is active:
    `state="running"` answers "is a backfill running?" with 0 or 1 entries (only
    one run may be in flight). Each entry is exactly the status object
    `adas_get_backfill_status` returns, `failedDates` and all.

    A **runtime cache, not a history**: at most the 5 most recently started
    runs, cleared on an ADAS restart or redeploy, and per instance — during a
    rolling-update overlap it can omit a run the other pod holds, so an empty
    `state="running"` answer is not proof that no backfill runs anywhere. Not
    paginated.

    Args:
      state: optional filter, one of `running`, `completed`, `failed`
        (case-insensitive). Omit for every retained run.

    Returns: `{"runs": [...]}` — always present, an empty array when nothing
      matches. `{error: {code: "invalid_argument"}}` for an unknown or empty
      `state` (rejected here; ADAS itself answers 400 `bad_request`).

    Related: adas_get_backfill_status, adas_trigger_backfill.

    Examples:
      state="running"
      (no arguments — every retained run)
    """

    if state is not None:
        normalized = state.strip().lower()
        if normalized not in _BACKFILL_STATES:
            return invalid_argument(
                f"state must be one of {', '.join(_BACKFILL_STATES)}; got {state!r}"
            )
        state = normalized
    return await get_client().request(
        "GET",
        "/v1/data-sync/backfill",
        params={"state": state},
        ctx=ctx,
        require_auth=False,
    )


async def adas_trigger_backfill(
    ctx: Context, date_from: str, date_to: str
) -> dict[str, Any]:
    """Trigger an asynchronous backfill of DIAL usage logs over a UTC date range.

    Ingests every day in the **inclusive** range `[date_from, date_to]` through
    the same pipeline as the scheduled and poller paths. Returns immediately
    (202) with a run id — poll `adas_get_backfill_status` for progress.

    Overlapping a range that was already ingested is safe: the target table's
    `ReplacingMergeTree` engine collapses duplicates, and ADAS performs no
    already-ingested pre-check.

    Only one backfill may run at a time. Dates are UTC, ISO `yyyy-MM-dd`.
    (The wire fields are `from`/`to`; the parameters are named `date_from`/
    `date_to` because `from` is a Python keyword.)

    Returns: `{runId, from, to, days}` — the generated run id plus the resolved
      range. `{error: {code: "invalid_argument"}}` when a date is missing or
      unparseable, `date_from` is after `date_to`, `date_to` is in the future,
      or the span exceeds the configured maximum (`backfill.max-days`, default
      366). `{error: {code: "conflict", status: 409, details: {error:
      "backfill_conflict", ...}}}` when a run is already in progress on this
      instance — `code` is derived from the HTTP status, and ADAS's own stable
      code `backfill_conflict` is in `details.error`; the remedy is to wait for
      the running one (find it with `adas_list_backfill_runs`, `state="running"`)
      rather than retry. `{error: {code: "internal_error", status: 503}}` when
      backfill is disabled (`details.error` is `backfill_disabled`).

    Related: adas_get_backfill_status, adas_list_backfill_runs.

    Examples:
      date_from="2026-05-26", date_to="2026-06-24"
    """

    if not date_from:
        return invalid_argument("date_from is required")
    if not date_to:
        return invalid_argument("date_to is required")
    try:
        return await get_client().request(
            "POST",
            "/v1/data-sync/backfill",
            json={"from": date_from, "to": date_to},
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_get_backfill_status")(adas_get_backfill_status)
    mcp.tool(name="adas_list_backfill_runs")(adas_list_backfill_runs)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_trigger_backfill")(adas_trigger_backfill)
