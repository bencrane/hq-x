"""End-to-end exercise of the GTM-pipeline foundation against the DAT
fixture.

Per directive §10:

  1. Resolve the DAT initiative (id bbd9d9c3-c48e-4373-91f4-721775dca54e).
  2. Reset its pipeline_status to 'idle' and any prior gtm_subagent_runs
     rows to 'superseded'.
  3. Verify the six MAGS agents are registered.
  4. Verify acq-eng org_doctrine row exists with non-empty parameters.
  5. Verify the independent-brand doctrine .md is on disk.
  6. Drive the pipeline through to terminal state. We bypass Trigger.dev
     for ergonomics — call gtm_pipeline.run_step + the verdict loop
     inline so an operator can debug locally without a Trigger.dev
     deployment. Behavior is identical because the workflow is just a
     loop around /run-step calls.
  7. Pretty-print every run's slug / status / cost / parsed output.
  8. Persist a markdown summary to docs/initiatives-archive/<id>/foundation_e2e_<ts>.md.
  9. Exit 0 if pipeline completes OR fails-via-verdict (both are
     successful foundation smoke runs — they prove the seam works).
     Exit non-zero only on prerequisite-missing, network errors, or
     wall-clock timeout.

Usage:
  doppler --project hq-x --config dev run -- \\
      uv run python -m scripts.seed_dat_gtm_pipeline_foundation
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
    "gtm-master-strategist",
    "gtm-master-strategist-verdict",
    "gtm-per-recipient-creative",
    "gtm-per-recipient-creative-verdict",
]

WALL_CLOCK_TIMEOUT_SEC = 15 * 60
MAX_VERDICT_RETRIES = 1  # mirror src/trigger/gtm-run-initiative-pipeline.ts


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
            f"ERROR: DAT initiative {DAT_INITIATIVE_ID} not found. "
            "Run scripts/seed_dat_gtm_initiative.py first.",
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
            "ERROR: business.org_doctrine row for acq-eng "
            f"({ACQ_ENG_ORG_ID}) missing or has empty parameters.\n"
            "Run scripts/sync_org_doctrine.py acq-eng first.",
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


# ── reset + supersede prior runs ────────────────────────────────────────


def _reset_initiative_state() -> None:
    """Set initiative.pipeline_status='idle' and supersede every prior
    gtm_subagent_runs row for this initiative."""
    with psycopg.connect(_conn_str()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE business.gtm_initiatives
                SET pipeline_status = 'idle',
                    updated_at = NOW()
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
            n = cur.rowcount
        conn.commit()
    print(f"reset: pipeline_status='idle', superseded {n} prior runs")


# ── inline pipeline driver (mirrors the Trigger workflow) ───────────────


async def _run_pipeline() -> dict[str, Any]:
    """Inline driver — same loop as src/trigger/gtm-run-initiative-pipeline.ts.

    Returns ``{terminal_status, failed_at_slug, runs}``.
    """
    upstream: dict[str, Any] = {}
    runs: list[dict[str, Any]] = []
    deadline = time.time() + WALL_CLOCK_TIMEOUT_SEC

    await pipeline.set_pipeline_status(
        DAT_INITIATIVE_ID, "running", bump_started_at=True,
    )

    for pair in pipeline.PIPELINE_STEPS:
        actor = pair["actor"]
        verdict = pair["verdict"]
        if time.time() > deadline:
            return {
                "terminal_status": "wall_clock_timeout",
                "failed_at_slug": actor,
                "runs": runs,
            }

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
                runs.append({
                    "agent_slug": actor,
                    "status": "failed",
                    "error": str(exc),
                })
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
                "output_summary": _summarize_output(actor, actor_run["output_blob"]),
            })
            print(
                f"  -> status={actor_run['status']} "
                f"session={actor_run['anthropic_session_id']}"
            )
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
                runs.append({
                    "agent_slug": verdict,
                    "status": "failed",
                    "error": str(exc),
                })
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
                "run_index": verdict_run["run_index"],
                "session_id": verdict_run["anthropic_session_id"],
                "ship": verdict_value.get("ship") if isinstance(verdict_value, dict) else None,
                "issues": verdict_value.get("issues") if isinstance(verdict_value, dict) else None,
                "redo_with": verdict_value.get("redo_with") if isinstance(verdict_value, dict) else None,
            })
            print(
                f"  -> status={verdict_run['status']} "
                f"ship={verdict_value.get('ship') if isinstance(verdict_value, dict) else 'unknown'} "
                f"session={verdict_run['anthropic_session_id']}"
            )
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


def _summarize_output(slug: str, output_blob: dict[str, Any] | None) -> Any:
    if not output_blob:
        return None
    val = output_blob.get("value")
    if isinstance(val, dict):
        # JSON output — return top-level keys + a few headline values.
        keys = list(val.keys())[:10]
        head = {}
        if "decision" in val:
            head["decision"] = val["decision"]
        if "ship" in val:
            head["ship"] = val["ship"]
        if "total_estimated_outlay_cents" in val:
            head["total_estimated_outlay_cents"] = val["total_estimated_outlay_cents"]
        if "projected_margin_pct" in val:
            head["projected_margin_pct"] = val["projected_margin_pct"]
        return {"keys": keys, "head": head}
    if isinstance(val, str):
        return {"chars": len(val), "first_200": val[:200]}
    return val


# ── markdown summary writer ─────────────────────────────────────────────


def _write_markdown_summary(result: dict[str, Any]) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = REPO_ROOT / "docs" / "initiatives-archive" / str(DAT_INITIATIVE_ID)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"foundation_e2e_{ts}.md"

    lines: list[str] = [
        f"# DAT GTM-pipeline foundation E2E — {ts}",
        "",
        f"Initiative: `{DAT_INITIATIVE_ID}`",
        f"Terminal status: **{result['terminal_status']}**",
    ]
    if result.get("failed_at_slug"):
        lines.append(f"Failed at: `{result['failed_at_slug']}`")
    lines.extend(["", "## Runs", ""])

    for i, run in enumerate(result["runs"], start=1):
        lines.append(f"### {i}. `{run['agent_slug']}`")
        lines.append(f"- status: **{run['status']}**")
        if run.get("run_index") is not None:
            lines.append(f"- run_index: {run['run_index']}")
        if run.get("session_id"):
            lines.append(f"- session_id: `{run['session_id']}`")
        if "ship" in run:
            lines.append(f"- ship: **{run['ship']}**")
        if run.get("issues"):
            lines.append("- issues:")
            for issue in run["issues"]:
                lines.append(
                    f"  - **{issue.get('severity', '?')}** "
                    f"`{issue.get('area', '?')}` — {issue.get('detail', '')}"
                )
        if run.get("redo_with"):
            lines.append(f"- redo_with: {run['redo_with']}")
        if run.get("output_summary"):
            lines.append(f"- output_summary: ```\n{json.dumps(run['output_summary'], indent=2, default=str)}\n```")
        if run.get("error"):
            lines.append(f"- error: `{run['error']}`")
        lines.append("")

    out_path.write_text("\n".join(lines))
    return out_path


# ── main ────────────────────────────────────────────────────────────────


async def _amain() -> int:
    print("== verifying prerequisites ==")
    initiative = await _verify_initiative_exists()
    print(
        f"  initiative: id={initiative['id']} "
        f"status={initiative['status']} "
        f"brand_id={initiative['brand_id']} "
        f"partner_id={initiative['partner_id']}"
    )

    by_slug = await _verify_agents_registered()
    for slug in REQUIRED_AGENT_SLUGS:
        row = by_slug[slug]
        print(
            f"  agent: {slug} -> {row['anthropic_agent_id']} "
            f"({row['role']}, model={row['model']})"
        )

    await _verify_doctrine()
    print(f"  doctrine: acq-eng row present")

    _verify_independent_brand_doctrine()
    print(f"  independent-brand doctrine: {INDEPENDENT_BRAND_DOCTRINE.exists()}")

    print("\n== resetting initiative ==")
    _reset_initiative_state()

    print("\n== running pipeline ==")
    result = await _run_pipeline()

    print("\n== summary ==")
    print(f"terminal_status: {result['terminal_status']}")
    if result.get("failed_at_slug"):
        print(f"failed_at_slug: {result['failed_at_slug']}")
    for run in result["runs"]:
        print(
            f"  {run['agent_slug']:<42} status={run['status']:<10}",
            end="",
        )
        if "ship" in run:
            print(f" ship={run['ship']}", end="")
        if run.get("error"):
            print(f" error={run['error'][:80]}", end="")
        print()

    out_path = _write_markdown_summary(result)
    print(f"\nwrote summary: {out_path.relative_to(REPO_ROOT)}")

    if result["terminal_status"] in {"completed", "verdict_block_after_retries"}:
        if result["terminal_status"] == "completed":
            print(
                "\n== FOUNDATION E2E PASSED — pipeline ran end-to-end and "
                "every verdict shipped =="
            )
        else:
            print(
                "\n== FOUNDATION E2E PASSED (verdict-blocked) — pipeline ran "
                "end-to-end, all rows persisted, surfaces a real iteration target =="
            )
        return 0

    print(
        f"\n== FOUNDATION E2E FAILED — terminal_status={result['terminal_status']} ==",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
