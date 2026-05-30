"""Match-method registry helpers — idempotent UPSERTs over the four ops tables.

Every cross-source identity-bridge generator (FMCSA-PDL, SAM-USAspending UEI,
SAM-PDL domain, etc.) imports this module and:

  1. Calls `register_match_method(...)` to ensure the rule row exists.
  2. Calls `register_match_method_version(...)` to ensure the (rule, semver) row
     exists with current normalizer/blacklist references.
  3. Calls `register_bridge(...)` to ensure the bridge instance row exists.
  4. Calls `start_bridge_run(...)` at the start of a generation run — returns
     a fresh `bridge_run_id UUID` that gets embedded in every Parquet row.
  5. Calls `complete_bridge_run(...)` on success or `fail_bridge_run(...)` on
     failure, with run metrics + r2 output key.

All four registry helpers (1-3) are idempotent UPSERTs keyed on the natural
business key:

  - register_match_method:         keyed on method_name.
  - register_match_method_version: keyed on (method_name, semver).
  - register_bridge:               keyed on bridge_name.

Second invocation with same args returns the same UUID and updates nothing
(except `created_at` is preserved). Different args raise ValueError.

Lessons learned: L18 (idempotent UPSERTs — no "INSERT IF NOT EXISTS" sprinkled
in bridge generators).

Usage (in a bridge generator):

    from scripts._lib.match_method_registry import (
        register_match_method,
        register_match_method_version,
        register_bridge,
        start_bridge_run,
        complete_bridge_run,
        fail_bridge_run,
    )

    method_id = register_match_method(
        method_name="domain_exact",
        description="Exact match on lower(trim(domain)) after protocol/path strip.",
    )
    version_id = register_match_method_version(
        method_name="domain_exact",
        semver="1.0.0",
        normalizer_module="_lib/domain_normalize.py",
        normalizer_version="1.0.0",
        blacklist_module="_lib/free_mail_domains.py",
        blacklist_version="1.0.0",
        tier_rule_description="platinum=1:1, gold=1:N or N:1, silver=N:M with fan-out <=50",
        rejection_rule_description="fan-out >50 collapsed to rows_collision_rejected",
        input_columns_left=["EMAIL_ADDRESS"],
        input_columns_right=["website"],
        output_value_description="lower(trim(domain after protocol/path strip))",
    )
    bridge_id = register_bridge(
        bridge_name="fmcsa_pdl_domain",
        source_left="source_fmcsa_census",
        source_right="source_pdl_companies",
        method_name="domain_exact",
        r2_output_prefix="bridges/fmcsa_pdl_domain/",
        description="FMCSA email domain x PDL website domain via exact-equality.",
    )

    run_id = start_bridge_run(
        bridge_name="fmcsa_pdl_domain",
        method_semver="1.0.0",
        bridge_version="1.0.0",  # legacy stamp — kept for backwards-compat
        source_left="source_fmcsa_census",
        source_right="source_pdl_companies",
        match_method="domain_exact",
        r2_output_key="bridges/fmcsa_pdl_domain/snapshot=YYYY-MM-DD/data.parquet",
    )
    # ... do work, write Parquet ...
    complete_bridge_run(run_id, metrics={
        "rows_left": 4400000, "rows_right": 30000000,
        "rows_matched": 296981, "rows_tier1": 184571,
        "rows_tier2": 108235, "rows_tier3": 4175,
        "rows_collision_rejected": 0, "domains_blacklisted": 1234,
    })
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

import psycopg


# --------------------------------------------------------------------------- #
# Connection helper
# --------------------------------------------------------------------------- #


def _connect() -> psycopg.Connection:
    """Open a fresh connection to the canonical Postgres (DEX_DB_URL_DIRECT)."""
    return psycopg.connect(os.environ["DEX_DB_URL_DIRECT"])


# --------------------------------------------------------------------------- #
# register_match_method
# --------------------------------------------------------------------------- #


def register_match_method(
    method_name: str,
    description: str,
    *,
    conn: psycopg.Connection | None = None,
) -> uuid.UUID:
    """Idempotent UPSERT into ops.match_methods. Returns match_method_id.

    Keyed on `method_name` (UNIQUE NOT NULL). If the row exists with a
    different description, the description is UPDATEd and the original
    UUID returned (description drift is treated as a non-breaking change).
    """
    if not method_name or not method_name.strip():
        raise ValueError("method_name must be non-empty")
    if not description or not description.strip():
        raise ValueError("description must be non-empty")

    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.match_methods (method_name, description)
                VALUES (%s, %s)
                ON CONFLICT (method_name) DO UPDATE
                  SET description = EXCLUDED.description
                RETURNING match_method_id
                """,
                (method_name, description),
            )
            row = cur.fetchone()
            assert row is not None
            method_id = row[0]
        if own_conn:
            conn.commit()
        return uuid.UUID(str(method_id))
    finally:
        if own_conn and conn is not None:
            conn.close()


# --------------------------------------------------------------------------- #
# register_match_method_version
# --------------------------------------------------------------------------- #


def register_match_method_version(
    method_name: str,
    semver: str,
    *,
    normalizer_module: str | None,
    normalizer_version: str | None,
    blacklist_module: str | None,
    blacklist_version: str | None,
    tier_rule_description: str,
    rejection_rule_description: str | None,
    input_columns_left: Iterable[str],
    input_columns_right: Iterable[str],
    output_value_description: str,
    tier_rule_config: dict[str, Any] | None = None,
    conn: psycopg.Connection | None = None,
) -> uuid.UUID:
    """Idempotent UPSERT into ops.match_method_versions. Returns version_id.

    Keyed on (method_name, semver). The method must already exist (call
    register_match_method first). If the (method, semver) row exists with
    different config, the config is UPDATEd to current values; original UUID
    returned. This lets a bridge generator change the normalizer-module ref
    without creating a new semver, but it also means semver bumps must be
    explicit — the generator should bump semver whenever the rule changes
    in a way that affects matched rows.
    """
    import json

    if not method_name or not method_name.strip():
        raise ValueError("method_name must be non-empty")
    if not semver or not semver.strip():
        raise ValueError("semver must be non-empty")
    if not tier_rule_description or not tier_rule_description.strip():
        raise ValueError("tier_rule_description must be non-empty")
    if not output_value_description or not output_value_description.strip():
        raise ValueError("output_value_description must be non-empty")

    left_cols = list(input_columns_left)
    right_cols = list(input_columns_right)
    if not left_cols:
        raise ValueError("input_columns_left must contain at least one column")
    if not right_cols:
        raise ValueError("input_columns_right must contain at least one column")

    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            # Resolve method_id.
            cur.execute(
                "SELECT match_method_id FROM ops.match_methods WHERE method_name = %s",
                (method_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"match_method {method_name!r} not registered — call "
                    f"register_match_method first"
                )
            method_id = row[0]

            cur.execute(
                """
                INSERT INTO ops.match_method_versions (
                    match_method_id, semver,
                    normalizer_module, normalizer_version,
                    blacklist_module, blacklist_version,
                    tier_rule_description, tier_rule_config,
                    rejection_rule_description,
                    input_columns_left, input_columns_right,
                    output_value_description
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s
                )
                ON CONFLICT (match_method_id, semver) DO UPDATE
                  SET normalizer_module = EXCLUDED.normalizer_module,
                      normalizer_version = EXCLUDED.normalizer_version,
                      blacklist_module = EXCLUDED.blacklist_module,
                      blacklist_version = EXCLUDED.blacklist_version,
                      tier_rule_description = EXCLUDED.tier_rule_description,
                      tier_rule_config = EXCLUDED.tier_rule_config,
                      rejection_rule_description = EXCLUDED.rejection_rule_description,
                      input_columns_left = EXCLUDED.input_columns_left,
                      input_columns_right = EXCLUDED.input_columns_right,
                      output_value_description = EXCLUDED.output_value_description
                RETURNING version_id
                """,
                (
                    method_id, semver,
                    normalizer_module, normalizer_version,
                    blacklist_module, blacklist_version,
                    tier_rule_description,
                    json.dumps(tier_rule_config) if tier_rule_config is not None else None,
                    rejection_rule_description,
                    left_cols, right_cols,
                    output_value_description,
                ),
            )
            version_row = cur.fetchone()
            assert version_row is not None
            version_id = version_row[0]
        if own_conn:
            conn.commit()
        return uuid.UUID(str(version_id))
    finally:
        if own_conn and conn is not None:
            conn.close()


# --------------------------------------------------------------------------- #
# register_bridge
# --------------------------------------------------------------------------- #


def register_bridge(
    bridge_name: str,
    source_left: str,
    source_right: str,
    method_name: str,
    r2_output_prefix: str,
    description: str | None = None,
    *,
    conn: psycopg.Connection | None = None,
) -> uuid.UUID:
    """Idempotent UPSERT into ops.bridges. Returns bridge_id.

    Keyed on bridge_name. Method must already exist.
    """
    if not bridge_name or not bridge_name.strip():
        raise ValueError("bridge_name must be non-empty")
    if not source_left or not source_left.strip():
        raise ValueError("source_left must be non-empty")
    if not source_right or not source_right.strip():
        raise ValueError("source_right must be non-empty")
    if not method_name or not method_name.strip():
        raise ValueError("method_name must be non-empty")
    if not r2_output_prefix or not r2_output_prefix.strip():
        raise ValueError("r2_output_prefix must be non-empty")

    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT match_method_id FROM ops.match_methods WHERE method_name = %s",
                (method_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"match_method {method_name!r} not registered — call "
                    f"register_match_method first"
                )
            method_id = row[0]

            cur.execute(
                """
                INSERT INTO ops.bridges (
                    bridge_name, source_left, source_right,
                    match_method_id, r2_output_prefix, description
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (bridge_name) DO UPDATE
                  SET source_left = EXCLUDED.source_left,
                      source_right = EXCLUDED.source_right,
                      match_method_id = EXCLUDED.match_method_id,
                      r2_output_prefix = EXCLUDED.r2_output_prefix,
                      description = EXCLUDED.description
                RETURNING bridge_id
                """,
                (
                    bridge_name, source_left, source_right,
                    method_id, r2_output_prefix, description,
                ),
            )
            bridge_row = cur.fetchone()
            assert bridge_row is not None
            bridge_id = bridge_row[0]
        if own_conn:
            conn.commit()
        return uuid.UUID(str(bridge_id))
    finally:
        if own_conn and conn is not None:
            conn.close()


# --------------------------------------------------------------------------- #
# start_bridge_run / complete_bridge_run / fail_bridge_run
# --------------------------------------------------------------------------- #


def start_bridge_run(
    bridge_name: str,
    method_semver: str,
    *,
    bridge_version: str,
    source_left: str,
    source_right: str,
    match_method: str,
    r2_output_key: str,
    conn: psycopg.Connection | None = None,
) -> uuid.UUID:
    """INSERT a 'running' row into ops.bridge_generation_runs. Returns run_id.

    Resolves bridge_id from bridge_name, match_method_version_id from
    (method_name, semver) — both required FKs in the registry-aware shape.

    The legacy `bridge_version`, `source_left`, `source_right`,
    `match_method`, `bridge_name` columns are still populated for
    backwards-compat (operators have queries against the flat shape).
    """
    if not bridge_name or not bridge_name.strip():
        raise ValueError("bridge_name must be non-empty")
    if not method_semver or not method_semver.strip():
        raise ValueError("method_semver must be non-empty")

    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT bridge_id, match_method_id FROM ops.bridges WHERE bridge_name = %s",
                (bridge_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"bridge {bridge_name!r} not registered — call "
                    f"register_bridge first"
                )
            bridge_id, method_id = row[0], row[1]

            cur.execute(
                """
                SELECT version_id FROM ops.match_method_versions
                 WHERE match_method_id = %s AND semver = %s
                """,
                (method_id, method_semver),
            )
            ver_row = cur.fetchone()
            if ver_row is None:
                raise ValueError(
                    f"match_method version (method_id={method_id}, semver="
                    f"{method_semver!r}) not registered — call "
                    f"register_match_method_version first"
                )
            version_id = ver_row[0]

            run_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO ops.bridge_generation_runs (
                    run_id, bridge_name, bridge_version,
                    source_left, source_right, match_method,
                    r2_output_key,
                    started_at, status,
                    bridge_id, match_method_version_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'running', %s, %s
                )
                """,
                (
                    str(run_id), bridge_name, bridge_version,
                    source_left, source_right, match_method,
                    r2_output_key,
                    datetime.now(tz=timezone.utc),
                    str(bridge_id), str(version_id),
                ),
            )
        if own_conn:
            conn.commit()
        return run_id
    finally:
        if own_conn and conn is not None:
            conn.close()


def complete_bridge_run(
    bridge_run_id: uuid.UUID,
    metrics: dict[str, Any],
    *,
    conn: psycopg.Connection | None = None,
) -> None:
    """UPDATE the run row to status='completed' with metrics populated.

    metrics keys (all optional):
      rows_left, rows_right, rows_matched,
      rows_tier1, rows_tier2, rows_tier3,
      rows_collision_rejected, domains_blacklisted.
    """
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.bridge_generation_runs
                   SET status = 'completed',
                       finished_at = %s,
                       duration_seconds =
                         EXTRACT(EPOCH FROM (%s - started_at)),
                       rows_left = %s, rows_right = %s,
                       rows_matched = %s,
                       rows_tier1 = %s, rows_tier2 = %s, rows_tier3 = %s,
                       rows_collision_rejected = %s,
                       domains_blacklisted = %s,
                       error_message = NULL
                 WHERE run_id = %s
                """,
                (
                    datetime.now(tz=timezone.utc),
                    datetime.now(tz=timezone.utc),
                    metrics.get("rows_left"),
                    metrics.get("rows_right"),
                    metrics.get("rows_matched"),
                    metrics.get("rows_tier1"),
                    metrics.get("rows_tier2"),
                    metrics.get("rows_tier3"),
                    metrics.get("rows_collision_rejected"),
                    metrics.get("domains_blacklisted"),
                    str(bridge_run_id),
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    f"complete_bridge_run: expected 1 row update, got "
                    f"{cur.rowcount} — run_id={bridge_run_id} not found"
                )
        if own_conn:
            conn.commit()
    finally:
        if own_conn and conn is not None:
            conn.close()


def fail_bridge_run(
    bridge_run_id: uuid.UUID,
    error_message: str,
    *,
    conn: psycopg.Connection | None = None,
) -> None:
    """UPDATE the run row to status='failed' with error_message populated."""
    if not error_message or not error_message.strip():
        raise ValueError("error_message must be non-empty for fail_bridge_run")

    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ops.bridge_generation_runs
                   SET status = 'failed',
                       finished_at = %s,
                       duration_seconds =
                         EXTRACT(EPOCH FROM (%s - started_at)),
                       error_message = %s
                 WHERE run_id = %s
                """,
                (
                    datetime.now(tz=timezone.utc),
                    datetime.now(tz=timezone.utc),
                    error_message,
                    str(bridge_run_id),
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(
                    f"fail_bridge_run: expected 1 row update, got "
                    f"{cur.rowcount} — run_id={bridge_run_id} not found"
                )
        if own_conn:
            conn.commit()
    finally:
        if own_conn and conn is not None:
            conn.close()
