#!/usr/bin/env python3
"""Ingest SOC and O*NET lookup datasets into lookup schema tables.

Usage:
SUPER_ADMIN_JWT_SECRET=unused-for-ingest doppler run --project data-engine-x-api --config prd -- python3 scripts/ingest_soc_onet_lookups.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# Allow running as `python3 scripts/...` from repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings

try:
    from psycopg import connect
except ModuleNotFoundError:
    venv_python = REPO_ROOT / ".venv/bin/python3"
    if venv_python.exists() and Path(sys.executable) != venv_python:
        os.execv(str(venv_python), [str(venv_python), *sys.argv])
    raise

SOC_SOURCE_PATH = REPO_ROOT / "docs/api-reference-docs/soc/soc2018_all.csv"
ONET_BASE_PATH = REPO_ROOT / "docs/api-reference-docs/onet/db_30_2_text"
ONET_CROSSWALK_SOURCE_PATH = REPO_ROOT / "docs/api-reference-docs/onet/2019_to_SOC_Crosswalk.csv"

SOC_EXPECTED_COUNTS = {2: 23, 3: 98, 5: 459, 6: 867}
SOC_EXPECTED_TOTAL = 1447
ONET_EXPECTED_OCCUPATIONS = 1016


@dataclass
class IngestSummary:
    upserted_rows: dict[str, int]
    table_counts: dict[str, int]
    soc_level_distribution: dict[int, int]
    join_count: int
    machinists_check: dict[str, Any]
    cnc_machinist_title_exists: bool


def _parse_date_mm_yyyy(raw_value: str | None) -> date | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value or value.lower() == "n/a":
        return None

    parts = value.split("/")
    if len(parts) != 2:
        return None

    month_raw, year_raw = parts
    try:
        month = int(month_raw)
        year = int(year_raw)
    except ValueError:
        return None

    if month < 1 or month > 12:
        return None
    return date(year, month, 1)


def _parse_float(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value or value.lower() == "n/a":
        return None
    return float(value)


def _parse_int(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value or value.lower() == "n/a":
        return None
    return int(float(value))


def _parse_bool_yn(raw_value: str | None) -> bool | None:
    if raw_value is None:
        return None
    value = raw_value.strip().upper()
    if value == "Y":
        return True
    if value == "N":
        return False
    return None


def _normalize_text(raw_value: str | None, default: str = "") -> str:
    if raw_value is None:
        return default
    return raw_value.strip()


def _normalize_nullable_text(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    return value if value else None


def _resolve_onet_data_dir() -> Path:
    nested_path = ONET_BASE_PATH / "db_30_2_text"
    if nested_path.exists():
        return nested_path
    if ONET_BASE_PATH.exists():
        return ONET_BASE_PATH
    raise RuntimeError(f"O*NET source directory not found at {ONET_BASE_PATH}")


def _read_soc_rows() -> tuple[list[tuple[Any, ...]], dict[int, int]]:
    if not SOC_SOURCE_PATH.exists():
        raise RuntimeError(f"SOC source file not found: {SOC_SOURCE_PATH}")

    rows: list[tuple[Any, ...]] = []
    level_counts: dict[int, int] = {}
    with SOC_SOURCE_PATH.open("r", encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            soc_code = _normalize_text(row["code"])
            title = _normalize_text(row["title"])
            level = int(_normalize_text(row["Level"]))
            hierarchical_structure = _normalize_text(row["Hierarchical_structure"])
            parent = _normalize_nullable_text(row.get("parent"))
            if parent == "NA":
                parent = None

            major_group_code = _normalize_text(row["soc2d"])
            minor_group_code = _normalize_nullable_text(row.get("soc3d"))
            broad_group_code = _normalize_nullable_text(row.get("soc5d"))
            detailed_occupation_code = _normalize_nullable_text(row.get("soc6d"))

            if minor_group_code == "NA":
                minor_group_code = None
            if broad_group_code == "NA":
                broad_group_code = None
            if detailed_occupation_code == "NA":
                detailed_occupation_code = None

            rows.append(
                (
                    soc_code,
                    title,
                    level,
                    hierarchical_structure,
                    parent,
                    major_group_code,
                    minor_group_code,
                    broad_group_code,
                    detailed_occupation_code,
                )
            )
            level_counts[level] = level_counts.get(level, 0) + 1

    if len(rows) != SOC_EXPECTED_TOTAL:
        raise RuntimeError(f"Expected {SOC_EXPECTED_TOTAL} SOC rows, found {len(rows)}")
    if level_counts != SOC_EXPECTED_COUNTS:
        raise RuntimeError(
            f"SOC level distribution mismatch. Expected {SOC_EXPECTED_COUNTS}, found {level_counts}"
        )

    return rows, level_counts


def _read_tsv_dict_rows(file_path: Path) -> list[dict[str, str]]:
    with file_path.open("r", encoding="utf-8-sig", newline="") as tsv_file:
        reader = csv.DictReader(tsv_file, delimiter="\t")
        return list(reader)


def _read_onet_occupation_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Occupation Data.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        onet_soc_code = _normalize_text(row["O*NET-SOC Code"])
        soc_code = onet_soc_code.split(".", 1)[0]
        rows.append(
            (
                onet_soc_code,
                soc_code,
                _normalize_text(row["Title"]),
                _normalize_nullable_text(row.get("Description")),
            )
        )

    if len(rows) != ONET_EXPECTED_OCCUPATIONS:
        raise RuntimeError(f"Expected {ONET_EXPECTED_OCCUPATIONS} O*NET occupations, found {len(rows)}")
    return rows


def _read_onet_alternate_title_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Alternate Titles.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _normalize_text(row["Alternate Title"]),
                _normalize_text(row.get("Short Title"), default=""),
                _normalize_text(row.get("Source(s)"), default=""),
            )
        )
    return rows


def _read_onet_skills_like_rows(
    file_path: Path,
    filter_recommend_suppress: bool,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for row in _read_tsv_dict_rows(file_path):
        recommend_suppress = _normalize_text(row.get("Recommend Suppress"), default="N")
        if filter_recommend_suppress and recommend_suppress.upper() == "Y":
            continue

        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _normalize_text(row["Element ID"]),
                _normalize_text(row["Element Name"]),
                _normalize_text(row["Scale ID"]),
                _parse_float(row.get("Data Value")),
                _parse_int(row.get("N")),
                _parse_float(row.get("Standard Error")),
                _parse_float(row.get("Lower CI Bound")),
                _parse_float(row.get("Upper CI Bound")),
                _normalize_nullable_text(row.get("Not Relevant")),
                _parse_date_mm_yyyy(row.get("Date")),
                _normalize_nullable_text(row.get("Domain Source")),
            )
        )
    return rows


def _read_onet_task_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Task Statements.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _parse_int(row.get("Task ID")),
                _normalize_text(row["Task"]),
                _normalize_nullable_text(row.get("Task Type")),
                _parse_int(row.get("Incumbents Responding")),
                _parse_date_mm_yyyy(row.get("Date")),
                _normalize_nullable_text(row.get("Domain Source")),
            )
        )
    return rows


def _read_onet_education_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Education, Training, and Experience.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _normalize_text(row["Element ID"]),
                _normalize_text(row["Element Name"]),
                _normalize_text(row["Scale ID"]),
                _normalize_text(row.get("Category"), default="n/a"),
                _parse_float(row.get("Data Value")),
                _parse_int(row.get("N")),
                _parse_float(row.get("Standard Error")),
                _parse_float(row.get("Lower CI Bound")),
                _parse_float(row.get("Upper CI Bound")),
                _parse_date_mm_yyyy(row.get("Date")),
                _normalize_nullable_text(row.get("Domain Source")),
            )
        )
    return rows


def _read_onet_job_zone_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Job Zones.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _parse_int(row.get("Job Zone")),
                _parse_date_mm_yyyy(row.get("Date")),
                _normalize_nullable_text(row.get("Domain Source")),
            )
        )
    return rows


def _read_onet_technology_skill_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Technology Skills.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _normalize_text(row["Example"]),
                _normalize_text(row["Commodity Code"]),
                _normalize_nullable_text(row.get("Commodity Title")),
                _parse_bool_yn(row.get("Hot Technology")),
                _parse_bool_yn(row.get("In Demand")),
            )
        )
    return rows


def _read_onet_crosswalk_rows() -> list[tuple[Any, ...]]:
    if not ONET_CROSSWALK_SOURCE_PATH.exists():
        raise RuntimeError(f"O*NET crosswalk file not found: {ONET_CROSSWALK_SOURCE_PATH}")

    rows = []
    with ONET_CROSSWALK_SOURCE_PATH.open("r", encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            rows.append(
                (
                    _normalize_text(row["O*NET-SOC 2019 Code"]),
                    _normalize_text(row["O*NET-SOC 2019 Title"]),
                    _normalize_text(row["2018 SOC Code"]),
                    _normalize_text(row["2018 SOC Title"]),
                )
            )
    return rows


def _read_onet_reported_title_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Sample of Reported Titles.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _normalize_text(row["Reported Job Title"]),
                _parse_bool_yn(row.get("Shown in My Next Move")),
            )
        )
    return rows


def _read_onet_related_occupation_rows(onet_dir: Path) -> list[tuple[Any, ...]]:
    file_path = onet_dir / "Related Occupations.txt"
    rows = []
    for row in _read_tsv_dict_rows(file_path):
        rows.append(
            (
                _normalize_text(row["O*NET-SOC Code"]),
                _normalize_text(row["Related O*NET-SOC Code"]),
                _normalize_text(row["Relatedness Tier"]),
                _parse_int(row.get("Index")),
            )
        )
    return rows


def _upsert_all(
    soc_rows: list[tuple[Any, ...]],
    onet_occupation_rows: list[tuple[Any, ...]],
    onet_alternate_title_rows: list[tuple[Any, ...]],
    onet_skill_rows: list[tuple[Any, ...]],
    onet_task_rows: list[tuple[Any, ...]],
    onet_education_rows: list[tuple[Any, ...]],
    onet_job_zone_rows: list[tuple[Any, ...]],
    onet_technology_rows: list[tuple[Any, ...]],
    onet_crosswalk_rows: list[tuple[Any, ...]],
    onet_knowledge_rows: list[tuple[Any, ...]],
    onet_reported_title_rows: list[tuple[Any, ...]],
    onet_related_occupation_rows: list[tuple[Any, ...]],
    soc_level_distribution: dict[int, int],
) -> IngestSummary:
    settings = get_settings()

    sql_soc = """
    INSERT INTO lookup.soc_codes (
        soc_code, title, level, hierarchical_structure, parent_soc_code,
        major_group_code, minor_group_code, broad_group_code, detailed_occupation_code, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (soc_code)
    DO UPDATE SET
        title = EXCLUDED.title,
        level = EXCLUDED.level,
        hierarchical_structure = EXCLUDED.hierarchical_structure,
        parent_soc_code = EXCLUDED.parent_soc_code,
        major_group_code = EXCLUDED.major_group_code,
        minor_group_code = EXCLUDED.minor_group_code,
        broad_group_code = EXCLUDED.broad_group_code,
        detailed_occupation_code = EXCLUDED.detailed_occupation_code,
        updated_at = NOW()
    """

    sql_onet_occupations = """
    INSERT INTO lookup.onet_occupations (
        onet_soc_code, soc_code, title, description, updated_at
    )
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code)
    DO UPDATE SET
        soc_code = EXCLUDED.soc_code,
        title = EXCLUDED.title,
        description = EXCLUDED.description,
        updated_at = NOW()
    """

    sql_onet_alternate_titles = """
    INSERT INTO lookup.onet_alternate_titles (
        onet_soc_code, alternate_title, short_title, source_codes, updated_at
    )
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, alternate_title, short_title, source_codes)
    DO UPDATE SET
        updated_at = NOW()
    """

    sql_onet_skills = """
    INSERT INTO lookup.onet_skills (
        onet_soc_code, element_id, element_name, scale_id, data_value, sample_size,
        standard_error, lower_ci_bound, upper_ci_bound, not_relevant, observed_on, domain_source, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, element_id, scale_id)
    DO UPDATE SET
        element_name = EXCLUDED.element_name,
        data_value = EXCLUDED.data_value,
        sample_size = EXCLUDED.sample_size,
        standard_error = EXCLUDED.standard_error,
        lower_ci_bound = EXCLUDED.lower_ci_bound,
        upper_ci_bound = EXCLUDED.upper_ci_bound,
        not_relevant = EXCLUDED.not_relevant,
        observed_on = EXCLUDED.observed_on,
        domain_source = EXCLUDED.domain_source,
        updated_at = NOW()
    """

    sql_onet_tasks = """
    INSERT INTO lookup.onet_tasks (
        onet_soc_code, task_id, task, task_type, incumbents_responding, observed_on, domain_source, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, task_id)
    DO UPDATE SET
        task = EXCLUDED.task,
        task_type = EXCLUDED.task_type,
        incumbents_responding = EXCLUDED.incumbents_responding,
        observed_on = EXCLUDED.observed_on,
        domain_source = EXCLUDED.domain_source,
        updated_at = NOW()
    """

    sql_onet_education = """
    INSERT INTO lookup.onet_education (
        onet_soc_code, element_id, element_name, scale_id, category, data_value, sample_size,
        standard_error, lower_ci_bound, upper_ci_bound, observed_on, domain_source, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, element_id, scale_id, category)
    DO UPDATE SET
        element_name = EXCLUDED.element_name,
        data_value = EXCLUDED.data_value,
        sample_size = EXCLUDED.sample_size,
        standard_error = EXCLUDED.standard_error,
        lower_ci_bound = EXCLUDED.lower_ci_bound,
        upper_ci_bound = EXCLUDED.upper_ci_bound,
        observed_on = EXCLUDED.observed_on,
        domain_source = EXCLUDED.domain_source,
        updated_at = NOW()
    """

    sql_onet_job_zones = """
    INSERT INTO lookup.onet_job_zones (
        onet_soc_code, job_zone, observed_on, domain_source, updated_at
    )
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code)
    DO UPDATE SET
        job_zone = EXCLUDED.job_zone,
        observed_on = EXCLUDED.observed_on,
        domain_source = EXCLUDED.domain_source,
        updated_at = NOW()
    """

    sql_onet_technology_skills = """
    INSERT INTO lookup.onet_technology_skills (
        onet_soc_code, example, commodity_code, commodity_title, hot_technology, in_demand, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, example, commodity_code)
    DO UPDATE SET
        commodity_title = EXCLUDED.commodity_title,
        hot_technology = EXCLUDED.hot_technology,
        in_demand = EXCLUDED.in_demand,
        updated_at = NOW()
    """

    sql_onet_soc_crosswalk = """
    INSERT INTO lookup.onet_soc_crosswalk (
        onet_soc_code, onet_soc_title, soc_code, soc_title, updated_at
    )
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code)
    DO UPDATE SET
        onet_soc_title = EXCLUDED.onet_soc_title,
        soc_code = EXCLUDED.soc_code,
        soc_title = EXCLUDED.soc_title,
        updated_at = NOW()
    """

    sql_onet_knowledge = """
    INSERT INTO lookup.onet_knowledge (
        onet_soc_code, element_id, element_name, scale_id, data_value, sample_size,
        standard_error, lower_ci_bound, upper_ci_bound, not_relevant, observed_on, domain_source, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, element_id, scale_id)
    DO UPDATE SET
        element_name = EXCLUDED.element_name,
        data_value = EXCLUDED.data_value,
        sample_size = EXCLUDED.sample_size,
        standard_error = EXCLUDED.standard_error,
        lower_ci_bound = EXCLUDED.lower_ci_bound,
        upper_ci_bound = EXCLUDED.upper_ci_bound,
        not_relevant = EXCLUDED.not_relevant,
        observed_on = EXCLUDED.observed_on,
        domain_source = EXCLUDED.domain_source,
        updated_at = NOW()
    """

    sql_onet_reported_titles = """
    INSERT INTO lookup.onet_reported_titles (
        onet_soc_code, reported_job_title, shown_in_my_next_move, updated_at
    )
    VALUES (%s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, reported_job_title)
    DO UPDATE SET
        shown_in_my_next_move = EXCLUDED.shown_in_my_next_move,
        updated_at = NOW()
    """

    sql_onet_related_occupations = """
    INSERT INTO lookup.onet_related_occupations (
        onet_soc_code, related_onet_soc_code, relatedness_tier, relatedness_index, updated_at
    )
    VALUES (%s, %s, %s, %s, NOW())
    ON CONFLICT (onet_soc_code, related_onet_soc_code, relatedness_tier, relatedness_index)
    DO UPDATE SET
        updated_at = NOW()
    """

    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql_soc, soc_rows)
            cur.executemany(sql_onet_occupations, onet_occupation_rows)
            cur.executemany(sql_onet_alternate_titles, onet_alternate_title_rows)
            cur.executemany(sql_onet_skills, onet_skill_rows)
            cur.executemany(sql_onet_tasks, onet_task_rows)
            cur.executemany(sql_onet_education, onet_education_rows)
            cur.executemany(sql_onet_job_zones, onet_job_zone_rows)
            cur.executemany(sql_onet_technology_skills, onet_technology_rows)
            cur.executemany(sql_onet_soc_crosswalk, onet_crosswalk_rows)
            cur.executemany(sql_onet_knowledge, onet_knowledge_rows)
            cur.executemany(sql_onet_reported_titles, onet_reported_title_rows)
            cur.executemany(sql_onet_related_occupations, onet_related_occupation_rows)
            conn.commit()

            table_names = [
                "soc_codes",
                "onet_occupations",
                "onet_alternate_titles",
                "onet_skills",
                "onet_tasks",
                "onet_education",
                "onet_job_zones",
                "onet_technology_skills",
                "onet_soc_crosswalk",
                "onet_knowledge",
                "onet_reported_titles",
                "onet_related_occupations",
            ]
            table_counts: dict[str, int] = {}
            for table_name in table_names:
                cur.execute(f"SELECT COUNT(*) FROM lookup.{table_name}")
                table_counts[table_name] = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT COUNT(*)
                FROM lookup.onet_occupations o
                JOIN lookup.soc_codes s
                  ON o.soc_code = s.soc_code
                """
            )
            join_count = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT soc_code, title, level, parent_soc_code
                FROM lookup.soc_codes
                WHERE soc_code = '51-4041'
                """
            )
            row = cur.fetchone()
            machinists_check = {
                "soc_code": row[0] if row else None,
                "title": row[1] if row else None,
                "level": row[2] if row else None,
                "parent_soc_code": row[3] if row else None,
            }

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM lookup.onet_alternate_titles
                    WHERE onet_soc_code = '51-4041.00'
                      AND alternate_title ILIKE '%CNC Machinist%'
                )
                """
            )
            cnc_machinist_title_exists = bool(cur.fetchone()[0])

    upserted_rows = {
        "soc_codes": len(soc_rows),
        "onet_occupations": len(onet_occupation_rows),
        "onet_alternate_titles": len(onet_alternate_title_rows),
        "onet_skills": len(onet_skill_rows),
        "onet_tasks": len(onet_task_rows),
        "onet_education": len(onet_education_rows),
        "onet_job_zones": len(onet_job_zone_rows),
        "onet_technology_skills": len(onet_technology_rows),
        "onet_soc_crosswalk": len(onet_crosswalk_rows),
        "onet_knowledge": len(onet_knowledge_rows),
        "onet_reported_titles": len(onet_reported_title_rows),
        "onet_related_occupations": len(onet_related_occupation_rows),
    }

    return IngestSummary(
        upserted_rows=upserted_rows,
        table_counts=table_counts,
        soc_level_distribution=soc_level_distribution,
        join_count=join_count,
        machinists_check=machinists_check,
        cnc_machinist_title_exists=cnc_machinist_title_exists,
    )


def main() -> None:
    onet_data_dir = _resolve_onet_data_dir()

    soc_rows, soc_level_distribution = _read_soc_rows()
    onet_occupation_rows = _read_onet_occupation_rows(onet_data_dir)
    onet_alternate_title_rows = _read_onet_alternate_title_rows(onet_data_dir)
    onet_skill_rows = _read_onet_skills_like_rows(
        onet_data_dir / "Skills.txt",
        filter_recommend_suppress=True,
    )
    onet_task_rows = _read_onet_task_rows(onet_data_dir)
    onet_education_rows = _read_onet_education_rows(onet_data_dir)
    onet_job_zone_rows = _read_onet_job_zone_rows(onet_data_dir)
    onet_technology_rows = _read_onet_technology_skill_rows(onet_data_dir)
    onet_crosswalk_rows = _read_onet_crosswalk_rows()
    onet_knowledge_rows = _read_onet_skills_like_rows(
        onet_data_dir / "Knowledge.txt",
        filter_recommend_suppress=True,
    )
    onet_reported_title_rows = _read_onet_reported_title_rows(onet_data_dir)
    onet_related_occupation_rows = _read_onet_related_occupation_rows(onet_data_dir)

    summary = _upsert_all(
        soc_rows=soc_rows,
        onet_occupation_rows=onet_occupation_rows,
        onet_alternate_title_rows=onet_alternate_title_rows,
        onet_skill_rows=onet_skill_rows,
        onet_task_rows=onet_task_rows,
        onet_education_rows=onet_education_rows,
        onet_job_zone_rows=onet_job_zone_rows,
        onet_technology_rows=onet_technology_rows,
        onet_crosswalk_rows=onet_crosswalk_rows,
        onet_knowledge_rows=onet_knowledge_rows,
        onet_reported_title_rows=onet_reported_title_rows,
        onet_related_occupation_rows=onet_related_occupation_rows,
        soc_level_distribution=soc_level_distribution,
    )

    print(
        json.dumps(
            {
                "upserted_rows": summary.upserted_rows,
                "table_counts": summary.table_counts,
                "soc_level_distribution": summary.soc_level_distribution,
                "join_count": summary.join_count,
                "machinists_check": summary.machinists_check,
                "cnc_machinist_title_exists": summary.cnc_machinist_title_exists,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
