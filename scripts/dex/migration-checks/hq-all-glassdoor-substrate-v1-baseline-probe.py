#!/usr/bin/env python3
"""Baseline probe for /scope cycle hq-all-glassdoor-substrate-v1.

Scaffolded by Stage 2 Migration validator subagent (2026-05-17 UTC) from the directive at
/Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-glassdoor-substrate-v1.md.

Purpose: capture pre-execution baseline state for forensic comparison if
the cycle fails partway and needs operator-driven diagnosis. Each probe is
read-only (NO writes); each prints a single-line `KEY=VALUE` for cycle-report
greppability.

Usage:
    doppler run --project hq-all --config prd -- bash -c '
        uv run --quiet --with pylance --with requests --with psycopg2-binary \
            python3 hq-all-glassdoor-substrate-v1-baseline-probe.py
    '

Exit code: 0 always (probe is observational). Errors print to stderr but do
not block the cycle.
"""
from __future__ import annotations

import os
import sys
from typing import Any


def probe(name: str, fn) -> None:
    """Run a probe; print KEY=VALUE result; never raise."""
    try:
        val = fn()
        print(f"{name}={val}")
    except Exception as exc:  # noqa: BLE001 — probe must not crash
        print(f"{name}=ERROR:{type(exc).__name__}:{exc}", file=sys.stderr)


def probe_pdl_lance() -> str:
    import lance

    storage_options = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }
    ds = lance.dataset(
        "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance",
        storage_options=storage_options,
    )
    rows = ds.count_rows()
    has_pdl_website = "pdl_website" in ds.schema.names
    has_pdl_id = "pdl_id" in ds.schema.names
    has_legal_name = "legal_name_normalized" in ds.schema.names
    return f"rows={rows};has_pdl_website={has_pdl_website};has_pdl_id={has_pdl_id};has_legal_name_normalized={has_legal_name}"


def probe_existing_glassdoor_sources() -> str:
    """All 4 entities.source_glassdoor_* tables should NOT exist yet."""
    import psycopg2

    conn = psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='entities'
          AND table_name LIKE 'source_glassdoor_%'
        ORDER BY table_name
        """
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return f"existing_tables={','.join(rows) if rows else 'NONE'};count={len(rows)}"


def probe_existing_ops_ledger() -> str:
    """ops.glassdoor_ingest_runs should NOT exist yet."""
    import psycopg2

    conn = psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema='ops' AND table_name='glassdoor_ingest_runs'
        """
    )
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return f"ops_glassdoor_ingest_runs_exists={exists}"


def probe_existing_ops_data_sources_rows() -> str:
    """No 'glassdoor_*' or 'bridges_pdl_glassdoor_*' rows should exist in ops.data_sources yet."""
    import psycopg2

    conn = psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT display_name FROM ops.data_sources
        WHERE display_name LIKE 'glassdoor_%'
           OR display_name LIKE 'bridges_pdl_glassdoor_%'
        ORDER BY display_name
        """
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return f"existing_glassdoor_data_sources={','.join(rows) if rows else 'NONE'};count={len(rows)}"


def probe_domain_exact_method() -> str:
    """L21 — domain_exact v1.0.0 must already exist (cycle reuses it; does NOT register)."""
    import psycopg2

    conn = psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT mm.match_method_id::text, mmv.semver
        FROM ops.match_methods mm
        JOIN ops.match_method_versions mmv ON mm.match_method_id = mmv.match_method_id
        WHERE mm.method_name = 'domain_exact'
        ORDER BY mmv.semver
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return "domain_exact=NOT_REGISTERED (BLOCKER — cycle assumes L21 reuse)"
    return ";".join(f"id={r[0]};semver={r[1]}" for r in rows)


def probe_existing_bridges() -> str:
    """Existing bridges using domain_exact v1.0.0 — sanity check that L21 reuse is the right pattern."""
    import psycopg2

    conn = psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])
    cur = conn.cursor()
    cur.execute(
        """
        SELECT b.bridge_name
        FROM ops.bridges b
        JOIN ops.match_methods mm ON b.match_method_id = mm.match_method_id
        WHERE mm.method_name = 'domain_exact'
        ORDER BY b.bridge_name
        """
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return f"existing_domain_exact_bridges={','.join(rows) if rows else 'NONE'};count={len(rows)}"


def probe_existing_pdl_glassdoor_bridge() -> str:
    """The pdl_glassdoor_lance bridge should NOT exist yet."""
    import psycopg2

    conn = psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM ops.bridges WHERE bridge_name = 'pdl_glassdoor_lance'"
    )
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return f"pdl_glassdoor_lance_bridge_exists={exists}"


def probe_railway_baseline() -> str:
    """Capture the Railway baseline deployment SHA pre-merge."""
    import subprocess

    proc = subprocess.run(
        [
            "railway",
            "deployment",
            "list",
            "--service",
            "data-engine-x",
            "--limit",
            "1",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return f"ERROR:railway:{proc.stderr.strip()}"
    import json

    data = json.loads(proc.stdout)
    if not data:
        return "no_deployments"
    d = data[0]
    return f"status={d.get('status')};sha={d.get('meta', {}).get('commitHash', '')[:8]}"


def probe_openwebninja_credit_check() -> str:
    """Tiny dry-pass to confirm API key is live."""
    import requests

    api_key = os.environ.get("OPENWEBNINJA_API_KEY")
    if not api_key:
        return "OPENWEBNINJA_API_KEY=MISSING"
    r = requests.get(
        "https://api.openwebninja.com/realtime-glassdoor-data/company-search",
        params={"query": "Apple", "limit": 1},
        headers={"x-api-key": api_key},
        timeout=10,
    )
    return f"status={r.status_code};status_field={(r.json() or {}).get('status', 'NO_STATUS')}"


def main() -> int:
    print("# hq-all-glassdoor-substrate-v1 baseline probe — pre-execution state")
    print(f"# timestamp: {os.popen('date -u +%Y-%m-%dT%H:%M:%SZ').read().strip()}")
    print(f"# git_head: {os.popen('git -C \"$HQ_ALL_ROOT\" rev-parse HEAD 2>/dev/null || echo unknown').read().strip()}")

    probe("pdl_lance", probe_pdl_lance)
    probe("existing_glassdoor_sources", probe_existing_glassdoor_sources)
    probe("ops_glassdoor_ingest_runs", probe_existing_ops_ledger)
    probe("ops_data_sources_glassdoor", probe_existing_ops_data_sources_rows)
    probe("domain_exact_method", probe_domain_exact_method)
    probe("existing_domain_exact_bridges", probe_existing_bridges)
    probe("pdl_glassdoor_lance_bridge", probe_existing_pdl_glassdoor_bridge)
    probe("railway_baseline", probe_railway_baseline)
    probe("openwebninja_credit_check", probe_openwebninja_credit_check)

    return 0


if __name__ == "__main__":
    sys.exit(main())
