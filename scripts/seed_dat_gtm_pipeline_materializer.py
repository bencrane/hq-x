"""End-to-end exercise of the GTM-pipeline materializer slice against the
DAT fixture.

Per directive §8:

  1. Resolve the DAT initiative.
  2. Reset its pipeline state. Verify all 10 MAGS agents are
     registered.
  3. Reset materializer-derived state for this initiative (memberships,
     manifest, recipients tied only to this initiative). Campaigns +
     channel_campaigns + steps are kept idempotent — execute_channel_step_plan
     reuses the existing rows if they exist.
  4. Drive the pipeline through to terminal state, bypassing Trigger.dev
     by inlining the loop. The fanout step is faked via direct
     pipeline.run_step calls per (recipient, step) since we can't fan
     out without Trigger.dev locally.
  5. Pretty-print every run + the materialized state (campaign id, step
     count, recipient count, manifest count, dub link count, per-recipient
     run count).
  6. Persist a markdown summary to docs/initiatives-archive/<id>/materializer_e2e_<ts>.md.
  7. Exit 0 on completed / verdict_block_after_retries / fanout_high_failure_rate;
     non-zero only on prerequisite-missing or wall-clock timeout.

Set MATERIALIZER_AUDIENCE_LIMIT=10 (or similar) for fast dev runs.

Usage:
  MATERIALIZER_AUDIENCE_LIMIT=10 \\
      doppler --project hq-x --config dev run -- \\
      uv run python -m scripts.seed_dat_gtm_pipeline_materializer
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from app.services import (
    agent_prompts,
    gtm_pipeline as pipeline,
    gtm_initiatives as gtm_svc,
    org_doctrine,
)

DAT_INITIATIVE_ID = UUID("bbd9d9c3-c48e-4373-91f4-721775dca54e")
ACQ_ENG_ORG_ID = UUID("4482eb19-f961-48e1-a957-41939d042908")

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEPENDENT_BRAND_DOCTRINE = (
    REPO_ROOT / "data" / "brands" / "_meta" / "independent-brand-doctrine.md"
)

REQUIRED_AGENT_SLUGS = [
    "gtm-sequence-definer",
    "gtm-sequence-definer-verdict",
    "gtm-channel-step-materializer",
    "gtm-channel-step-materializer-verdict",
    "gtm-audience-materializer",
    "gtm-audience-materializer-verdict",
    "gtm-master-strategist",
    "gtm-master-strategist-verdict",
    "gtm-per-recipient-creative",
    "gtm-per-recipient-creative-verdict",
]

WALL_CLOCK_TIMEOUT_SEC = 60 * 60
MAX_VERDICT_RETRIES = 1
FANOUT_FAILURE_THRESHOLD = 0.5


def _conn_str() -> str:
    raw = os.environ.get("HQX_DB_URL_DIRECT") or os.environ.get("HQX_DB_URL_POOLED")
    if not raw:
        print(
            "HQX_DB_URL_DIRECT/POOLED not in env (run via doppler run --)",
            file=sys.stderr,
        )
        sys.exit(2)
    return raw


# ── prerequisite checks ─────────────────────────────────────────────────


async def _verify_initiative_exists() -> dict[str, Any]:
    initiative = await gtm_svc.get_initiative(DAT_INITIATIVE_ID)
    if initiative is None:
        print(
            f"ERROR: DAT initiative {DAT_INITIATIVE_ID} not found.",
            file=sys.stderr,
        )
        sys.exit(2)
    return initiative


async def _verify_agents_registered() -> dict[str, Any]:
    rows = await agent_prompts.list_registry_rows()
    by_slug = {r["agent_slug"]: r for r in rows}
    missing = [s for s in REQUIRED_AGENT_SLUGS if s not in by_slug]
    if missing:
        print(
            "ERROR: missing agents in business.gtm_agent_registry: "
            f"{missing}\nRun managed-agents-x/scripts/setup_gtm_agents.py "
            "for each slug first, then scripts/register_gtm_agent.py.",
            file=sys.stderr,
        )
        sys.exit(2)
    return by_slug


async def _verify_doctrine() -> None:
    doctrine = await org_doctrine.get_for_org(ACQ_ENG_ORG_ID)
    if doctrine is None or not doctrine.get("parameters"):
        print(
            "ERROR: business.org_doctrine row for acq-eng missing.",
            file=sys.stderr,
        )
        sys.exit(2)


def _verify_independent_brand_doctrine() -> None:
    if not INDEPENDENT_BRAND_DOCTRINE.is_file():
        print(
            f"ERROR: independent-brand doctrine missing on disk at "
            f"{INDEPENDENT_BRAND_DOCTRINE}",
            file=sys.stderr,
        )
        sys.exit(2)


# ── reset state ─────────────────────────────────────────────────────────


def _reset_initiative_state() -> None:
    """Set pipeline_status='idle', supersede prior runs, and clear the
    materializer-derived state so the seed runs cleanly. Reused
    recipients (created by other initiatives) are NOT touched.
    """
    with psycopg.connect(_conn_str()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE business.gtm_initiatives
                SET pipeline_status = 'idle', updated_at = NOW()
                WHERE id = %s
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            cur.execute(
                """
                UPDATE business.gtm_subagent_runs
                SET status = 'superseded',
                    completed_at = COALESCE(completed_at, NOW())
                WHERE initiative_id = %s
                  AND status IN ('queued', 'running', 'succeeded', 'failed')
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            superseded = cur.rowcount
            # Soft-reset memberships from this initiative — manifest +
            # step memberships of materialized direct_mail steps under
            # this initiative.
            cur.execute(
                """
                UPDATE business.initiative_recipient_memberships
                SET removed_at = NOW(), removed_reason = 'e2e_reset'
                WHERE initiative_id = %s AND removed_at IS NULL
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            manifest_removed = cur.rowcount
            cur.execute(
                """
                DELETE FROM business.channel_campaign_step_recipients r
                USING business.channel_campaign_steps s,
                      business.channel_campaigns cc
                WHERE r.channel_campaign_step_id = s.id
                  AND s.channel_campaign_id = cc.id
                  AND cc.initiative_id = %s
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            mem_removed = cur.rowcount
        conn.commit()
    print(
        f"reset: pipeline_status='idle', superseded {superseded} runs, "
        f"removed {manifest_removed} manifest rows, deleted {mem_removed} "
        f"step memberships"
    )


# ── snapshot materialized state ─────────────────────────────────────────


def _snapshot_materialized_state() -> dict[str, Any]:
    with psycopg.connect(_conn_str()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM business.campaigns WHERE initiative_id = %s",
                (str(DAT_INITIATIVE_ID),),
            )
            campaigns = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT id, channel FROM business.channel_campaigns "
                "WHERE initiative_id = %s",
                (str(DAT_INITIATIVE_ID),),
            )
            ccs = [(r[0], r[1]) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT s.id, cc.channel
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc ON cc.id = s.channel_campaign_id
                WHERE cc.initiative_id = %s
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            step_rows = [(r[0], r[1]) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT COUNT(DISTINCT recipient_id)
                FROM business.initiative_recipient_memberships
                WHERE initiative_id = %s AND removed_at IS NULL
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            manifest_count = (cur.fetchone() or (0,))[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM business.channel_campaign_step_recipients r
                JOIN business.channel_campaign_steps s ON s.id = r.channel_campaign_step_id
                JOIN business.channel_campaigns cc ON cc.id = s.channel_campaign_id
                WHERE cc.initiative_id = %s
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            membership_count = (cur.fetchone() or (0,))[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM dmaas_dub_links dl
                JOIN business.channel_campaign_steps s
                    ON s.id = dl.channel_campaign_step_id
                JOIN business.channel_campaigns cc
                    ON cc.id = s.channel_campaign_id
                WHERE cc.initiative_id = %s
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            dub_link_count = (cur.fetchone() or (0,))[0]
            cur.execute(
                """
                SELECT COUNT(*) FROM business.gtm_subagent_runs
                WHERE initiative_id = %s
                  AND agent_slug IN ('gtm-per-recipient-creative',
                                     'gtm-per-recipient-creative-verdict')
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            per_recipient_run_count = (cur.fetchone() or (0,))[0]

    dm_step_ids = [r[0] for r in step_rows if r[1] == "direct_mail"]
    return {
        "campaign_count": len(campaigns),
        "channel_campaign_count": len(ccs),
        "channels": sorted({c for _, c in ccs}),
        "step_count": len(step_rows),
        "dm_step_count": len(dm_step_ids),
        "dm_step_ids": dm_step_ids,
        "manifest_count": manifest_count,
        "step_membership_count": membership_count,
        "dub_link_count": dub_link_count,
        "per_recipient_run_count": per_recipient_run_count,
    }


# ── inline pipeline driver ──────────────────────────────────────────────


async def _run_pipeline() -> dict[str, Any]:
    upstream: dict[str, Any] = {}
    runs: list[dict[str, Any]] = []
    deadline = time.time() + WALL_CLOCK_TIMEOUT_SEC

    await pipeline.set_pipeline_status(
        DAT_INITIATIVE_ID, "running", bump_started_at=True,
    )

    for pair in pipeline.PIPELINE_STEPS:
        actor = pair["actor"]
        verdict = pair["verdict"]
        is_fanout = bool(pair.get("is_fanout"))

        if time.time() > deadline:
            return {
                "terminal_status": "wall_clock_timeout",
                "failed_at_slug": actor,
                "runs": runs,
            }

        if is_fanout:
            outcome = await _run_fanout(actor, verdict, upstream)
            runs.extend(outcome["runs"])
            if outcome["terminal"] is not None:
                await pipeline.set_pipeline_status(
                    DAT_INITIATIVE_ID,
                    "completed" if outcome["terminal"] == "completed" else "failed",
                )
                return {
                    "terminal_status": outcome["terminal"],
                    "failed_at_slug": actor if outcome["terminal"] != "completed" else None,
                    "runs": runs,
                }
            continue

        last_verdict: dict[str, Any] | None = None
        attempt = 0
        while attempt <= MAX_VERDICT_RETRIES:
            print(f"\n=== {actor} (attempt {attempt + 1}) ===")
            try:
                actor_run = await pipeline.run_step(
                    initiative_id=DAT_INITIATIVE_ID,
                    agent_slug=actor,
                    hint=last_verdict.get("redo_with") if attempt > 0 and last_verdict else None,
                    upstream_outputs=upstream,
                )
            except pipeline.RunStepError as exc:
                print(f"actor {actor} raised RunStepError: {exc}")
                runs.append({"agent_slug": actor, "status": "failed", "error": str(exc)})
                await pipeline.set_pipeline_status(DAT_INITIATIVE_ID, "failed")
                return {
                    "terminal_status": "actor_run_step_error",
                    "failed_at_slug": actor,
                    "runs": runs,
                }

            runs.append({
                "agent_slug": actor,
                "status": actor_run["status"],
                "run_index": actor_run["run_index"],
                "session_id": actor_run["anthropic_session_id"],
            })
            print(f"  -> status={actor_run['status']}")
            if actor_run["status"] != "succeeded":
                await pipeline.set_pipeline_status(DAT_INITIATIVE_ID, "failed")
                return {
                    "terminal_status": "actor_run_failed",
                    "failed_at_slug": actor,
                    "runs": runs,
                }
            actor_value = (actor_run["output_blob"] or {}).get("value")
            upstream[actor] = actor_value

            print(f"\n=== {verdict} ===")
            try:
                verdict_run = await pipeline.run_step(
                    initiative_id=DAT_INITIATIVE_ID,
                    agent_slug=verdict,
                    upstream_outputs=upstream,
                )
            except pipeline.RunStepError as exc:
                print(f"verdict {verdict} raised RunStepError: {exc}")
                runs.append({"agent_slug": verdict, "status": "failed", "error": str(exc)})
                await pipeline.set_pipeline_status(DAT_INITIATIVE_ID, "failed")
                return {
                    "terminal_status": "verdict_run_step_error",
                    "failed_at_slug": verdict,
                    "runs": runs,
                }

            verdict_value = (verdict_run["output_blob"] or {}).get("value") or {}
            runs.append({
                "agent_slug": verdict,
                "status": verdict_run["status"],
                "ship": verdict_value.get("ship") if isinstance(verdict_value, dict) else None,
            })
            if verdict_run["status"] != "succeeded":
                await pipeline.set_pipeline_status(DAT_INITIATIVE_ID, "failed")
                return {
                    "terminal_status": "verdict_run_failed",
                    "failed_at_slug": verdict,
                    "runs": runs,
                }
            last_verdict = verdict_value if isinstance(verdict_value, dict) else None
            if last_verdict and last_verdict.get("ship"):
                upstream[verdict] = verdict_value
                break
            attempt += 1

        if not last_verdict or not last_verdict.get("ship"):
            await pipeline.set_pipeline_status(DAT_INITIATIVE_ID, "failed")
            return {
                "terminal_status": "verdict_block_after_retries",
                "failed_at_slug": actor,
                "runs": runs,
            }

    await pipeline.set_pipeline_status(DAT_INITIATIVE_ID, "completed")
    return {
        "terminal_status": "completed",
        "failed_at_slug": None,
        "runs": runs,
    }


async def _run_fanout(
    actor: str, verdict: str, upstream: dict[str, Any],
) -> dict[str, Any]:
    """Fan out the per-recipient creative across every (recipient × DM step)
    by calling pipeline.run_step inline. This mirrors Trigger.dev's
    batchTriggerAndWait behavior in a single-process driver.
    """
    targets = await _resolve_fanout_targets()
    if not targets:
        return {
            "terminal": "fanout_targets_empty",
            "runs": [],
        }
    runs: list[dict[str, Any]] = []
    failures = 0
    for tgt in targets:
        recipient_id = UUID(tgt["recipient_id"])
        step_id = UUID(tgt["channel_campaign_step_id"])
        ok = True
        try:
            actor_run = await pipeline.run_step(
                initiative_id=DAT_INITIATIVE_ID,
                agent_slug=actor,
                upstream_outputs=upstream,
                recipient_id=recipient_id,
                channel_campaign_step_id=step_id,
            )
        except pipeline.RunStepError:
            ok = False
            actor_run = None
        if not actor_run or actor_run["status"] != "succeeded":
            ok = False
        if ok:
            try:
                verdict_run = await pipeline.run_step(
                    initiative_id=DAT_INITIATIVE_ID,
                    agent_slug=verdict,
                    upstream_outputs={
                        **upstream,
                        actor: (actor_run["output_blob"] or {}).get("value"),
                    },
                    recipient_id=recipient_id,
                    channel_campaign_step_id=step_id,
                )
            except pipeline.RunStepError:
                ok = False
                verdict_run = None
            if not ok or not verdict_run or verdict_run["status"] != "succeeded":
                ok = False
            elif not (
                isinstance((verdict_run["output_blob"] or {}).get("value"), dict)
                and (verdict_run["output_blob"] or {}).get("value", {}).get("ship")
            ):
                ok = False
        runs.append({
            "agent_slug": actor,
            "recipient_id": str(recipient_id),
            "step_id": str(step_id),
            "shipped": ok,
        })
        if not ok:
            failures += 1
    failure_rate = failures / max(1, len(targets))
    if failure_rate > FANOUT_FAILURE_THRESHOLD:
        return {
            "terminal": f"fanout_high_failure_rate:{int(failure_rate * 100)}%",
            "runs": runs,
        }
    return {"terminal": "completed", "runs": runs}


async def _resolve_fanout_targets() -> list[dict[str, str]]:
    """Local equivalent of /internal/gtm/initiatives/{id}/fanout-targets."""
    with psycopg.connect(_conn_str()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT output_blob FROM business.gtm_subagent_runs
                WHERE initiative_id = %s
                  AND agent_slug = 'gtm-channel-step-materializer'
                  AND status = 'succeeded'
                ORDER BY run_index DESC LIMIT 1
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            cs = cur.fetchone()
            if not cs:
                return []
            value = (cs[0] or {}).get("value") or {}
            executed = value.get("executed") if isinstance(value, dict) else None
            dm_step_ids = list((executed or {}).get("dm_step_ids") or [])
            if not dm_step_ids:
                return []
            cur.execute(
                """
                SELECT recipient_id FROM business.initiative_recipient_memberships
                WHERE initiative_id = %s AND removed_at IS NULL
                ORDER BY added_at
                """,
                (str(DAT_INITIATIVE_ID),),
            )
            recipients = [str(r[0]) for r in cur.fetchall()]
    return [
        {"recipient_id": r, "channel_campaign_step_id": s}
        for r in recipients
        for s in dm_step_ids
    ]


# ── markdown summary writer ─────────────────────────────────────────────


def _write_markdown_summary(
    result: dict[str, Any], snapshot: dict[str, Any],
) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = REPO_ROOT / "docs" / "initiatives-archive" / str(DAT_INITIATIVE_ID)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"materializer_e2e_{ts}.md"

    lines: list[str] = [
        f"# DAT GTM-pipeline materializer E2E — {ts}",
        "",
        f"Initiative: `{DAT_INITIATIVE_ID}`",
        f"Terminal status: **{result['terminal_status']}**",
    ]
    if result.get("failed_at_slug"):
        lines.append(f"Failed at: `{result['failed_at_slug']}`")
    lines.append("")
    lines.append("## Materialized state")
    lines.append("")
    for k, v in snapshot.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Runs")
    lines.append("")
    for i, run in enumerate(result["runs"], start=1):
        lines.append(f"- {i}. `{run.get('agent_slug')}` "
                     f"status={run.get('status') or run.get('shipped')}")
    out_path.write_text("\n".join(lines))
    return out_path


# ── main ────────────────────────────────────────────────────────────────


async def _amain() -> int:
    print("== verifying prerequisites ==")
    initiative = await _verify_initiative_exists()
    print(f"  initiative: id={initiative['id']}")
    by_slug = await _verify_agents_registered()
    for slug in REQUIRED_AGENT_SLUGS:
        row = by_slug[slug]
        print(f"  agent: {slug} -> {row['anthropic_agent_id']}")
    await _verify_doctrine()
    _verify_independent_brand_doctrine()
    print("\n== resetting initiative ==")
    _reset_initiative_state()
    print("\n== running pipeline ==")
    result = await _run_pipeline()
    print("\n== summary ==")
    print(f"terminal_status: {result['terminal_status']}")
    snapshot = _snapshot_materialized_state()
    for k, v in snapshot.items():
        print(f"  {k}: {v}")
    out_path = _write_markdown_summary(result, snapshot)
    print(f"\nwrote summary: {out_path.relative_to(REPO_ROOT)}")
    if result["terminal_status"] == "completed":
        print("\n== MATERIALIZER E2E PASSED ==")
        return 0
    if result["terminal_status"].startswith("verdict_block_after_retries"):
        print("\n== MATERIALIZER E2E PASSED (verdict-blocked, surface for iteration) ==")
        return 0
    if result["terminal_status"].startswith("fanout_high_failure_rate"):
        print(
            "\n== MATERIALIZER E2E PASSED (fanout failure-rate threshold), "
            "surface per-recipient prompt iteration target =="
        )
        return 0
    print(
        f"\n== MATERIALIZER E2E FAILED — terminal_status={result['terminal_status']} ==",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
