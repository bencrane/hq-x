#!/usr/bin/env python3
"""
Test USASpending.gov and SAM.gov API endpoints.
Validation only — no DB writes. Produces a markdown report.

Note: Prefer scripts/test_usaspending_sam_apis.sh (curl-based) for reliability.
This Python script may hang on SAM.gov requests in some environments.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

USASPENDING_BASE = "https://api.usaspending.gov"
SAM_BASE = "https://api.sam.gov"


@dataclass
class TestResult:
    test_num: int
    name: str
    passed: bool
    status_code: int | None
    error: str | None = None
    sample_data: list[dict[str, Any]] = field(default_factory=list)
    full_fields: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


def run_usaspending_tests() -> list[TestResult]:
    results: list[TestResult] = []
    client = httpx.Client(timeout=30.0)

    # Test 1: Spending By Award Search
    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "naics_codes": {"require": ["31", "32", "33"]},
            "recipient_type_names": ["small_business"],
            "time_period": [{"start_date": "2026-02-13", "end_date": "2026-03-13"}],
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Recipient UEI",
            "recipient_id",
            "Award Amount",
            "Awarding Agency",
            "NAICS",
            "Start Date",
            "Place of Performance State Code",
        ],
        "sort": "Start Date",
        "order": "desc",
        "limit": 10,
    }
    try:
        r = client.post(f"{USASPENDING_BASE}/api/v2/search/spending_by_award/", json=payload)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = (
            r.status_code == 200
            and "results" in data
            and isinstance(data.get("results"), list)
        )
        sample = []
        if passed and data.get("results"):
            for item in data["results"][:5]:
                sample.append({k: v for k, v in item.items()})
            page_meta = data.get("page_metadata", {})
            results.append(
                TestResult(
                    test_num=1,
                    name="Spending By Award Search",
                    passed=passed,
                    status_code=r.status_code,
                    sample_data=sample,
                    metadata={
                        "total_results": len(data.get("results", [])),
                        "page_metadata": page_meta,
                    },
                )
            )
        else:
            results.append(
                TestResult(
                    test_num=1,
                    name="Spending By Award Search",
                    passed=False,
                    status_code=r.status_code,
                    error=data.get("detail", r.text[:500]) if not passed else None,
                )
            )
    except Exception as e:
        results.append(
            TestResult(
                test_num=1,
                name="Spending By Award Search",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 1b: Pagination (page 2)
    if results[-1].passed:
        try:
            payload["page"] = 2
            r = client.post(f"{USASPENDING_BASE}/api/v2/search/spending_by_award/", json=payload)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            passed = r.status_code == 200 and "results" in data
            results.append(
                TestResult(
                    test_num=1,
                    name="Spending By Award Search (page 2)",
                    passed=passed,
                    status_code=r.status_code,
                    metadata={"page_metadata": data.get("page_metadata", {})},
                    notes="Pagination test",
                )
            )
        except Exception as e:
            results.append(
                TestResult(
                    test_num=1,
                    name="Spending By Award Search (page 2)",
                    passed=False,
                    status_code=None,
                    error=str(e),
                )
            )

    # Get award identifiers and recipient from Test 1 for Tests 2 and 3
    award_id = None
    generated_internal_id = None
    recipient_id = None
    if results[0].passed and results[0].sample_data:
        first = results[0].sample_data[0]
        award_id = first.get("Award ID") or first.get("award_id")
        generated_internal_id = first.get("generated_internal_id")
        recipient_id = first.get("recipient_id")

    # Test 6: Full schema — request all base + contract fields to discover full response
    all_requestable_fields = [
        "Award ID", "Recipient Name", "Recipient DUNS Number", "recipient_id", "Recipient UEI",
        "Awarding Agency", "Awarding Agency Code", "Awarding Sub Agency", "Awarding Sub Agency Code",
        "Funding Agency", "Funding Agency Code", "Funding Sub Agency", "Funding Sub Agency Code",
        "Place of Performance City Code", "Place of Performance State Code", "Place of Performance Country Code",
        "Place of Performance Zip5", "Description", "Last Modified Date", "Base Obligation Date",
        "Start Date", "End Date", "Award Amount", "Total Outlays", "Contract Award Type", "NAICS", "PSC",
        "Recipient Location", "Primary Place of Performance", "generated_internal_id",
    ]
    try:
        payload_full_fields = {
            "filters": {
                "award_type_codes": ["A", "B", "C", "D"],
                "naics_codes": {"require": ["31", "32", "33"]},
                "recipient_type_names": ["small_business"],
                "time_period": [{"start_date": "2026-02-13", "end_date": "2026-03-13"}],
            },
            "fields": all_requestable_fields,
            "sort": "Start Date",
            "order": "desc",
            "limit": 5,
        }
        r = client.post(f"{USASPENDING_BASE}/api/v2/search/spending_by_award/", json=payload_full_fields)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = r.status_code == 200 and "results" in data
        all_fields: list[str] = []
        if passed and data.get("results"):
            all_fields = list(data["results"][0].keys())
        results.append(
            TestResult(
                test_num=6,
                name="Full Response Schema Discovery (all fields)",
                passed=passed,
                status_code=r.status_code,
                full_fields=all_fields,
                sample_data=[data["results"][0]] if data.get("results") else [],
            )
        )
        if passed and data.get("results") and not generated_internal_id:
            first = data["results"][0]
            generated_internal_id = first.get("generated_internal_id")
            if not award_id:
                award_id = first.get("Award ID") or first.get("award_id")
            if not recipient_id:
                recipient_id = first.get("recipient_id")
    except Exception as e:
        results.append(
            TestResult(
                test_num=6,
                name="Full Response Schema Discovery",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 2: Recipient Details (uses recipient_id from search results)
    try:
        rid = recipient_id
        if not rid:
            results.append(
                TestResult(
                    test_num=2,
                    name="Recipient Details",
                    passed=False,
                    status_code=None,
                    error="No recipient_id in Test 1 results",
                )
            )
        else:
            r = client.get(f"{USASPENDING_BASE}/api/v2/recipient/{rid}/")
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            passed = r.status_code == 200 and (data.get("name") or data.get("recipient_name") or "recipient_id" in str(data))
            results.append(
                TestResult(
                    test_num=2,
                    name="Recipient Details",
                    passed=passed,
                    status_code=r.status_code,
                    sample_data=[{k: v for k, v in list(data.items())[:15]}],
                    notes=f"Identifier: recipient_id (hash-level format)",
                )
            )
    except Exception as e:
        results.append(
            TestResult(
                test_num=2,
                name="Recipient Details",
                passed=False,
                status_code=None,
                error=str(e),
                notes="May need recipient_hash or different ID format",
            )
        )

    # Test 3: Award Details (uses generated_internal_id — Award ID alone returns 404)
    try:
        aid = generated_internal_id or award_id
        if aid:
            r = client.get(f"{USASPENDING_BASE}/api/v2/awards/{aid}/")
        else:
            r = client.get(f"{USASPENDING_BASE}/api/v2/awards/0/")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = r.status_code == 200 and (data.get("id") or data.get("award_id") or "award" in str(data).lower())
        results.append(
            TestResult(
                test_num=3,
                name="Award Details",
                passed=passed,
                status_code=r.status_code,
                sample_data=[{k: v for k, v in list(data.items())[:20]}],
                full_fields=list(data.keys()) if passed else [],
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=3,
                name="Award Details",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 4: NAICS Reference
    try:
        r = client.get(f"{USASPENDING_BASE}/api/v2/references/naics/33/")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = r.status_code == 200
        results.append(
            TestResult(
                test_num=4,
                name="NAICS Code Reference",
                passed=passed,
                status_code=r.status_code,
                sample_data=[data] if isinstance(data, dict) else data[:3] if isinstance(data, list) else [],
                notes="Response structure logged",
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=4,
                name="NAICS Code Reference",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 5: Bulk Download (optional) — requires agencies, date_range, date_type per API contract
    try:
        bulk_payload = {
            "filters": {
                "prime_award_types": ["A", "B", "C", "D"],
                "date_type": "action_date",
                "date_range": {"start_date": "2026-03-01", "end_date": "2026-03-08"},
                "agencies": [{"type": "awarding", "tier": "toptier", "name": "Department of Defense"}],
            },
        }
        r = client.post(f"{USASPENDING_BASE}/api/v2/bulk_download/awards/", json=bulk_payload)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = r.status_code in (200, 201, 202) or "download" in str(data).lower() or "status" in str(data).lower()
        results.append(
            TestResult(
                test_num=5,
                name="Bulk Download (optional)",
                passed=passed,
                status_code=r.status_code,
                metadata=data if isinstance(data, dict) else {},
                notes="Just confirm endpoint responsive; do not wait for file",
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=5,
                name="Bulk Download (optional)",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    client.close()
    return results


def run_sam_tests(api_key: str) -> list[TestResult]:
    results: list[TestResult] = []
    client = httpx.Client(timeout=30.0)
    key_param = f"api_key={api_key}"

    # Test 7: Entity Management — Basic Search
    try:
        url = f"{SAM_BASE}/entity-information/v3/entities?{key_param}&naicsCode=33&registrationDate=[2026-03-01,2026-03-13]&includeSections=entityRegistration,coreData"
        r = client.get(url)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = r.status_code == 200 and "totalRecords" in data or "entityData" in data
        entity_data = data.get("entityData", [])
        sample = []
        uei_from_test7 = None
        if passed and entity_data:
            for e in entity_data[:5]:
                sample.append(
                    {
                        "ueiSAM": e.get("ueiSAM"),
                        "legalBusinessName": e.get("legalBusinessName"),
                        "registrationDate": e.get("registrationDate"),
                        "physicalAddress": e.get("physicalAddress", {}),
                        "businessTypes": e.get("businessTypes", []),
                    }
                )
                if not uei_from_test7:
                    uei_from_test7 = e.get("ueiSAM")
        results.append(
            TestResult(
                test_num=7,
                name="Entity Management — Basic Search",
                passed=passed,
                status_code=r.status_code,
                sample_data=sample,
                metadata={"totalRecords": data.get("totalRecords"), "error": data.get("errorMessage")},
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=7,
                name="Entity Management — Basic Search",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )
        uei_from_test7 = None

    # Test 8: Search by UEI
    try:
        uei = uei_from_test7 or "ABCDEFGHIJ12"  # fallback
        url = f"{SAM_BASE}/entity-information/v3/entities?{key_param}&ueiSAM={uei}&includeSections=entityRegistration,coreData"
        r = client.get(url)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        entity_data = data.get("entityData", [])
        passed = r.status_code == 200 and len(entity_data) >= 1
        results.append(
            TestResult(
                test_num=8,
                name="Entity Management — Search by UEI",
                passed=passed,
                status_code=r.status_code,
                sample_data=entity_data[:1] if entity_data else [],
                notes=f"UEI used: {uei}",
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=8,
                name="Entity Management — Search by UEI",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 9: Search by Business Name
    try:
        url = f"{SAM_BASE}/entity-information/v3/entities?{key_param}&legalBusinessName=ACME&includeSections=entityRegistration,coreData"
        r = client.get(url)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        entity_data = data.get("entityData", [])
        passed = r.status_code == 200
        results.append(
            TestResult(
                test_num=9,
                name="Entity Management — Search by Business Name",
                passed=passed,
                status_code=r.status_code,
                sample_data=[{"legalBusinessName": e.get("legalBusinessName"), "ueiSAM": e.get("ueiSAM")} for e in entity_data[:5]],
                metadata={"totalRecords": data.get("totalRecords")},
                notes="Check if search is exact/partial/contains",
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=9,
                name="Entity Management — Search by Business Name",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 10: CSV Format
    try:
        url = f"{SAM_BASE}/entity-information/v3/entities?{key_param}&naicsCode=33&registrationDate=[2026-03-01,2026-03-13]&format=csv"
        r = client.get(url)
        is_csv = "text/csv" in r.headers.get("content-type", "") or r.text.strip().startswith("ueiSAM") or "," in r.text[:100]
        passed = r.status_code == 200 and (is_csv or "download" in r.text.lower() or "url" in r.text.lower())
        results.append(
            TestResult(
                test_num=10,
                name="Entity Management — CSV Format",
                passed=passed,
                status_code=r.status_code,
                metadata={"content_type": r.headers.get("content-type"), "body_preview": r.text[:300]},
                notes="Inline CSV vs download link",
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=10,
                name="Entity Management — CSV Format",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 11: Full Section Discovery
    try:
        uei = uei_from_test7 or "ABCDEFGHIJ12"
        url = f"{SAM_BASE}/entity-information/v3/entities?{key_param}&ueiSAM={uei}&includeSections=entityRegistration,coreData,generalInformation,repsAndCerts,pointsOfContact"
        r = client.get(url)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        entity_data = data.get("entityData", [])
        all_sections: list[str] = []
        if entity_data:
            for e in entity_data:
                all_sections.extend(e.keys())
            all_sections = list(dict.fromkeys(all_sections))
        passed = r.status_code == 200
        results.append(
            TestResult(
                test_num=11,
                name="Entity Management — Full Section Discovery",
                passed=passed,
                status_code=r.status_code,
                full_fields=all_sections,
                sample_data=entity_data[:1] if entity_data else [],
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=11,
                name="Entity Management — Full Section Discovery",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 12: Entity Extracts — List Available Files
    try:
        url = f"{SAM_BASE}/data-services/v1/extracts?{key_param}&fileType=ENTITY&sensitivity=PUBLIC&frequency=MONTHLY"
        r = client.get(url)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = r.status_code == 200
        results.append(
            TestResult(
                test_num=12,
                name="Entity Extracts — List Available Files",
                passed=passed,
                status_code=r.status_code,
                metadata=data if isinstance(data, dict) else {},
                sample_data=data.get("listData", [])[:3] if isinstance(data, dict) else [],
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=12,
                name="Entity Extracts — List Available Files",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 13: Entity Extracts — Daily Delta
    try:
        url = f"{SAM_BASE}/data-services/v1/extracts?{key_param}&fileType=ENTITY&sensitivity=PUBLIC&frequency=DAILY&date=03/12/2026"
        r = client.get(url)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        passed = r.status_code == 200
        results.append(
            TestResult(
                test_num=13,
                name="Entity Extracts — Daily Delta",
                passed=passed,
                status_code=r.status_code,
                metadata=data if isinstance(data, dict) else {},
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=13,
                name="Entity Extracts — Daily Delta",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    # Test 14: Get Opportunities
    try:
        url = f"{SAM_BASE}/opportunities/v2/search?{key_param}&postedFrom=03/01/2026&postedTo=03/13/2026&ptype=o&limit=5"
        r = client.get(url)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        opps = data.get("opportunitiesData", [])
        passed = r.status_code == 200
        results.append(
            TestResult(
                test_num=14,
                name="Get Opportunities API",
                passed=passed,
                status_code=r.status_code,
                sample_data=[{"title": o.get("title"), "agency": o.get("agency"), "postedDate": o.get("postedDate"), "naicsCode": o.get("naicsCode")} for o in opps[:5]],
                metadata={"totalRecords": data.get("totalRecords")},
            )
        )
    except Exception as e:
        results.append(
            TestResult(
                test_num=14,
                name="Get Opportunities API",
                passed=False,
                status_code=None,
                error=str(e),
            )
        )

    client.close()
    return results


def write_report(usaspending_results: list[TestResult], sam_results: list[TestResult], out_path: str) -> None:
    all_results = sorted(usaspending_results + sam_results, key=lambda x: (x.test_num, x.name))

    lines = [
        "# USASpending.gov & SAM.gov API Test Report",
        "",
        f"**Generated:** {datetime.utcnow().isoformat()}Z",
        "",
        "## Summary",
        "",
    ]

    # Pass/fail table
    lines.append("| Test | Name | Status | Status Code |")
    lines.append("|------|------|--------|-------------|")
    for r in all_results:
        status = "✅ PASS" if r.passed else "❌ FAIL"
        code = str(r.status_code) if r.status_code else "—"
        lines.append(f"| {r.test_num} | {r.name} | {status} | {code} |")

    lines.extend(["", "---", ""])

    # Detailed sections
    for r in all_results:
        lines.append(f"### Test {r.test_num}: {r.name}")
        lines.append("")
        if r.error:
            lines.append(f"**Error:** {r.error}")
        if r.notes:
            lines.append(f"**Notes:** {r.notes}")
        if r.sample_data:
            lines.append("**Sample data:**")
            lines.append("```json")
            lines.append(json.dumps(r.sample_data[:5], indent=2, default=str))
            lines.append("```")
        if r.metadata:
            lines.append("**Metadata:**")
            lines.append("```json")
            lines.append(json.dumps(r.metadata, indent=2, default=str))
            lines.append("```")
        if r.full_fields:
            lines.append("**Full field list:**")
            lines.append("```")
            lines.append(", ".join(r.full_fields))
            lines.append("```")
        lines.append("")

    # Extract files section
    lines.append("## Available Extract Files (SAM.gov)")
    lines.append("")
    for r in sam_results:
        if r.test_num in (12, 13) and r.metadata:
            lines.append(f"### Test {r.test_num} extract metadata")
            lines.append("```json")
            lines.append(json.dumps(r.metadata, indent=2, default=str))
            lines.append("```")
            lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    lines.append("(To be filled based on test outcomes — which endpoints for daily ingestion vs baseline load)")
    lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))


def main() -> int:
    api_key = os.environ.get("SAM_GOV_API_KEY")
    if not api_key:
        print("SAM_GOV_API_KEY not set. Run with: doppler run -- python3 scripts/test_usaspending_sam_apis.py", file=sys.stderr)
        print("Proceeding with USASpending tests only; SAM tests will fail with 401.", file=sys.stderr)
        api_key = "MISSING_KEY"

    print("Running USASpending.gov tests...", flush=True)
    usaspending_results = run_usaspending_tests()

    print("Running SAM.gov tests...")
    sam_results = run_sam_tests(api_key)

    out_path = "docs/USASPENDING_SAM_API_TEST_REPORT.md"
    write_report(usaspending_results, sam_results, out_path)
    print(f"Report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
