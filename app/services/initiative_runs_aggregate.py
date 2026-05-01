"""Per-initiative subagent-runs aggregate.

Powers ``GET /api/v1/admin/initiatives/{id}/runs/aggregated``. Returns
per-step, per-status counts plus fanout-aware ``expected_count`` /
``completed_count`` / ``failed_count`` so the frontend can render the
fanout view without paging through 5,000+ raw rows.

For non-fanout agents the response carries the per-status breakdown only
(`is_fanout: false`, ``expected_count: 0``).

For fanout agents the expected count is read from the most recent
succeeded ``gtm-audience-materializer`` run's ``executed.recipient_count``
× number of materialized DM steps. ``completed_count`` is the number of
``succeeded`` rows; ``failed_count`` is the ``failed`` rows. The other
status buckets are surfaced as ``other`` for the frontend.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.services.gtm_pipeline import PIPELINE_STEPS

_FANOUT_SLUGS = {
    pair["actor"] for pair in PIPELINE_STEPS if pair.get("is_fanout")
} | {pair["verdict"] for pair in PIPELINE_STEPS if pair.get("is_fanout")}


async def aggregate_runs(initiative_id: UUID) -> dict[str, Any]:
    """Return per-agent aggregate counts for an initiative's run history.

    Shape:
        {
            "<agent_slug>": {
                total_runs: int,
                latest_run_index: int,
                by_status: {queued, running, succeeded, failed, superseded},
                fanout: {
                    is_fanout: bool,
                    expected_count: int,
                    completed_count: int,
                    failed_count: int
                }
            },
            ...
        }
    """
    by_status_per_slug: dict[str, dict[str, Any]] = {}
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT agent_slug, status, COUNT(*) AS n,
                       MAX(run_index) AS latest_run_index
                FROM business.gtm_subagent_runs
                WHERE initiative_id = %s
                GROUP BY agent_slug, status
                """,
                (str(initiative_id),),
            )
            rows = await cur.fetchall()

    for slug, status_value, n, latest_run_index in rows:
        bucket = by_status_per_slug.setdefault(
            slug,
            {
                "total_runs": 0,
                "latest_run_index": 0,
                "by_status": {
                    "queued": 0,
                    "running": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "superseded": 0,
                },
            },
        )
        bucket["total_runs"] += int(n)
        bucket["by_status"][status_value] = int(n)
        if latest_run_index is not None:
            bucket["latest_run_index"] = max(
                bucket["latest_run_index"], int(latest_run_index)
            )

    expected_fanout_count = await _expected_fanout_count(initiative_id)

    out: dict[str, Any] = {}
    # Ensure every pipeline slug appears in the response, even with
    # zero runs, so the frontend can render the placeholder steps.
    all_slugs: list[str] = []
    for pair in PIPELINE_STEPS:
        all_slugs.append(pair["actor"])
        all_slugs.append(pair["verdict"])

    for slug in all_slugs:
        bucket = by_status_per_slug.get(
            slug,
            {
                "total_runs": 0,
                "latest_run_index": 0,
                "by_status": {
                    "queued": 0,
                    "running": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "superseded": 0,
                },
            },
        )
        is_fanout = slug in _FANOUT_SLUGS
        out[slug] = {
            **bucket,
            "fanout": {
                "is_fanout": is_fanout,
                "expected_count": expected_fanout_count if is_fanout else 0,
                "completed_count": bucket["by_status"].get("succeeded", 0)
                if is_fanout else 0,
                "failed_count": bucket["by_status"].get("failed", 0)
                if is_fanout else 0,
            },
        }

    return out


async def _expected_fanout_count(initiative_id: UUID) -> int:
    """Number of fanout invocations the pipeline expects for the latest
    succeeded audience materializer run.

    expected = recipient_count × len(dm_step_ids)

    Looks at the most recent succeeded ``gtm-audience-materializer`` row
    whose output_blob carries ``value.executed.recipient_count`` and
    cross-references the upstream channel-step-materializer for the
    DM-step count. Returns 0 if either is missing.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT output_blob
                FROM business.gtm_subagent_runs
                WHERE initiative_id = %s
                  AND agent_slug = 'gtm-audience-materializer'
                  AND status = 'succeeded'
                ORDER BY run_index DESC
                LIMIT 1
                """,
                (str(initiative_id),),
            )
            audience_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT output_blob
                FROM business.gtm_subagent_runs
                WHERE initiative_id = %s
                  AND agent_slug = 'gtm-channel-step-materializer'
                  AND status = 'succeeded'
                ORDER BY run_index DESC
                LIMIT 1
                """,
                (str(initiative_id),),
            )
            channel_row = await cur.fetchone()

    if not audience_row or not channel_row:
        return 0

    recipient_count = _dig(
        audience_row[0], "value", "executed", "recipient_count", default=0
    )
    dm_step_ids = _dig(
        channel_row[0], "value", "executed", "dm_step_ids", default=[]
    )
    if not isinstance(recipient_count, int) or not isinstance(dm_step_ids, list):
        return 0
    return recipient_count * len(dm_step_ids)


def _dig(blob: Any, *keys: str, default: Any = None) -> Any:
    cur = blob
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur is not None else default


__all__ = ["aggregate_runs"]
