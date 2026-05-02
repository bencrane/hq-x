"""Structural tests for the GTM-initiative-attribution slice 1 migrations.

These migrations are CI-gated by ``scripts/migrate.py`` against a real
DB; here we statically lint the SQL files for the load-bearing DDL so a
silent edit (e.g. dropping the partial unique index predicate, or
turning the unique into a plain index) is caught at unit-test time.

The behavioral half — that the partial unique index enforces "one
active row per (initiative, recipient)" but allows re-insertion after
soft-delete — is exercised by the in-memory fake in
``test_initiative_recipient_memberships_service.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _read(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text()


def _norm(sql: str) -> str:
    """Whitespace-normalize SQL for substring matching across line breaks."""
    return re.sub(r"\s+", " ", sql)


def test_campaigns_initiative_link_migration_present() -> None:
    sql = _norm(_read("20260501T030000_campaigns_initiative_link.sql"))
    # Adds the column with the right FK behavior.
    assert "ALTER TABLE business.campaigns" in sql
    assert "ADD COLUMN initiative_id UUID NULL" in sql
    assert "REFERENCES business.gtm_initiatives(id)" in sql
    assert "ON DELETE RESTRICT" in sql
    # Partial index excludes legacy NULL rows from the index.
    assert "CREATE INDEX idx_campaigns_initiative" in sql
    assert "WHERE initiative_id IS NOT NULL" in sql


def test_channel_campaigns_initiative_denorm_migration_present() -> None:
    sql = _norm(
        _read("20260501T030100_channel_campaigns_initiative_denorm.sql")
    )
    assert "ALTER TABLE business.channel_campaigns" in sql
    assert "ADD COLUMN initiative_id UUID NULL" in sql
    assert "REFERENCES business.gtm_initiatives(id)" in sql
    assert "ON DELETE RESTRICT" in sql
    assert "CREATE INDEX idx_channel_campaigns_initiative" in sql
    assert "WHERE initiative_id IS NOT NULL" in sql


def test_initiative_recipient_memberships_table_shape() -> None:
    sql = _norm(_read("20260501T030200_initiative_recipient_memberships.sql"))
    # Table + the four critical FKs.
    assert (
        "CREATE TABLE business.initiative_recipient_memberships" in sql
    )
    assert "REFERENCES business.gtm_initiatives(id) ON DELETE RESTRICT" in sql
    assert "REFERENCES business.partner_contracts(id) ON DELETE RESTRICT" in sql
    assert "REFERENCES business.recipients(id) ON DELETE RESTRICT" in sql
    # data_engine_audience_id is denormalized + NOT NULL (frozen at
    # materialization time, no FK because DEX is a separate DB).
    assert "data_engine_audience_id UUID NOT NULL" in sql
    # Soft-delete columns must be present (load-bearing for the
    # active-uniqueness contract).
    assert "removed_at TIMESTAMPTZ" in sql
    assert "removed_reason TEXT" in sql


def test_active_membership_uniqueness_index_is_partial() -> None:
    """The whole point of the partial unique index is that soft-deleted
    rows don't block a re-add. A plain unique index would.
    """
    sql = _norm(_read("20260501T030200_initiative_recipient_memberships.sql"))
    assert (
        "CREATE UNIQUE INDEX uq_irm_active_recipient_per_initiative" in sql
    )
    assert (
        "ON business.initiative_recipient_memberships "
        "(initiative_id, recipient_id)" in sql
    )
    assert "WHERE removed_at IS NULL" in sql


def test_lookup_indexes_filter_active_rows() -> None:
    """The three lookup indexes (recipient, contract, audience-spec) all
    filter ``WHERE removed_at IS NULL`` — soft-deleted rows are off the
    hot read paths."""
    sql = _norm(_read("20260501T030200_initiative_recipient_memberships.sql"))
    for index_name in (
        "idx_irm_recipient_active",
        "idx_irm_contract_active",
        "idx_irm_audience_spec",
    ):
        assert index_name in sql, f"missing index {index_name}"
    # Each of those indexes is partial-on-active.
    assert sql.count("WHERE removed_at IS NULL") >= 4  # 1 unique + 3 lookup


@pytest.mark.parametrize(
    "filename",
    [
        "20260501T030000_campaigns_initiative_link.sql",
        "20260501T030100_channel_campaigns_initiative_denorm.sql",
        "20260501T030200_initiative_recipient_memberships.sql",
    ],
)
def test_migration_file_is_idempotent_safe(filename: str) -> None:
    """All three migrations are forward-only ALTERs / CREATEs. They run
    inside ``scripts/migrate.py`` which already gates on
    ``schema_migrations`` for re-runs, so the SQL itself doesn't need
    ``IF NOT EXISTS`` — but it must NOT contain any DROP that would
    silently undo prior schema (defence against an accidental edit)."""
    sql = _read(filename).upper()
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql
