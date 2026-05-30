"""s1 - UCC CA debtor × CA SoS entities owner-identity bridge (Pattern B).

**v2.0.0 (address-axis composite):** the bridge now joins on legal-name +
state exact-equality AND also checks whether the UCC debtor's normalized
physical address agrees with any of the SoS entity's three address roles
(``principal``, ``principal_in_ca``, ``mailing``). Address agreement promotes
the name-axis fan-out tier; absence holds or demotes it:

    platinum  ← name 1:1 AND address_agrees
    gold      ← (name 1:1 AND no address) OR (name 1:N|N:1 AND address_agrees)
    silver    ← everything else (name N:M or no address corroboration on
                non-1:1 name matches)
    rejected  ← either side's fan-out > COLLISION_THRESHOLD (unchanged)

The legacy v1.0.0 fan-out-only tier is preserved in column
``name_confidence_tier``; the new composite tier is in ``confidence_tier``
(and also ``composite_confidence_tier`` for explicitness). Downstream
consumers filtering on ``confidence_tier='platinum'`` will see a smaller,
more precise cohort. Schema additions in v2.0.0:

    debtor_address_base_normalized                  (left-side, pre-baked)
    sos_principal_address_base_normalized           (right-side, pre-baked)
    sos_principal_address_in_ca_base_normalized
    sos_mailing_address_base_normalized
    address_agrees                       BOOLEAN
    address_match_path                   VARCHAR — pipe-delimited role(s) that matched, or NULL
    address_match_value                  VARCHAR — the matching normalized address, or NULL
    name_confidence_tier                 VARCHAR — legacy v1.0.0 tier rule
    composite_confidence_tier            VARCHAR — same as confidence_tier

Method: ``legal_name_state_exact_ca_with_address_corroboration`` v1.0.0 (NEW
method registered by this script — NOT a reuse of legal_name_state_exact_ca
v1.0.0 since the address-axis input columns differ).

Pattern B exact-match bridge: UCC CA debtors filtered to
``DEBTOR_TYPE='Organization'`` (debtor legal name from filings) ×
CA SoS entities (``entity_name_normalized`` produced by PR #464). Normalizer
is canonical ``scripts._lib.entity_name_normalize`` on both sides for names
and ``scripts._lib.address_normalize`` on both sides for addresses (the
address columns are pre-baked into the source spines per PR #803).

Bridge version: 2.0.0 (was 1.0.0 — legal_name_state_exact_ca name-only).
COLLISION_THRESHOLD = 50 (rows where either side's fan-out exceeds this are
rejected — N:M up to 50×50 = 2,500-row joins per matched name).
MIN_ROWS_MATCHED = 2_000_000 (validator-calibrated 2026-05-18 post full-corpus
baseline probe — floor set at ~33% headroom vs observed 3,062,504 non-rejected
rows: platinum=390,637; gold=1,535,461; silver=1,136,406; rejected=359,765).

Inputs:
    s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance
        (filter pc.field('DEBTOR_TYPE') == 'Organization'; normalize
        ORG_NAME via _lib/entity_name_normalize.normalize_entity_name)
    s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance
        (entity_name_normalized is_valid; produced by PR #464)

Output:
    s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_debtor_sos_ca_owner_lance
    (dual BTREE on debtor_name_normalized AND entity_num)

Normalizer (validator p1 — PR #459/#460 root cause):
    ONLY ``scripts._lib.entity_name_normalize`` on both sides. The legacy
    UCC-specific normalizer (terminal-only suffix-strip semantics) diverges
    from the canonical global suffix-strip and caused the UCC × Overture ×
    PDL bridge reverts (PR #459/#460, both reverted 2026-05-15).
    Canonical reference: ``build_bridge_sba_sos_ca_owner_lance.py`` (PR #464).

Match method REUSE (validator p4 / L21):
    register_bridge                          → ops.bridges                (idempotent)
    start_bridge_run                         → ops.bridge_generation_runs (status=running)
    write Lance + BTREE + tier counts
    complete_bridge_run                      → status=completed + metrics
    fail_bridge_run (on shortfall or dry-run) → status=failed + error
    (Method-definition and method-version-definition helpers are NOT called
    — those rows are SHARED with sba_sos_ca_owner from PR #464; an UPSERT
    here would corrupt the SBA × SoS bridge's provenance.)

Tier rule:
    platinum = 1:1
    gold     = 1:N | N:1
    silver   = N:M (both ≤ 50)
    rejected = >50 on either side

Modal hosting (validator p2 / contract §13-16): @app.function(cpu=8, memory=32768, timeout=10800)
Memory 32 GB matches PR #464/#466's empirically-validated shape; defeats the
DataFusion sort-pool OOM that BTREE creation triggers on multi-million-row
inputs. Option B follow-up to paused predecessor Option A (local RAM < 16 GB).

Run via (Option B.a — contract §17):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      modal run --detach scripts/build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py::main --apply
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import modal

# Marker — the actual imports live inside emit() scoped to Modal:
#   from scripts._lib.entity_name_normalize import (
#       __version__ as NORMALIZER_VERSION, normalize_entity_name,
#   )
#   from scripts._lib.lance_commit_lock import lance_commit_lock
#   from scripts._lib.match_method_registry import (
#       register_bridge, start_bridge_run, complete_bridge_run, fail_bridge_run,
#   )
#   NOTE: method-definition + method-version-definition helpers are
#         INTENTIONALLY NOT IMPORTED (validator p4 / L21 — REUSE not redefine).

# ---------------------------------------------------------------------------
# Modal app + image (verbatim from PR #466 L93-112, swap lender→debtor)
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-ucc-ca-debtor-sos-ca-owner-lance")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "duckdb",
        "psycopg[binary]",
        "pylance>=0.20",
        "pyarrow>=16.0",
    )
    .add_local_dir(
        Path(__file__).resolve().parent,
        remote_path="/root/scripts",
    )
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("bulk-ingest-r2"),
    modal.Secret.from_name("dex-db"),
]

# ---------------------------------------------------------------------------
# Constants (load-bearing — match harness greps; contract §2)
# ---------------------------------------------------------------------------

BRIDGE_NAME = "ucc_ca_debtor_sos_ca_owner"
METHOD_NAME = "legal_name_state_exact_ca_with_address_corroboration"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "2.0.0"

COLLISION_THRESHOLD = 50
# Validator-calibrated 2026-05-18 post full-corpus baseline probe.
# UCC CA Organization debtors = 3,681,852; post-normalize = 3,681,435.
# CA SoS entities (entity_name_normalized is_valid) = 9,389,284.
# Observed non-rejected rows = 3,062,504 (platinum=390,637; gold=1,535,461;
# silver=1,136,406; rejected=359,765). Floor = 2,000,000 (~33% headroom).
MIN_ROWS_MATCHED = 2_000_000

DATASET_SLUG = "ucc_ca_debtor_sos_ca_owner_lance"
SOURCE_LEFT = "ucc_ca_debtors_lance"
SOURCE_RIGHT = "sos_ca_entities_lance"

LEFT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/ucc_ca/debtors_lance"
UCC_DEBTORS_LANCE_URI = LEFT_LANCE_URI  # alias for readability
SOS_ENTITIES_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_debtor_sos_ca_owner_lance"
)

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _materialize_inputs(storage_options: dict, normalize_entity_name) -> tuple:
    """Load UCC CA debtors (Organization) + CA SoS entities into Arrow.

    UCC debtor side:
      - Read raw ORG_NAME + identifying/address columns from debtors_lance,
        filter to DEBTOR_TYPE='Organization' at the Lance scanner
        (mirrors PR #466's SECURED_PARTY_TYPE='Organization' filter; excludes
        ~2.2M individual-debtor rows focusing bridge on corporate refi-targets).
      - Normalize ORG_NAME in Python via _lib/entity_name_normalize
        (canonical normalizer; contract §3 — NOT a DuckDB UDF).
      - Drop rows where the normalized name is None or empty.

    SoS side:
      - Read entity_name_normalized + entity metadata columns from
        ca_entities_lance with is_valid() filter at the scanner.
    """
    import lance
    import pyarrow as pa
    import pyarrow.compute as pc

    logger.info("opening ucc_ca/debtors_lance ...")
    ucc_ds = lance.dataset(UCC_DEBTORS_LANCE_URI, storage_options=storage_options)
    ucc_tbl = ucc_ds.scanner(
        columns=[
            "UCC1_NUM",
            "UCC3_NUM",
            "DEBTOR_TYPE",
            "ORG_NAME",
            "ADDR1",
            "CITY",
            "STATE",
            "POSTAL_CODE",
            "address_base_normalized",
        ],
        filter=pc.field("DEBTOR_TYPE") == "Organization",
    ).to_table()
    rows_ucc_raw = len(ucc_tbl)
    logger.info(
        "  ucc debtors_lance (DEBTOR_TYPE=Organization): %d rows",
        rows_ucc_raw,
    )

    # Normalize ORG_NAME in Python (canonical normalizer ONLY — contract §3).
    org_names = ucc_tbl.column("ORG_NAME").to_pylist()
    normalized = [normalize_entity_name(n) for n in org_names]
    ucc_tbl = ucc_tbl.append_column(
        "debtor_name_normalized",
        pa.array(normalized, type=pa.string()),
    )
    # Filter out None/empty normalizations (free-mail / generic / suffix-stripped).
    mask = pc.is_valid(ucc_tbl.column("debtor_name_normalized"))
    ucc_tbl = ucc_tbl.filter(mask)
    rows_ucc_post_norm = len(ucc_tbl)
    logger.info(
        "  ucc after normalization (debtor_name_normalized is_valid): %d rows",
        rows_ucc_post_norm,
    )

    logger.info("opening sos/ca_entities_lance ...")
    sos_ds = lance.dataset(SOS_ENTITIES_LANCE_URI, storage_options=storage_options)
    sos_filter = pc.field("entity_name_normalized").is_valid()
    sos_tbl = sos_ds.scanner(
        columns=[
            "entity_num",
            "entity_name",
            "entity_name_normalized",
            "entity_status",
            "standing_sos",
            "entity_type",
            "llc_management_structure",
            "initial_filing_date",
            "principal_address_base_normalized",
            "principal_address_in_ca_base_normalized",
            "mailing_address_base_normalized",
        ],
        filter=sos_filter,
    ).to_table()
    rows_sos = len(sos_tbl)
    logger.info("  sos ca_entities_lance (post-filter): %d rows", rows_sos)

    return ucc_tbl, sos_tbl, rows_ucc_raw, rows_sos


def _build_match_table(
    ucc_tbl,
    sos_tbl,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Run exact-equality JOIN + fan-out tiering in DuckDB (Arrow bridge).

    DuckDB tuning for Modal scale (contract §11 / cpu=8 memory=32768):
      SET threads=8
      SET memory_limit='24GB'
      SET temp_directory='/tmp/lance'
      SET max_temp_directory_size='200GB'
      SET preserve_insertion_order=false
    """
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=8")
    con.execute("SET memory_limit='24GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='200GB'")
    con.execute("SET preserve_insertion_order=false")

    con.register("ucc", ucc_tbl)
    con.register("sos", sos_tbl)

    rows_ucc_reg = con.execute("SELECT COUNT(*) FROM ucc").fetchone()[0]
    rows_sos_reg = con.execute("SELECT COUNT(*) FROM sos").fetchone()[0]
    logger.info(
        "  registered: ucc=%d  sos=%d",
        rows_ucc_reg, rows_sos_reg,
    )

    # 1. Inner JOIN on normalized name (contract §5); pull address-axis columns through.
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_raw AS
        SELECT
            u.UCC1_NUM                       AS ucc1_num,
            u.UCC3_NUM                       AS ucc3_num,
            u.DEBTOR_TYPE                    AS debtor_type,
            u.ORG_NAME                       AS debtor_org_name_raw,
            u.debtor_name_normalized,
            u.ADDR1                          AS debtor_addr1,
            u.CITY                           AS debtor_city,
            u.STATE                          AS debtor_state,
            u.POSTAL_CODE                    AS debtor_postal_code,
            u.address_base_normalized        AS debtor_address_base_normalized,
            s.entity_num,
            s.entity_name,
            s.entity_name_normalized,
            s.entity_status,
            s.standing_sos,
            s.entity_type,
            s.llc_management_structure,
            s.initial_filing_date,
            s.principal_address_base_normalized       AS sos_principal_address_base_normalized,
            s.principal_address_in_ca_base_normalized AS sos_principal_address_in_ca_base_normalized,
            s.mailing_address_base_normalized         AS sos_mailing_address_base_normalized,
            '{METHOD_NAME}'                  AS match_method,
            u.debtor_name_normalized         AS match_value_normalized,
            'CA'                             AS match_state,
            '{BRIDGE_VERSION}'               AS bridge_version,
            '{bridge_run_id}'                AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'   AS generated_at
        FROM ucc u
        JOIN sos s
          ON u.debtor_name_normalized = s.entity_name_normalized
        """
    )
    rows_matched_pre = con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0]
    logger.info("  bridge_raw (pre-tier): %d rows", rows_matched_pre)

    # 2. Fan-out counts (contract §6 — symmetric two-sided per PR #466/#487).
    #    debtor_fan_out: # of UCC debtors sharing this normalized name.
    #    sos_fan_out: # of distinct SoS entity_num values for this normalized name.
    con.execute(
        """
        CREATE TEMP TABLE debtor_fanout AS
        SELECT debtor_name_normalized, COUNT(*) AS debtor_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE sos_fanout AS
        SELECT debtor_name_normalized,
               COUNT(DISTINCT entity_num) AS sos_fan_out
        FROM bridge_raw GROUP BY 1
        """
    )

    # 3. Tier rule + address agreement + composite tier.
    # `name_confidence_tier`     = pure name-axis fan-out tier (legacy meaning).
    # `address_agrees`           = TRUE iff debtor's address matches ANY SoS address role.
    # `address_match_path`       = pipe-delimited roles that matched ('principal' |
    #                              'principal_in_ca' | 'mailing'); NULL when none matched.
    # `address_match_value`      = the debtor's normalized address when it agreed; else NULL.
    # `composite_confidence_tier` = address-axis-corroborated tier:
    #     - platinum: name 1:1 AND address_agrees
    #     - gold:     (name 1:1, no address) OR (name 1:N|N:1 AND address_agrees)
    #     - silver:   everything else where name still matched and tier<>rejected
    # `confidence_tier`          = `composite_confidence_tier` (canonical column name preserved
    #                              for downstream consumers; semantics stricter as of v2.0.0).
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_all AS
        SELECT
            b.*,
            df.debtor_fan_out,
            sf.sos_fan_out,
            -- address agreement: TRUE iff debtor's normalized address equals
            -- any of the three SoS address roles (principal / principal_in_ca / mailing).
            (
              b.debtor_address_base_normalized IS NOT NULL
              AND (
                b.debtor_address_base_normalized = b.sos_principal_address_base_normalized
                OR b.debtor_address_base_normalized = b.sos_principal_address_in_ca_base_normalized
                OR b.debtor_address_base_normalized = b.sos_mailing_address_base_normalized
              )
            )                                                    AS address_agrees,
            -- which role(s) matched, pipe-delimited
            NULLIF(
              CONCAT_WS('|',
                CASE WHEN b.debtor_address_base_normalized IS NOT NULL
                       AND b.debtor_address_base_normalized = b.sos_principal_address_base_normalized
                     THEN 'principal' END,
                CASE WHEN b.debtor_address_base_normalized IS NOT NULL
                       AND b.debtor_address_base_normalized = b.sos_principal_address_in_ca_base_normalized
                     THEN 'principal_in_ca' END,
                CASE WHEN b.debtor_address_base_normalized IS NOT NULL
                       AND b.debtor_address_base_normalized = b.sos_mailing_address_base_normalized
                     THEN 'mailing' END
              ),
              ''
            )                                                    AS address_match_path,
            -- the debtor's normalized address when it agreed
            CASE
              WHEN b.debtor_address_base_normalized IS NOT NULL
                   AND (
                     b.debtor_address_base_normalized = b.sos_principal_address_base_normalized
                     OR b.debtor_address_base_normalized = b.sos_principal_address_in_ca_base_normalized
                     OR b.debtor_address_base_normalized = b.sos_mailing_address_base_normalized
                   )
              THEN b.debtor_address_base_normalized
              ELSE NULL
            END                                                  AS address_match_value,
            -- name-axis tier (legacy v1.0.0 meaning, exposed for backward-compat)
            CASE
                WHEN df.debtor_fan_out > {COLLISION_THRESHOLD}
                  OR sf.sos_fan_out > {COLLISION_THRESHOLD}
                    THEN 'rejected'
                WHEN df.debtor_fan_out = 1 AND sf.sos_fan_out = 1
                    THEN 'platinum'
                WHEN df.debtor_fan_out = 1 OR  sf.sos_fan_out = 1
                    THEN 'gold'
                ELSE 'silver'
            END                                                  AS name_confidence_tier
        FROM bridge_raw b
        JOIN debtor_fanout df USING (debtor_name_normalized)
        JOIN sos_fanout sf USING (debtor_name_normalized)
        """
    )

    # Composite tier — address agreement promotes; absence holds tier.
    con.execute(
        """
        CREATE TEMP TABLE bridge_tiered AS
        SELECT
            b.*,
            CASE
                WHEN b.name_confidence_tier = 'rejected'                                 THEN 'rejected'
                WHEN b.name_confidence_tier = 'platinum' AND b.address_agrees             THEN 'platinum'
                WHEN b.name_confidence_tier = 'platinum' AND NOT b.address_agrees         THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND b.address_agrees             THEN 'gold'
                WHEN b.name_confidence_tier = 'gold'     AND NOT b.address_agrees         THEN 'silver'
                WHEN b.name_confidence_tier = 'silver'   AND b.address_agrees             THEN 'silver'
                WHEN b.name_confidence_tier = 'silver'   AND NOT b.address_agrees         THEN 'silver'
                ELSE 'silver'
            END AS composite_confidence_tier
        FROM bridge_all b
        """
    )

    con.execute(
        """
        CREATE TEMP TABLE bridge_match AS
        SELECT
            *,
            composite_confidence_tier AS confidence_tier
        FROM bridge_tiered
        WHERE composite_confidence_tier <> 'rejected'
        """
    )

    counts_row = con.execute(
        """
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE confidence_tier = 'platinum'),
            COUNT(*) FILTER (WHERE confidence_tier = 'gold'),
            COUNT(*) FILTER (WHERE confidence_tier = 'silver'),
            COUNT(*) FILTER (WHERE address_agrees),
            COUNT(*) FILTER (WHERE address_match_path = 'principal'),
            COUNT(*) FILTER (WHERE address_match_path = 'principal_in_ca'),
            COUNT(*) FILTER (WHERE address_match_path = 'mailing')
        FROM bridge_match
        """
    ).fetchone()
    rejected = con.execute(
        "SELECT COUNT(*) FROM bridge_tiered WHERE composite_confidence_tier = 'rejected'"
    ).fetchone()[0]

    counts = {
        "rows_matched": counts_row[0],
        "rows_tier1": counts_row[1],
        "rows_tier2": counts_row[2],
        "rows_tier3": counts_row[3],
        "rows_address_agrees": counts_row[4],
        "rows_addr_principal": counts_row[5],
        "rows_addr_principal_in_ca": counts_row[6],
        "rows_addr_mailing": counts_row[7],
        "rows_collision_rejected": rejected,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict, lance_commit_lock) -> int:
    """Write bridge_match to Lance via Arrow-bridge pattern + dual BTREE (contract §7+§9)."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR
    os.environ["LANCE_BYPASS_SPILLING"] = "true"

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_match").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)",
            lance_count, write_dur, ds.version,
        )

        # Dual BTREE — contract §7 (both must succeed or run fails)
        try:
            ds.create_scalar_index("debtor_name_normalized", index_type="BTREE", replace=True)
            logger.info("BTREE on debtor_name_normalized: OK")
        except Exception as e:
            logger.error("BTREE on debtor_name_normalized FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on entity_num: OK")
        except Exception as e:
            logger.error("BTREE on entity_num FAILED: %s", e)
            raise
        # v2.0.0: composite tier + address-agreement BTREEs (non-fatal — these
        # are filter accelerators, not load-bearing identity keys).
        for col in ("composite_confidence_tier", "address_agrees"):
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                logger.info("BTREE on %s: OK", col)
            except Exception as e:
                logger.warning("BTREE on %s failed (non-fatal): %s", col, e)

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=10800,
    memory=32768,
    cpu=8,
)
def emit(apply: bool = False) -> dict:
    """Build the UCC CA org-debtor × CA SoS owner-identity bridge.

    apply=True  — full pipeline: Lance write + BTREE + complete_bridge_run.
    apply=False — dry-run: register + start + fail_bridge_run (no Lance write).
    """
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import (
        __version__ as NORMALIZER_VERSION,
        normalize_entity_name,
    )
    from scripts._lib.address_normalize import (
        __version__ as ADDR_NORMALIZER_VERSION,
    )
    from scripts._lib.lance_commit_lock import lance_commit_lock
    # v2.0.0 (address-axis composite) is a NEW method
    # `legal_name_state_exact_ca_with_address_corroboration` v1.0.0 — register
    # its method-definition + method-version-definition rows here. NOT reused.
    from scripts._lib.match_method_registry import (
        complete_bridge_run,
        fail_bridge_run,
        register_bridge,
        register_match_method,
        register_match_method_version,
        start_bridge_run,
    )

    _bridge_database_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    storage_options = _storage_options()
    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    logger.info(
        "bridge: %s  method=%s v%s  name_norm=v%s  addr_norm=v%s  apply=%s",
        BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER, NORMALIZER_VERSION, ADDR_NORMALIZER_VERSION, apply,
    )
    logger.info("inputs: %s + %s", UCC_DEBTORS_LANCE_URI, SOS_ENTITIES_LANCE_URI)
    logger.info("output: %s", BRIDGE_LANCE_URI)

    # Provenance: register NEW method + version (v2.0.0 is a fresh address-axis composite),
    # then register_bridge.
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Composite legal-name + state exact match with address corroboration. "
            "Inner-joins LEFT and RIGHT on exact-equality normalized legal name "
            "(canonical _lib/entity_name_normalize, CA-state constrained); then "
            "checks whether LEFT's normalized physical address (base form, "
            "unit-stripped via _lib/address_normalize) equals ANY of the RIGHT "
            "side's address roles (e.g. SoS principal / principal_in_ca / "
            "mailing). Address agreement promotes the name-axis fan-out tier: "
            "platinum requires BOTH 1:1 name AND address corroboration."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py + _lib/address_normalize.py",
        normalizer_version=f"name v{NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        blacklist_module="(same as component normalizers)",
        blacklist_version=f"name v{NORMALIZER_VERSION} / addr v{ADDR_NORMALIZER_VERSION}",
        tier_rule_description=(
            "Name fan-out tier (legacy 1:1=platinum, 1:N|N:1=gold, N:M<=50=silver, "
            ">50=rejected) is preserved as `name_confidence_tier`. Composite tier "
            "(stored in canonical `confidence_tier`): platinum REQUIRES name 1:1 "
            "AND address_agrees; without address corroboration the name-axis "
            "platinum demotes to gold and gold demotes to silver. Silver is held."
        ),
        rejection_rule_description=(
            "Reject when EITHER side's fan-out under the shared normalized name "
            "exceeds COLLISION_THRESHOLD (50). Same rule as the v1.0.0 method."
        ),
        input_columns_left=[
            "debtor_name_normalized", "STATE", "address_base_normalized",
        ],
        input_columns_right=[
            "entity_name_normalized",
            "principal_address_base_normalized",
            "principal_address_in_ca_base_normalized",
            "mailing_address_base_normalized",
        ],
        output_value_description=(
            "(debtor_name_normalized, entity_num) pair with name-axis match + "
            "boolean address_agrees + address_match_path (which SoS address "
            "role(s) matched) + composite_confidence_tier."
        ),
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "UCC CA org-debtor (Organization) x CA SoS entities — "
            "v2.0.0 composite name+address axis. Inner-joins on _lib-normalized "
            "legal name (CA-constrained); promotes/demotes the fan-out tier "
            "based on whether the UCC debtor's normalized physical address "
            "agrees with any of the SoS entity's three address roles "
            "(principal, principal_in_ca, mailing). Platinum requires both "
            "name 1:1 AND address corroboration. Method: "
            "legal_name_state_exact_ca_with_address_corroboration v1.0.0."
        ),
    )
    run_uuid = start_bridge_run(
        bridge_name=BRIDGE_NAME,
        method_semver=METHOD_SEMVER,
        bridge_version=BRIDGE_VERSION,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        match_method=METHOD_NAME,
        r2_output_key=BRIDGE_LANCE_URI,
    )
    bridge_run_id = str(run_uuid)
    logger.info("bridge_run_id=%s", bridge_run_id)

    # Dry-run gate (contract §17 Option B.a) — gate BEFORE any Lance write.
    if not apply:
        logger.info("DRY-RUN: dry-run; no Lance write (pass --apply to execute)")
        fail_bridge_run(run_uuid, "dry-run; no Lance write (pass --apply to execute)")
        logger.info("bridge_run marked failed-dry-run (run_id=%s)", bridge_run_id)
        return {
            "status": "dry-run",
            "bridge_run_id": bridge_run_id,
            "message": "dry-run; no Lance write (pass --apply to execute)",
        }

    try:
        ucc_tbl, sos_tbl, rows_left, rows_right = _materialize_inputs(
            storage_options, normalize_entity_name,
        )
        con, counts = _build_match_table(
            ucc_tbl, sos_tbl,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge composite tier distribution (v2.0.0 — address-axis):")
        logger.info("  rows_matched:            %d", counts["rows_matched"])
        logger.info("    platinum:               %d  (name 1:1 + address agrees)", counts["rows_tier1"])
        logger.info("    gold:                   %d  (name 1:1 OR (name 1:N|N:1 + address agrees))", counts["rows_tier2"])
        logger.info("    silver:                 %d  (residual N:M / no address corroboration)", counts["rows_tier3"])
        logger.info("  address_agrees:           %d  (any-role agreement)", counts["rows_address_agrees"])
        logger.info("    principal only:         %d", counts["rows_addr_principal"])
        logger.info("    principal_in_ca only:   %d", counts["rows_addr_principal_in_ca"])
        logger.info("    mailing only:           %d", counts["rows_addr_mailing"])
        logger.info(
            "  rows_collision_rejected:  %d",
            counts["rows_collision_rejected"],
        )

        if counts["rows_matched"] < MIN_ROWS_MATCHED:
            msg = (
                f"HARD FAIL: rows_matched={counts['rows_matched']:,} < "
                f"floor={MIN_ROWS_MATCHED:,}"
            )
            logger.error(msg)
            fail_bridge_run(run_uuid, msg)
            return {"status": "failed", "error": msg, "counts": counts}

        lance_count = _write_bridge_lance(con, storage_options, lance_commit_lock)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_left,
                "rows_right": rows_right,
                "rows_matched": counts["rows_matched"],
                "rows_tier1": counts["rows_tier1"],
                "rows_tier2": counts["rows_tier2"],
                "rows_tier3": counts["rows_tier3"],
                "rows_address_agrees": counts["rows_address_agrees"],
                "rows_addr_principal": counts["rows_addr_principal"],
                "rows_addr_principal_in_ca": counts["rows_addr_principal_in_ca"],
                "rows_addr_mailing": counts["rows_addr_mailing"],
                "rows_collision_rejected": counts["rows_collision_rejected"],
                "lance_rows": lance_count,
            },
        )
        logger.info(
            "OK - run_id=%s  duration=%.1fs",
            bridge_run_id, time.time() - t0,
        )
        logger.info(
            "OK: bridges.ucc_ca_debtor_sos_ca_owner_lance written (%d rows; "
            "platinum=%d gold=%d silver=%d rejected=%d)",
            lance_count,
            counts["rows_tier1"],
            counts["rows_tier2"],
            counts["rows_tier3"],
            counts["rows_collision_rejected"],
        )
        return {
            "status": "succeeded",
            "bridge_run_id": bridge_run_id,
            "rows_left": rows_left,
            "rows_right": rows_right,
            "rows_matched": counts["rows_matched"],
            "rows_tier1": counts["rows_tier1"],
            "rows_tier2": counts["rows_tier2"],
            "rows_tier3": counts["rows_tier3"],
            "rows_address_agrees": counts["rows_address_agrees"],
            "rows_collision_rejected": counts["rows_collision_rejected"],
            "lance_count": lance_count,
            "duration_s": round(time.time() - t0, 1),
        }
    except Exception as exc:
        logger.exception("bridge generation failed")
        try:
            fail_bridge_run(run_uuid, repr(exc))
        except Exception:
            logger.exception("also failed to mark run as failed")
        raise


@app.local_entrypoint()
def main(apply: bool = False) -> None:
    """`modal run scripts/build_bridge_ucc_ca_debtor_sos_ca_owner_lance.py::main --apply`

    Without --apply: dry-run (register + start + fail_bridge_run; no Lance write).
    With --apply:    full emit (JOIN + tier + write Lance + BTREE + complete_bridge_run).
    """
    import json
    out = emit.remote(apply=apply)
    print(json.dumps(out, indent=2, default=str))
