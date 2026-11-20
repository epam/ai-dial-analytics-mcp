"""Data-sync failure ledger (`DataSyncFailuresController`).

ADAS records every log blob and every day that failed to ingest — on the
poller, the reconciliation run, a backfill or the retry sweep — in a durable
ledger that outlives runs and restarts. A transient failure is retried by a
scheduled sweep with exponential backoff; a deterministic one (or one out of
attempts) is **parked** until an operator requeues it. This is where to look
after a backfill reports `failedBlobs` or `failedDates`.

The listing needs `REGISTRY_READ` on the backend and registers always; the
requeue needs `DATA_SYNC_OPERATE` (as a backfill trigger does) and registers
behind `ADAS_CAPABILITIES=full` here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..client import get_client
from ..errors import MissingAuthError, invalid_argument, structured_error

# The wire codes ADAS binds, matched case-insensitively there. Anything else is
# a 400 naming the accepted values; checked here rather than spent on a round
# trip. `resolved` is deliberately not a state filter: resolved subjects are
# never listed.
_FAILURE_STATES = ("pending", "parked")
_FAILURE_KINDS = ("blob", "day", "records")
_MAX_LIMIT = 1000


def _normalized(
    value: str | None, field: str, accepted: tuple[str, ...]
) -> tuple[str | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    normalized = value.strip().lower()
    if normalized not in accepted:
        return value, invalid_argument(
            f"{field} must be one of {', '.join(accepted)}; got {value!r}"
        )
    return normalized, None


async def adas_list_data_sync_failures(
    ctx: Context,
    state: str | None = None,
    kind: str | None = None,
    reason: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    backfill_run_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """List the outstanding data-sync ingest failures, newest failure first.

    Every blob or day that failed to ingest and is not yet resolved —
    `pending` (the retry sweep will attempt it after its backoff) or `parked`
    (waits for a requeue: a deterministic failure, or one that ran out of
    attempts). Resolved subjects are never listed. Filters are optional and
    ANDed; `date_from`/`date_to` bound the subject's UTC `day` inclusively.
    (The wire parameters are `from`/`to`; renamed because `from` is a Python
    keyword.)

    A subject is one of three **kinds**: `blob` (a log blob that failed, by its
    full key), `day` (a reconciliation/backfill/retry day that failed as a
    whole, by ISO date), and `records` (a blob that *was* ingested but had
    lines skipped or records dropped — it shares its blob's key and can be
    outstanding beside a `blob` subject).

    Reason codes — deterministic, parked at once: `corrupt_gzip`,
    `mapping_error`, `blob_missing`, `rejected_by_server`, `skipped_records`
    (`records` only). Transient, retried: `read_error`, and for days
    `listing_failed`, `prepare_failed`, `write_failed`, `storage_full`.
    Messages never carry record content.

    Returns: `{failures: [{kind, subject, day, blob_last_modified?, state,
      failure_class, reason, message, path, attempts, first_failed_at,
      last_failed_at, next_attempt_at?, backfill_run_ids?}], truncated, total,
      by_state: {pending, parked}, retry_sweep: {enabled, max_per_sweep,
      next_sweep_at?}}`. `total` counts every match however many `limit`
      returned, and is exactly what a requeue with the same filters would
      requeue. `by_state` counts the matches **ignoring the `state` filter**,
      so one call answers both "how many wait for an operator" and "how many
      retry on their own". `retry_sweep.enabled: false` means pending subjects
      are not being retried at all. `path` is the ingest path of the latest
      failure (`poll`, `reconcile`, `backfill`, `retry`). No match is an empty
      `failures` with `total` 0. `{error: {code: "invalid_argument"}}` for an
      unknown `state`/`kind` or a `limit` outside 1..1000 (checked here), and
      from ADAS for an unparseable date, an inverted range, an empty
      parameter or a blank `backfill_run_id`; `{error: {code:
      "internal_error", status: 503}}` when ClickHouse cannot be read.

    Related: adas_retry_data_sync_failures, adas_get_backfill_status,
      adas_list_backfill_runs.

    Examples:
      (no arguments — the 100 newest outstanding failures)
      state="parked", reason="mapping_error"
      kind="records"
      backfill_run_id="00000000-0000-4000-8000-000000000001"
      date_from="2026-05-26", date_to="2026-06-24", limit=500
    """

    state, rejection = _normalized(state, "state", _FAILURE_STATES)
    if rejection is not None:
        return rejection
    kind, rejection = _normalized(kind, "kind", _FAILURE_KINDS)
    if rejection is not None:
        return rejection
    if limit is not None and not 1 <= limit <= _MAX_LIMIT:
        return invalid_argument(f"limit must be between 1 and {_MAX_LIMIT}")
    return await get_client().request(
        "GET",
        "/v1/data-sync/failures",
        params={
            "state": state,
            "kind": kind,
            "reason": reason,
            "from": date_from,
            "to": date_to,
            "backfill_run_id": backfill_run_id,
            "limit": limit,
        },
        ctx=ctx,
        require_auth=False,
    )


async def adas_retry_data_sync_failures(
    ctx: Context, body: dict[str, Any]
) -> dict[str, Any]:
    """Requeue outstanding data-sync failures so the next retry sweep attempts them.

    Every outstanding subject the body selects is requeued: it becomes
    `pending` with its attempts reset to zero, and the next sweep is due to
    attempt it. The typical use is after fixing what parked them — e.g. a
    mapping corrected and redeployed — so a deterministic failure gets
    another try. Requeueing works whether or not the sweep is enabled; with
    it disabled, a requeued subject stays `pending` until it is.

    `body` is an `IngestFailureRetryRequest` (look it up with
    `adas_describe_schema`, `type_name="IngestFailureRetryRequest"`); every
    member is optional and they are **ANDed**, but **at least one is
    required** — an empty body is refused (here, and by ADAS with 400) so it
    cannot requeue everything by accident:
      - `kind`, `state`, `reason`, `from`, `to`, `backfill_run_id` — the same
        filters as `adas_list_data_sync_failures` (here under their wire
        names `from`/`to`).
      - `subjects` — up to 1000 blob keys or ISO dates, matching a subject of
        any kind.
      - `targets` — up to 1000 exact `{kind, subject}` pairs. A blob's `blob`
        and `records` subjects share its key, so this is how to requeue one
        without the other.
    Subjects matching nothing outstanding (resolved ones included) are
    ignored and not counted. List with the same filters first: its `total`
    is the number this call will requeue.

    Returns: `{requeued, retry_sweep: {enabled, max_per_sweep,
      next_sweep_at?}}`. `{error: {code: "invalid_argument"}}` for an empty
      body (checked here), and from ADAS (400) for an unknown member, a bad
      filter value, or a null/blank `subjects` entry or malformed `targets`
      entry; `permission_denied` without `DATA_SYNC_OPERATE` under oidc.

    Related: adas_list_data_sync_failures, adas_trigger_backfill.

    Examples:
      body={"state": "parked", "reason": "mapping_error"}
      body={"targets": [{"kind": "records", "subject": "logs/2026/05/26/a.log.gz"}]}
      body={"backfill_run_id": "00000000-0000-4000-8000-000000000001", "state": "parked"}
      body={"subjects": ["2026-05-26", "2026-05-27"]}
    """

    if not isinstance(body, dict) or not body:
        return invalid_argument(
            "body must be a non-empty object: name at least one filter, "
            "subjects or targets"
        )
    try:
        return await get_client().request(
            "POST",
            "/v1/data-sync/failures/retry",
            json=body,
            ctx=ctx,
            require_auth=False,
        )
    except MissingAuthError as exc:  # pragma: no cover - require_auth=False
        return structured_error("unauthenticated", str(exc))


def register(mcp: FastMCP) -> None:
    mcp.tool(name="adas_list_data_sync_failures")(adas_list_data_sync_failures)


def register_full(mcp: FastMCP) -> None:
    mcp.tool(name="adas_retry_data_sync_failures")(adas_retry_data_sync_failures)
