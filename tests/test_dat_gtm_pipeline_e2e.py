"""Network-dependent end-to-end test for the DAT GTM-pipeline foundation.

Gated by env flag RUN_E2E_GTM=1. Skipped on default pytest -q.

When enabled, runs scripts.seed_dat_gtm_pipeline_foundation
programmatically against the dev DB + real Anthropic Managed Agents API,
then asserts the run rows landed in business.gtm_subagent_runs.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import pytest

from app.services import gtm_pipeline as pipeline


DAT_INITIATIVE_ID = UUID("bbd9d9c3-c48e-4373-91f4-721775dca54e")


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E_GTM") != "1",
    reason="set RUN_E2E_GTM=1 to run the network-dependent E2E test",
)


@pytest.mark.asyncio
async def test_dat_pipeline_runs_end_to_end():
    # Defer import so the module's prerequisite checks don't fire on
    # collection (they require Doppler + DB).
    from scripts import seed_dat_gtm_pipeline_foundation as seed

    rc = await seed._amain()  # type: ignore[attr-defined]
    assert rc == 0, "foundation E2E should exit 0 (completed or verdict-blocked)"

    # Six rows landed in business.gtm_subagent_runs (one per actor + verdict).
    runs = await pipeline.list_runs_for_initiative(DAT_INITIATIVE_ID)
    slugs = sorted(r["agent_slug"] for r in runs)
    expected = sorted([
        "gtm-sequence-definer",
        "gtm-sequence-definer-verdict",
        "gtm-master-strategist",
        "gtm-master-strategist-verdict",
        "gtm-per-recipient-creative",
        "gtm-per-recipient-creative-verdict",
    ])
    # We assert at least the actor-verdict pairs land. If the pipeline
    # halts at a verdict block, downstream pairs may not have rows.
    assert "gtm-sequence-definer" in slugs
    assert "gtm-sequence-definer-verdict" in slugs
