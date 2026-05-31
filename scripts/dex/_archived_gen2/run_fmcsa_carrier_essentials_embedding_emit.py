#!/usr/bin/env python3
"""Embed FMCSA carrier_essentials profiles → Lance vector dataset (Phase 4).

Phase 4 of the multi-phase hq-all rebuild — the vector layer activation.
Reads ``fmcsa.carrier_essentials_lance``, composes a profile text per
carrier, calls OpenAI ``text-embedding-3-small``, writes the resulting
embeddings to a Lance dataset at::

    s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_embeddings_lance/

with columns::

    dot_number       string       PRIMARY KEY (matches source's typing)
    embedding_vector list<float32, 1536>
    content_hash     string       SHA-256 of profile_text (change detection)
    profile_text     string       composite human-readable carrier profile
    embedded_at      timestamp(us, tz='UTC')
    model_version    string       e.g. 'text-embedding-3-small'

Plus a BTREE on ``dot_number`` and an IVF_PQ vector index on
``embedding_vector`` (Lance's headline benefit — top-K NN in <100ms cold).

Eligibility filter
------------------
``status_code = 'A' AND power_units_int >= 1``

Out of scope today (next sweep cycle):
  - Inactive / paper carriers (status='I' or 'X')
  - Carriers with ``power_units_int < 1`` (paper companies with no fleet)
  - Phase 5 will also add inspection narratives + crash signals from the
    sibling Lance datasets.

Cost
----
~1.95M carriers × ~150 tokens/profile × $0.02/1M tokens ≈ $5-6 for the
initial backfill. Subsequent daily runs only re-embed carriers whose
profile changed (content_hash diff) — typically <0.5% per day, so ~$0.05/day.

Idempotency
-----------
Re-run safe. The pipeline reads the existing embeddings dataset and only
embeds new/changed carriers (by content_hash). First-emit creates the
dataset; subsequent runs merge.

Usage
-----
    # Dry run (sizes + cost estimate, no API calls):
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance python3 \\
        apps/data-engine-x/scripts/run_fmcsa_carrier_essentials_embedding_emit.py --dry-run

    # Apply (writes Lance + builds vector index):
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance python3 \\
        apps/data-engine-x/scripts/run_fmcsa_carrier_essentials_embedding_emit.py --apply

    # Apply with a row cap (one-time validation):
    doppler run --project hq-all --config prd -- \\
        uv run --with pylance python3 \\
        apps/data-engine-x/scripts/run_fmcsa_carrier_essentials_embedding_emit.py \\
        --apply --max-rows 5000
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.embedding_emit import (  # noqa: E402
    EmbeddingEmitConfig,
    run_embedding_emit,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger("fmcsa-carrier-essentials-embedding-emit")

SOURCE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance"
)
EMBEDDINGS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/"
    "carrier_essentials_embeddings_lance"
)
DATASET_SLUG = "fmcsa_carrier_essentials_embeddings_lance"

# Eligibility — what we embed today. Owner-operators with power_units>=1
# are included; explicit-inactive carriers are excluded.
ELIGIBILITY_FILTER = "status_code = 'A' AND power_units_int >= 1"

PROFILE_TEXT_COLUMNS = [
    "dot_number", "legal_name", "dba_name",
    "phy_city", "phy_state",
    "business_org_desc", "carrier_operation",
    "fleetsize", "power_units_int", "total_drivers_int",
    "mcs150_mileage_int",
    "hm_ind",
    "crgo_passengers", "crgo_liqgas", "crgo_coldfood",
    "crgo_motoveh", "crgo_drivetow",
    "safety_rating",
    "operating_radius_class", "specialty_class", "fleet_bucket",
]


# Human-readable maps for the codes FMCSA uses in carrier_essentials.
_CARRIER_OPERATION_MAP = {
    "A": "interstate operation",
    "B": "intrastate hazmat operation",
    "C": "intrastate non-hazmat operation",
}

_FLEET_BUCKET_MAP = {
    "owner_op": "owner-operator",
    "small": "small fleet",
    "mid": "mid-size fleet",
    "large": "large fleet",
    "unknown": "fleet size unknown",
}

_OPERATING_RADIUS_MAP = {
    "interstate": "operates interstate",
    "intrastate_non_haz": "operates intrastate non-hazmat",
    "intrastate_hazmat": "operates intrastate hazmat",
    "unknown": "operating radius unknown",
}

_SPECIALTY_LABELS = {
    "hazmat": "specializes in hazmat",
    "passenger": "specializes in passenger transport",
    "reefer": "specializes in refrigerated/cold-chain",
    "auto_transport": "specializes in auto transport",
    "tanker": "specializes in tanker / liquid bulk",
}

_SAFETY_RATING_MAP = {
    "S": "FMCSA safety rating: Satisfactory",
    "C": "FMCSA safety rating: Conditional",
    "U": "FMCSA safety rating: Unsatisfactory",
}


def compose_carrier_profile_text(r: dict[str, Any]) -> str:
    """Compose a free-text profile from one carrier_essentials row.

    Format: pipe-separated phrases, English-readable. Embedding models
    handle this format well — semantic clustering picks up the
    cargo/operation/fleet signals.

    Empty/null fields are silently skipped.
    """
    parts: list[str] = []

    legal = (r.get("legal_name") or "").strip()
    dba = (r.get("dba_name") or "").strip()
    if legal:
        if dba and dba.upper() != legal.upper():
            parts.append(f"Carrier: {legal} (DBA {dba})")
        else:
            parts.append(f"Carrier: {legal}")
    elif dba:
        parts.append(f"Carrier (DBA only): {dba}")
    else:
        return ""  # no usable name — skip

    city = (r.get("phy_city") or "").strip()
    state = (r.get("phy_state") or "").strip()
    if city and state:
        parts.append(f"based in {city}, {state}")
    elif state:
        parts.append(f"based in {state}")

    biz = (r.get("business_org_desc") or "").strip()
    if biz:
        parts.append(f"business org type: {biz.lower()}")

    op = (r.get("carrier_operation") or "").strip().upper()
    if op in _CARRIER_OPERATION_MAP:
        parts.append(_CARRIER_OPERATION_MAP[op])

    radius = (r.get("operating_radius_class") or "").strip()
    if radius and radius != "unknown":
        parts.append(_OPERATING_RADIUS_MAP.get(radius, radius))

    bucket = (r.get("fleet_bucket") or "").strip()
    if bucket:
        parts.append(_FLEET_BUCKET_MAP.get(bucket, bucket))

    pu = r.get("power_units_int")
    td = r.get("total_drivers_int")
    if pu is not None and pu > 0:
        parts.append(f"{pu} power units")
    if td is not None and td > 0:
        parts.append(f"{td} drivers")

    mileage = r.get("mcs150_mileage_int")
    if mileage is not None and mileage > 0:
        # Format in millions for readability.
        if mileage >= 1_000_000:
            parts.append(f"~{mileage // 1_000_000}M annual VMT")
        elif mileage >= 1_000:
            parts.append(f"~{mileage // 1_000}K annual VMT")
        else:
            parts.append(f"{mileage} annual VMT")

    specialty = (r.get("specialty_class") or "").strip()
    if specialty in _SPECIALTY_LABELS:
        parts.append(_SPECIALTY_LABELS[specialty])

    hm = (r.get("hm_ind") or "").strip().upper()
    if hm == "Y":
        parts.append("hauls hazardous materials")

    cargo_flags = []
    if (r.get("crgo_passengers") or "").strip().upper() == "X":
        cargo_flags.append("passengers")
    if (r.get("crgo_liqgas") or "").strip().upper() == "X":
        cargo_flags.append("liquids/gases")
    if (r.get("crgo_coldfood") or "").strip().upper() == "X":
        cargo_flags.append("refrigerated food")
    if (r.get("crgo_motoveh") or "").strip().upper() == "X":
        cargo_flags.append("motor vehicles")
    if (r.get("crgo_drivetow") or "").strip().upper() == "X":
        cargo_flags.append("driveaway/towaway")
    if cargo_flags:
        parts.append("hauls: " + ", ".join(cargo_flags))

    rating = (r.get("safety_rating") or "").strip().upper()
    if rating in _SAFETY_RATING_MAP:
        parts.append(_SAFETY_RATING_MAP[rating])

    return " | ".join(parts)


def build_config(max_rows: int | None, build_vector_index: bool) -> EmbeddingEmitConfig:
    return EmbeddingEmitConfig(
        dataset_slug=DATASET_SLUG,
        source_lance_uri=SOURCE_LANCE_URI,
        embeddings_lance_uri=EMBEDDINGS_LANCE_URI,
        primary_key_column="dot_number",
        eligibility_filter=ELIGIBILITY_FILTER,
        profile_text_columns=PROFILE_TEXT_COLUMNS,
        profile_text_fn=compose_carrier_profile_text,
        build_vector_index=build_vector_index,
        max_rows=max_rows,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true",
                     help="run the full pipeline, embed + write Lance")
    grp.add_argument("--dry-run", action="store_true",
                     help="size the candidate set and compose 5 sample "
                          "profile texts; no API calls")
    ap.add_argument(
        "--max-rows", type=int, default=None,
        help="cap on rows to embed this run (for cost-bounded validation)",
    )
    ap.add_argument(
        "--no-vector-index", action="store_true",
        help="skip IVF_PQ index build (useful for small/test runs)",
    )
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64
    provider = os.environ.get("EMBEDDING_PROVIDER", "openai")
    if not args.dry_run and provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            LOG.error(
                "FAIL: OPENAI_API_KEY not set; either set it in Doppler "
                "hq-all/prd, or set EMBEDDING_PROVIDER=sentence-transformers."
            )
            return 64
        if os.environ.get("OPENAI_API_KEY") == "test":
            LOG.error("FAIL: OPENAI_API_KEY is the 'test' placeholder")
            return 64

    config = build_config(
        max_rows=args.max_rows,
        build_vector_index=not args.no_vector_index,
    )

    if args.dry_run:
        # Sample the eligibility filter + show 5 composed profile texts.
        import lance

        storage = {
            "aws_endpoint": os.environ["R2_ENDPOINT"],
            "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
            "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
            "aws_region": "us-east-1",
            "aws_virtual_hosted_style_request": "false",
        }
        ds = lance.dataset(SOURCE_LANCE_URI, storage_options=storage)
        n = ds.count_rows(filter=ELIGIBILITY_FILTER)
        cost = n * 150 / 1_000_000 * 0.02
        LOG.info("DRY RUN — candidates: %d", n)
        LOG.info("  estimated tokens: ~%d (~%.0f tokens/profile)",
                 n * 150, 150)
        LOG.info("  estimated cost:   $%.2f (at $0.02/1M tokens)", cost)
        LOG.info("  model:            %s", config.profile_text_fn.__module__)
        sample = ds.to_table(
            columns=list({config.primary_key_column,
                          *config.profile_text_columns}),
            filter=ELIGIBILITY_FILTER,
            limit=5,
        ).to_pylist()
        LOG.info("--- 5 sample profile texts ---")
        for r in sample:
            text = compose_carrier_profile_text(r)
            LOG.info("DOT %s: %s", r.get("dot_number"), text)
        return 0

    metrics = run_embedding_emit(config)
    LOG.info("=" * 60)
    LOG.info("DONE — metrics: %s", metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
