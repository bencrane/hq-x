"""Tests for app.services.dub_webhook_bootstrap.ensure_webhook_registered.

Pure-logic — patches dub_webhooks_repo + dub_client so nothing hits Dub
or the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.config import settings
from app.dmaas import dub_webhooks_repo
from app.dmaas.dub_webhooks_repo import DubWebhookRecord
from app.providers.dub import client as dub_client
from app.services import dub_webhook_bootstrap


@pytest.fixture
def stub_repo(monkeypatch):
    state: dict[str, Any] = {"rows": [], "find_calls": 0}

    async def fake_find_active(*, environment, receiver_url):
        state["find_calls"] += 1
        for r in state["rows"]:
            if (
                r.environment == environment
                and r.receiver_url == receiver_url
                and r.is_active
            ):
                return r
        return None

    async def fake_insert(**kwargs):
        rec = DubWebhookRecord(
            id=uuid4(),
            dub_webhook_id=kwargs["dub_webhook_id"],
            name=kwargs["name"],
            receiver_url=kwargs["receiver_url"],
            secret_hash=kwargs.get("secret_hash"),
            triggers=list(kwargs.get("triggers") or []),
            environment=kwargs["environment"],
            is_active=kwargs.get("is_active", True),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        state["rows"].append(rec)
        return rec

    monkeypatch.setattr(
        dub_webhooks_repo, "find_active_for_receiver", fake_find_active
    )
    monkeypatch.setattr(dub_webhooks_repo, "insert_dub_webhook", fake_insert)
    monkeypatch.setattr(
        dub_webhook_bootstrap.dub_webhooks_repo,
        "find_active_for_receiver",
        fake_find_active,
    )
    monkeypatch.setattr(
        dub_webhook_bootstrap.dub_webhooks_repo, "insert_dub_webhook", fake_insert
    )
    return state


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "DUB_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "DUB_WEBHOOK_SECRET", SecretStr("wh-secret"))
    monkeypatch.setattr(settings, "APP_ENV", "dev")
    yield


@pytest.mark.asyncio
async def test_first_call_creates_in_dub_and_inserts_row(
    configured, stub_repo, monkeypatch
):
    create_calls: list[dict[str, Any]] = []

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return {"id": "wh_new"}

    monkeypatch.setattr(dub_client, "create_webhook", fake_create)
    monkeypatch.setattr(
        dub_webhook_bootstrap.dub_client, "create_webhook", fake_create
    )

    rec = await dub_webhook_bootstrap.ensure_webhook_registered(
        receiver_url="https://api.hq-x.com/webhooks/dub"
    )
    assert rec.dub_webhook_id == "wh_new"
    assert rec.environment == "dev"
    assert rec.triggers == dub_webhook_bootstrap.DEFAULT_TRIGGERS
    assert len(create_calls) == 1
    assert create_calls[0]["url"] == "https://api.hq-x.com/webhooks/dub"
    assert len(stub_repo["rows"]) == 1


@pytest.mark.asyncio
async def test_second_call_same_env_url_returns_existing_no_dub_call(
    configured, stub_repo, monkeypatch
):
    # Pre-seed a row.
    await dub_webhooks_repo.insert_dub_webhook(
        dub_webhook_id="wh_old",
        name="hq-x:dev",
        receiver_url="https://api.hq-x.com/webhooks/dub",
        triggers=["link.clicked"],
        environment="dev",
    )

    def boom(**_):
        raise AssertionError("Dub create should not be called when active row exists")

    monkeypatch.setattr(dub_client, "create_webhook", boom)
    monkeypatch.setattr(dub_webhook_bootstrap.dub_client, "create_webhook", boom)

    rec = await dub_webhook_bootstrap.ensure_webhook_registered(
        receiver_url="https://api.hq-x.com/webhooks/dub"
    )
    assert rec.dub_webhook_id == "wh_old"
    assert len(stub_repo["rows"]) == 1


@pytest.mark.asyncio
async def test_different_env_creates_separately(configured, stub_repo, monkeypatch):
    await dub_webhooks_repo.insert_dub_webhook(
        dub_webhook_id="wh_dev",
        name="hq-x:dev",
        receiver_url="https://api.hq-x.com/webhooks/dub",
        triggers=["link.clicked"],
        environment="dev",
    )

    create_calls: list[dict[str, Any]] = []

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return {"id": "wh_stg"}

    monkeypatch.setattr(dub_client, "create_webhook", fake_create)
    monkeypatch.setattr(
        dub_webhook_bootstrap.dub_client, "create_webhook", fake_create
    )

    rec = await dub_webhook_bootstrap.ensure_webhook_registered(
        receiver_url="https://api.hq-x.com/webhooks/dub",
        environment="stg",
    )
    assert rec.dub_webhook_id == "wh_stg"
    assert rec.environment == "stg"
    assert len(create_calls) == 1


@pytest.mark.asyncio
async def test_different_url_creates_separately(configured, stub_repo, monkeypatch):
    await dub_webhooks_repo.insert_dub_webhook(
        dub_webhook_id="wh_one",
        name="hq-x:dev",
        receiver_url="https://api.hq-x.com/webhooks/dub",
        triggers=["link.clicked"],
        environment="dev",
    )

    def fake_create(**kwargs):
        return {"id": "wh_two"}

    monkeypatch.setattr(dub_client, "create_webhook", fake_create)
    monkeypatch.setattr(
        dub_webhook_bootstrap.dub_client, "create_webhook", fake_create
    )

    rec = await dub_webhook_bootstrap.ensure_webhook_registered(
        receiver_url="https://other.example.com/wh"
    )
    assert rec.dub_webhook_id == "wh_two"


@pytest.mark.asyncio
async def test_secret_is_hashed_not_stored(configured, stub_repo, monkeypatch):
    monkeypatch.setattr(
        dub_client, "create_webhook", lambda **_: {"id": "wh_new"}
    )
    monkeypatch.setattr(
        dub_webhook_bootstrap.dub_client,
        "create_webhook",
        lambda **_: {"id": "wh_new"},
    )

    rec = await dub_webhook_bootstrap.ensure_webhook_registered(
        receiver_url="https://api.hq-x.com/webhooks/dub"
    )
    assert rec.secret_hash is not None
    assert rec.secret_hash != "wh-secret"
    assert len(rec.secret_hash) == 64  # sha256 hex digest length
