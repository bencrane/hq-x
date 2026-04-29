"""Smoke test for the Vapi wrap routers + service.

Mirrors the structure of ``tests/smoke_voice_phase2.py`` (TestClient
harness, monkeypatched Vapi provider client) but uses a fake in-memory
DB so the smoke is hermetic — no migration needs to be applied to a
live database for the script to pass.

Run with::

    uv run python tests/smoke_vapi_wrap.py

Coverage (per directive Build 11):
  - Phone-number import: happy / already-imported / missing-creds
  - Phone-number bind: re-bind sets local FK + assistantId + serverUrl
  - Vapi outbound call: happy / missing Idempotency-Key / replay / Vapi error
  - Tools / squads / campaigns / files / knowledge-bases passthrough happy paths
  - Assistant /vapi extension: synced returns Vapi view, unsynced 404s
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

# Conftest populates env vars when pytest runs; do the same here so the
# script form works without pytest.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conftest  # noqa: F401  -- side effect: sets env vars

os.environ.setdefault("HQX_API_BASE_URL", "https://hqx-test.example")
os.environ.setdefault("VAPI_API_KEY", "test-vapi-key")
os.environ.setdefault("BRAND_CREDS_ENCRYPTION_KEY", "test-key-for-smoke")

from fastapi.testclient import TestClient  # noqa: E402

from app import db as db_module  # noqa: E402
from app.auth.flexible import SystemContext, require_flexible_auth  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.vapi import _http as vapi_http  # noqa: E402
from app.providers.vapi import client as vapi_client  # noqa: E402
from app.providers.vapi.errors import vapi_key as _vapi_key_resolver  # noqa: E402
from app.routers import vapi_analytics as vapi_analytics_router  # noqa: E402
from app.routers import vapi_calls as vapi_calls_router  # noqa: E402
from app.routers import vapi_campaigns as vapi_campaigns_router  # noqa: E402
from app.routers import vapi_files as vapi_files_router  # noqa: E402
from app.routers import vapi_insights as vapi_insights_router  # noqa: E402
from app.routers import vapi_knowledge_bases as vapi_kb_router  # noqa: E402
from app.routers import vapi_phone_numbers as vapi_phone_numbers_router  # noqa: E402
from app.routers import vapi_squads as vapi_squads_router  # noqa: E402
from app.routers import vapi_tools as vapi_tools_router  # noqa: E402
from app.routers import voice_ai as voice_ai_router  # noqa: E402
from app.services import brands as brands_svc  # noqa: E402
from app.services import vapi_calls as vapi_calls_svc  # noqa: E402

# ============================================================================
# Fake DB
# ============================================================================


class FakeDb:
    """In-memory store + a tiny SQL pattern matcher for the smoke surface."""

    def __init__(self) -> None:
        # Each table is keyed by primary key (id).
        self.voice_phone_numbers: dict[str, dict[str, Any]] = {}
        self.voice_assistants: dict[str, dict[str, Any]] = {}
        self.call_logs: dict[str, dict[str, Any]] = {}
        self.vapi_call_idempotency: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        self.voice_phone_numbers.clear()
        self.voice_assistants.clear()
        self.call_logs.clear()
        self.vapi_call_idempotency.clear()


FAKE_DB = FakeDb()


_VOICE_PHONE_NUMBER_COLS = [
    "id", "brand_id", "phone_number", "twilio_phone_number_sid",
    "vapi_phone_number_id", "voice_assistant_id", "label", "purpose",
    "status", "created_at", "updated_at",
]

_CALL_LOG_COLS = vapi_calls_svc._CALL_LOG_COLS


def _vpn_tuple(row: dict[str, Any]) -> tuple:
    return tuple(row.get(c) for c in _VOICE_PHONE_NUMBER_COLS)


def _call_log_tuple(row: dict[str, Any]) -> tuple:
    return tuple(row.get(c) for c in _CALL_LOG_COLS)


class FakeCursor:
    def __init__(self) -> None:
        self.description: list[tuple[str, ...]] = []
        self._result: Any = None

    async def execute(self, sql: str, params: Any = None) -> None:
        sql_norm = " ".join(sql.split()).strip()
        self._result, self.description = _dispatch(sql_norm, params or ())

    async def fetchone(self) -> Any:
        if isinstance(self._result, list):
            return self._result[0] if self._result else None
        return self._result

    async def fetchall(self) -> list[Any]:
        if isinstance(self._result, list):
            return self._result
        if self._result is None:
            return []
        return [self._result]


class FakeConn:
    @asynccontextmanager
    async def cursor(self):  # type: ignore[override]
        cur = FakeCursor()
        yield cur

    async def commit(self) -> None:
        return None


@asynccontextmanager
async def fake_get_db_connection():
    yield FakeConn()


# ----------------------------------------------------------------------------
# SQL dispatcher — matches the queries actually issued by the new routers /
# service. Returns (result, description). ``result`` is None for writes
# without RETURNING, a tuple for fetchone(), or a list for fetchall().
# ----------------------------------------------------------------------------


def _dispatch(sql: str, params: tuple) -> tuple[Any, list[tuple[str, ...]]]:
    # voice_phone_numbers SELECT (load_local + bind/get/release)
    if sql.startswith("SELECT id, brand_id, phone_number, twilio_phone_number_sid"):
        pn_id, brand_id = params[0], params[1]
        row = FAKE_DB.voice_phone_numbers.get(pn_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        return _vpn_tuple(row), [(c,) for c in _VOICE_PHONE_NUMBER_COLS]

    # voice_phone_numbers UPDATE in import (sets vapi_phone_number_id +
    # maybe voice_assistant_id) RETURNING
    if (
        "UPDATE voice_phone_numbers" in sql
        and "vapi_phone_number_id = %s" in sql
        and "RETURNING" in sql
    ):
        new_vapi_id, new_assistant_id, pn_id, brand_id = params
        row = FAKE_DB.voice_phone_numbers.get(pn_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        row["vapi_phone_number_id"] = new_vapi_id
        if new_assistant_id is not None:
            row["voice_assistant_id"] = new_assistant_id
        return _vpn_tuple(row), [(c,) for c in _VOICE_PHONE_NUMBER_COLS]

    # voice_phone_numbers UPDATE in bind (sets voice_assistant_id) RETURNING
    if (
        "UPDATE voice_phone_numbers" in sql
        and "voice_assistant_id = %s" in sql
        and "RETURNING" in sql
    ):
        new_assistant_id, pn_id, brand_id = params
        row = FAKE_DB.voice_phone_numbers.get(pn_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        row["voice_assistant_id"] = new_assistant_id
        return _vpn_tuple(row), [(c,) for c in _VOICE_PHONE_NUMBER_COLS]

    # voice_phone_numbers UPDATE in delete (clears vapi_phone_number_id, no RETURNING)
    if "UPDATE voice_phone_numbers" in sql and "vapi_phone_number_id = NULL" in sql:
        pn_id, brand_id = params
        row = FAKE_DB.voice_phone_numbers.get(pn_id)
        if row is not None and row.get("brand_id") == brand_id:
            row["vapi_phone_number_id"] = None
        return None, []

    # voice_phone_numbers list (vapi-mirror only)
    if (
        "FROM voice_phone_numbers" in sql
        and "vapi_phone_number_id IS NOT NULL" in sql
        and "ORDER BY created_at" in sql
    ):
        brand_id = params[0]
        rows = [
            _vpn_tuple(r) for r in FAKE_DB.voice_phone_numbers.values()
            if r.get("brand_id") == brand_id and r.get("vapi_phone_number_id")
        ]
        return rows, [(c,) for c in _VOICE_PHONE_NUMBER_COLS]

    # voice_assistants SELECT vapi_assistant_id
    if "SELECT vapi_assistant_id FROM voice_assistants" in sql or (
        sql.startswith("SELECT vapi_assistant_id") and "voice_assistants" in sql
    ):
        a_id, brand_id = params[0], params[1]
        row = FAKE_DB.voice_assistants.get(a_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        return (row.get("vapi_assistant_id"),), [("vapi_assistant_id",)]

    # voice_assistants SELECT vapi_phone_number_id, phone_number (in svc resolver)
    if "SELECT vapi_phone_number_id, phone_number FROM voice_phone_numbers" in sql:
        pn_id, brand_id = params[0], params[1]
        row = FAKE_DB.voice_phone_numbers.get(pn_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        return (
            (row.get("vapi_phone_number_id"), row.get("phone_number")),
            [("vapi_phone_number_id",), ("phone_number",)],
        )

    # voice_assistants full-row SELECT (used by voice_ai.get_assistant)
    if (
        "FROM voice_assistants" in sql
        and "WHERE id = %s AND brand_id = %s" in sql
        and "SELECT id" in sql
    ):
        a_id, brand_id = params[0], params[1]
        row = FAKE_DB.voice_assistants.get(a_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        cols_match = re.search(r"SELECT\s+(.*?)\s+FROM voice_assistants", sql)
        cols = [c.strip() for c in (cols_match.group(1) if cols_match else "id").split(",")]
        return tuple(row.get(c) for c in cols), [(c,) for c in cols]

    # call_logs INSERT (pre-create) RETURNING
    if "INSERT INTO call_logs" in sql and "RETURNING" in sql:
        (
            log_id, brand_id, partner_id, campaign_id,
            assistant_id, voice_phone_number_id,
            customer_number, metadata,
        ) = params
        row = {c: None for c in _CALL_LOG_COLS}
        row.update({
            "id": log_id, "brand_id": brand_id, "partner_id": partner_id,
            "campaign_id": campaign_id, "voice_assistant_id": assistant_id,
            "voice_phone_number_id": voice_phone_number_id,
            "direction": "outbound", "call_type": "outbound",
            "customer_number": customer_number, "status": "queued",
            "metadata": metadata,
        })
        FAKE_DB.call_logs[log_id] = row
        return _call_log_tuple(row), [(c,) for c in _CALL_LOG_COLS]

    # vapi_call_idempotency INSERT ... ON CONFLICT DO NOTHING RETURNING id
    if "INSERT INTO vapi_call_idempotency" in sql:
        idem_id, brand_id, key, log_id = params
        existing = next(
            (
                r for r in FAKE_DB.vapi_call_idempotency.values()
                if r["brand_id"] == brand_id and r["idempotency_key"] == key
            ),
            None,
        )
        if existing is not None:
            return None, []
        FAKE_DB.vapi_call_idempotency[idem_id] = {
            "id": idem_id, "brand_id": brand_id, "idempotency_key": key,
            "call_log_id": log_id, "vapi_call_id": None,
        }
        return (idem_id,), [("id",)]

    # vapi_call_idempotency lookup
    if "FROM vapi_call_idempotency" in sql and "WHERE i.brand_id" in sql:
        brand_id, key = params
        existing = next(
            (
                r for r in FAKE_DB.vapi_call_idempotency.values()
                if r["brand_id"] == brand_id and r["idempotency_key"] == key
            ),
            None,
        )
        if existing is None:
            return None, []
        return (existing["call_log_id"], existing["vapi_call_id"]), [
            ("call_log_id",), ("vapi_call_id",),
        ]

    # call_logs SELECT by id (after idempotency lookup)
    if sql.startswith("SELECT id, brand_id, partner_id, campaign_id, voice_assistant_id"):
        log_id = params[0]
        row = FAKE_DB.call_logs.get(log_id)
        if row is None:
            return None, []
        return _call_log_tuple(row), [(c,) for c in _CALL_LOG_COLS]

    # call_logs UPDATE vapi_call_id RETURNING
    if "UPDATE call_logs" in sql and "vapi_call_id = %s" in sql and "RETURNING" in sql:
        new_vapi_id, log_id = params
        row = FAKE_DB.call_logs.get(log_id)
        if row is None:
            return None, []
        row["vapi_call_id"] = new_vapi_id
        return _call_log_tuple(row), [(c,) for c in _CALL_LOG_COLS]

    # vapi_call_idempotency UPDATE vapi_call_id (no RETURNING)
    if "UPDATE vapi_call_idempotency" in sql and "vapi_call_id = %s" in sql:
        new_vapi_id, idem_id = params
        row = FAKE_DB.vapi_call_idempotency.get(idem_id)
        if row is not None:
            row["vapi_call_id"] = new_vapi_id
        return None, []

    # call_logs UPDATE end-call RETURNING
    if "UPDATE call_logs" in sql and "status = 'ended'" in sql and "RETURNING" in sql:
        log_id, brand_id = params
        row = FAKE_DB.call_logs.get(log_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        row["status"] = "ended"
        return _call_log_tuple(row), [(c,) for c in _CALL_LOG_COLS]

    # call_logs DELETE
    if sql.startswith("DELETE FROM call_logs"):
        log_id = params[0]
        FAKE_DB.call_logs.pop(log_id, None)
        return None, []

    # vapi_call_idempotency DELETE
    if sql.startswith("DELETE FROM vapi_call_idempotency"):
        idem_id = params[0]
        FAKE_DB.vapi_call_idempotency.pop(idem_id, None)
        return None, []

    # call_logs SELECT for list/get
    if "FROM call_logs" in sql and "ORDER BY created_at DESC" in sql:
        rows = [
            _call_log_tuple(r) for r in FAKE_DB.call_logs.values()
            if r.get("brand_id") == params[0] and r.get("vapi_call_id")
        ]
        return rows, [(c,) for c in _CALL_LOG_COLS]

    # call_logs SELECT by (id, brand_id) for /voice/calls/{id}
    if (
        sql.startswith(
            "SELECT id, brand_id, partner_id, campaign_id, "
            "voice_assistant_id, voice_phone_number_id"
        )
        and "WHERE id = %s AND brand_id = %s AND deleted_at IS NULL" in sql
    ):
        log_id, brand_id = params
        row = FAKE_DB.call_logs.get(log_id)
        if row is None or row.get("brand_id") != brand_id:
            return None, []
        return _call_log_tuple(row), [(c,) for c in _CALL_LOG_COLS]

    # Unknown queries — fail loudly so we know to extend the dispatcher.
    raise AssertionError(f"FakeDb: unhandled SQL: {sql[:200]}  params={params!r}")


# ============================================================================
# Helpers — Vapi client mocks
# ============================================================================


class VapiCallTracker:
    def __init__(self) -> None:
        self.calls: dict[str, list[tuple[Any, ...]]] = {}

    def record(self, name: str, args: tuple[Any, ...]) -> None:
        self.calls.setdefault(name, []).append(args)

    def count(self, name: str) -> int:
        return len(self.calls.get(name, []))


def _patch_vapi(tracker: VapiCallTracker, **overrides: Any) -> None:
    """Replace vapi_client functions with tracking stubs.

    Each override is a callable (api_key, *args, **kwargs) -> response.
    Names not overridden default to a recording stub returning ``{"ok": True}``.
    """
    names = [
        "create_assistant", "get_assistant", "update_assistant",
        "delete_assistant", "list_assistants",
        "create_call", "get_call", "list_calls", "update_call", "delete_call",
        "import_phone_number", "get_phone_number", "list_phone_numbers",
        "update_phone_number", "delete_phone_number",
        "create_tool", "get_tool", "list_tools", "update_tool", "delete_tool",
        "create_squad", "get_squad", "list_squads", "update_squad", "delete_squad",
        "create_campaign", "get_campaign", "list_campaigns",
        "update_campaign", "delete_campaign",
        "create_file", "get_file", "list_files", "update_file", "delete_file",
        "create_knowledge_base", "get_knowledge_base", "list_knowledge_bases",
        "update_knowledge_base", "delete_knowledge_base",
        "query_analytics",
        "create_insight", "get_insight", "list_insights",
        "update_insight", "delete_insight",
        "preview_insight", "run_insight",
    ]

    def make_stub(name: str, override: Any) -> Any:
        def _stub(*args: Any, **kwargs: Any) -> Any:
            tracker.record(name, args + tuple(sorted(kwargs.items())))
            if override is not None:
                return override(*args, **kwargs)
            return {"ok": True, "name": name}
        return _stub

    for name in names:
        stub = make_stub(name, overrides.get(name))
        setattr(vapi_client, name, stub)
        # Also patch the names imported into router/service modules.
        for module in (
            vapi_calls_router, vapi_calls_svc, vapi_phone_numbers_router,
            vapi_tools_router, vapi_squads_router, vapi_campaigns_router,
            vapi_files_router, vapi_kb_router, voice_ai_router,
            vapi_analytics_router, vapi_insights_router,
        ):
            if hasattr(module, "vapi_client"):
                setattr(module.vapi_client, name, stub)


# ============================================================================
# Auth + DB injection
# ============================================================================


app.dependency_overrides[require_flexible_auth] = lambda: SystemContext()


def _install_fake_db() -> None:
    """Replace get_db_connection in every module that imported it directly."""
    db_module.get_db_connection = fake_get_db_connection  # type: ignore[assignment]
    targets = [
        "app.routers.vapi_phone_numbers",
        "app.routers.vapi_calls",
        "app.routers.voice_ai",
        "app.services.vapi_calls",
    ]
    import importlib
    for target in targets:
        m = importlib.import_module(target)
        if hasattr(m, "get_db_connection"):
            m.get_db_connection = fake_get_db_connection  # type: ignore[attr-defined]


# Stub brand twilio creds so import_into_vapi can resolve them.
class _FakeCreds:
    account_sid = "ACtest"
    auth_token = "auth_test"


async def _fake_get_twilio_creds(brand_id: UUID) -> _FakeCreds | None:  # noqa: ARG001
    return _FakeCreds()


# ============================================================================
# Test cases
# ============================================================================


def _make_brand_id() -> str:
    return str(uuid4())


def _seed_phone_number(
    brand_id: str,
    *,
    twilio_sid: str | None = "PNtwilio",
    vapi_id: str | None = None,
    assistant_id: str | None = None,
) -> str:
    pn_id = str(uuid4())
    FAKE_DB.voice_phone_numbers[pn_id] = {
        "id": pn_id, "brand_id": brand_id,
        "phone_number": "+15551234567",
        "twilio_phone_number_sid": twilio_sid,
        "vapi_phone_number_id": vapi_id,
        "voice_assistant_id": assistant_id,
        "label": None, "purpose": "both",
        "status": "active",
        "created_at": "2026-04-28T00:00:00Z",
        "updated_at": "2026-04-28T00:00:00Z",
    }
    return pn_id


def _seed_assistant(
    brand_id: str,
    *,
    vapi_assistant_id: str | None = "asst_v1",
) -> str:
    a_id = str(uuid4())
    FAKE_DB.voice_assistants[a_id] = {
        "id": a_id, "brand_id": brand_id,
        "partner_id": None, "campaign_id": None,
        "name": "smoke", "assistant_type": "outbound_qualifier",
        "vapi_assistant_id": vapi_assistant_id,
        "system_prompt": None, "first_message": None,
        "first_message_mode": "assistant-speaks-first",
        "model_config": None, "voice_config": None,
        "transcriber_config": None, "tools_config": None,
        "analysis_config": None, "max_duration_seconds": 600,
        "metadata": None, "status": "active",
        "created_at": "2026-04-28T00:00:00Z",
        "updated_at": "2026-04-28T00:00:00Z",
    }
    return a_id


def t_phone_number_import_happy(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    pn_id = _seed_phone_number(brand_id)
    a_id = _seed_assistant(brand_id, vapi_assistant_id="asst_v1")
    tracker = VapiCallTracker()
    _patch_vapi(
        tracker,
        import_phone_number=lambda *a, **k: {"id": "vapi_pn_x"},
        update_phone_number=lambda *a, **k: {"id": "vapi_pn_x", "ok": True},
    )
    resp = client.post(
        f"/api/brands/{brand_id}/vapi/phone-numbers/import",
        json={"voice_phone_number_id": pn_id, "assistant_id": a_id},
    )
    if resp.status_code != 201:
        failures.append(f"import_happy status={resp.status_code} body={resp.text}")
        return
    body = resp.json()
    if body["vapi_phone_number_id"] != "vapi_pn_x":
        failures.append(f"import_happy vapi_phone_number_id={body['vapi_phone_number_id']!r}")
    if not body["server_url"].endswith("/api/v1/vapi/webhook"):
        failures.append(f"import_happy server_url={body['server_url']!r}")
    if FAKE_DB.voice_phone_numbers[pn_id]["vapi_phone_number_id"] != "vapi_pn_x":
        failures.append("import_happy local row not updated with vapi_phone_number_id")
    if tracker.count("import_phone_number") != 1 or tracker.count("update_phone_number") != 1:
        failures.append(
            "import_happy expected one import + one update_phone_number call; "
            f"got import={tracker.count('import_phone_number')} "
            f"update={tracker.count('update_phone_number')}"
        )
    print("[ok] phone-number import happy path")


def t_phone_number_import_already_imported(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    pn_id = _seed_phone_number(brand_id, vapi_id="vapi_pn_existing")
    tracker = VapiCallTracker()
    _patch_vapi(tracker)
    resp = client.post(
        f"/api/brands/{brand_id}/vapi/phone-numbers/import",
        json={"voice_phone_number_id": pn_id},
    )
    if resp.status_code != 409:
        failures.append(f"already_imported status={resp.status_code} body={resp.text}")
        return
    detail = resp.json().get("detail", {})
    if detail.get("error") != "already_imported":
        failures.append(f"already_imported error key={detail!r}")
    print("[ok] phone-number import already-imported -> 409")


def t_phone_number_import_no_creds(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    pn_id = _seed_phone_number(brand_id)
    tracker = VapiCallTracker()
    _patch_vapi(tracker)

    async def _no_creds(_brand_id: UUID) -> None:
        raise brands_svc.BrandCredsKeyMissing("missing for smoke")

    original = brands_svc.get_twilio_creds
    brands_svc.get_twilio_creds = _no_creds  # type: ignore[assignment]
    vapi_phone_numbers_router.brands_svc.get_twilio_creds = _no_creds  # type: ignore[assignment]
    try:
        resp = client.post(
            f"/api/brands/{brand_id}/vapi/phone-numbers/import",
            json={"voice_phone_number_id": pn_id},
        )
    finally:
        brands_svc.get_twilio_creds = original  # type: ignore[assignment]
        vapi_phone_numbers_router.brands_svc.get_twilio_creds = original  # type: ignore[assignment]
    if resp.status_code != 503:
        failures.append(f"no_creds status={resp.status_code} body={resp.text}")
        return
    print("[ok] phone-number import missing-creds -> 503")


def t_phone_number_bind(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    pn_id = _seed_phone_number(brand_id, vapi_id="vapi_pn_bound")
    a_id = _seed_assistant(brand_id, vapi_assistant_id="asst_v9")

    captured: dict[str, Any] = {}

    def _capture_update(api_key: str, vapi_id: str, **kwargs: Any) -> dict[str, Any]:
        captured["args"] = (vapi_id, kwargs)
        return {"id": vapi_id, "ok": True}

    tracker = VapiCallTracker()
    _patch_vapi(tracker, update_phone_number=_capture_update)
    resp = client.patch(
        f"/api/brands/{brand_id}/vapi/phone-numbers/{pn_id}/bind",
        json={"assistant_id": a_id},
    )
    if resp.status_code != 200:
        failures.append(f"bind status={resp.status_code} body={resp.text}")
        return
    if FAKE_DB.voice_phone_numbers[pn_id]["voice_assistant_id"] != a_id:
        failures.append("bind didn't update voice_assistant_id locally")
    args = captured.get("args", (None, {}))
    if args[1].get("assistant_id") != "asst_v9":
        failures.append(f"bind didn't pass assistant_id to Vapi update: {args!r}")
    if not (args[1].get("server_url") or "").endswith("/api/v1/vapi/webhook"):
        failures.append(f"bind didn't pass server_url to Vapi update: {args!r}")
    print("[ok] phone-number bind sets voice_assistant_id + Vapi update")


def t_vapi_outbound_happy(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    a_id = _seed_assistant(brand_id, vapi_assistant_id="asst_v1")
    pn_id = _seed_phone_number(brand_id, vapi_id="vapi_pn_x")
    tracker = VapiCallTracker()
    _patch_vapi(tracker, create_call=lambda *a, **k: {"id": "vapi_call_x"})
    resp = client.post(
        f"/api/brands/{brand_id}/voice/calls",
        json={
            "assistant_id": a_id,
            "voice_phone_number_id": pn_id,
            "customer_number": "+15555550100",
        },
        headers={"Idempotency-Key": "key-1"},
    )
    if resp.status_code != 201:
        failures.append(f"outbound_happy status={resp.status_code} body={resp.text}")
        return
    body = resp.json()
    if body["call_log"]["vapi_call_id"] != "vapi_call_x":
        failures.append(f"outbound_happy vapi_call_id={body['call_log'].get('vapi_call_id')!r}")
    if body["idempotent_replay"] is not False:
        failures.append("outbound_happy idempotent_replay should be False")
    if len(FAKE_DB.vapi_call_idempotency) != 1:
        failures.append(f"outbound_happy idempotency rows={len(FAKE_DB.vapi_call_idempotency)}")
    print("[ok] vapi outbound call happy path")


def t_vapi_outbound_missing_idempotency_key(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    a_id = _seed_assistant(brand_id)
    pn_id = _seed_phone_number(brand_id, vapi_id="vapi_pn_x")
    tracker = VapiCallTracker()
    _patch_vapi(tracker)
    resp = client.post(
        f"/api/brands/{brand_id}/voice/calls",
        json={
            "assistant_id": a_id,
            "voice_phone_number_id": pn_id,
            "customer_number": "+15555550100",
        },
    )
    if resp.status_code != 400:
        failures.append(f"missing_idem status={resp.status_code} body={resp.text}")
        return
    detail = resp.json().get("detail", {})
    if detail.get("error") != "idempotency_key_required":
        failures.append(f"missing_idem error={detail!r}")
    if tracker.count("create_call") != 0:
        failures.append("missing_idem should not have called Vapi create_call")
    print("[ok] vapi outbound missing Idempotency-Key -> 400")


def t_vapi_outbound_idempotency_replay(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    a_id = _seed_assistant(brand_id, vapi_assistant_id="asst_v1")
    pn_id = _seed_phone_number(brand_id, vapi_id="vapi_pn_x")
    tracker = VapiCallTracker()
    _patch_vapi(tracker, create_call=lambda *a, **k: {"id": "vapi_call_y"})
    body = {
        "assistant_id": a_id,
        "voice_phone_number_id": pn_id,
        "customer_number": "+15555550100",
    }
    headers = {"Idempotency-Key": "shared-key"}

    r1 = client.post(f"/api/brands/{brand_id}/voice/calls", json=body, headers=headers)
    r2 = client.post(f"/api/brands/{brand_id}/voice/calls", json=body, headers=headers)
    if r1.status_code != 201 or r2.status_code != 201:
        failures.append(f"idem_replay r1={r1.status_code} r2={r2.status_code}")
        return
    if r1.json()["call_log"]["id"] != r2.json()["call_log"]["id"]:
        failures.append("idem_replay returned different call_log_ids")
    if r2.json().get("idempotent_replay") is not True:
        failures.append("idem_replay second response should have idempotent_replay=True")
    if tracker.count("create_call") != 1:
        failures.append(
            f"idem_replay create_call called {tracker.count('create_call')} times (expected 1)"
        )
    print("[ok] vapi outbound replayed Idempotency-Key reuses call_log + skips Vapi")


def t_vapi_outbound_provider_error(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    a_id = _seed_assistant(brand_id, vapi_assistant_id="asst_v1")
    pn_id = _seed_phone_number(brand_id, vapi_id="vapi_pn_x")

    def _boom(*a: Any, **k: Any) -> Any:
        raise vapi_http.VapiProviderError("Vapi connectivity error: simulated 503")

    tracker = VapiCallTracker()
    _patch_vapi(tracker, create_call=_boom)
    resp = client.post(
        f"/api/brands/{brand_id}/voice/calls",
        json={
            "assistant_id": a_id,
            "voice_phone_number_id": pn_id,
            "customer_number": "+15555550100",
        },
        headers={"Idempotency-Key": "boom-key"},
    )
    if resp.status_code != 503:
        failures.append(f"provider_error status={resp.status_code} body={resp.text}")
        return
    if FAKE_DB.call_logs:
        failures.append(
            f"provider_error left call_logs rows: {list(FAKE_DB.call_logs)!r}"
        )
    if FAKE_DB.vapi_call_idempotency:
        failures.append(
            f"provider_error left idempotency rows: {list(FAKE_DB.vapi_call_idempotency)!r}"
        )
    print("[ok] vapi outbound provider error -> 503 + cleans up rows")


def t_passthrough_resources(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    tracker = VapiCallTracker()
    _patch_vapi(
        tracker,
        create_tool=lambda key, cfg: {"id": "tool_1", "echo": cfg},
        create_squad=lambda key, cfg: {"id": "squad_1", "echo": cfg},
        create_campaign=lambda key, cfg: {"id": "camp_1", "echo": cfg},
        create_file=lambda key, b, fn, ct: {"id": "file_1", "name": fn, "size": len(b)},
        create_knowledge_base=lambda key, cfg: {"id": "kb_1", "echo": cfg},
    )
    cases = [
        ("tools", {"name": "lookup", "type": "function"}),
        ("squads", {"name": "qual"}),
        ("campaigns", {"name": "spring"}),
        ("knowledge-bases", {"provider": "custom-knowledge-base"}),
    ]
    for resource, payload in cases:
        resp = client.post(
            f"/api/brands/{brand_id}/vapi/{resource}",
            json=payload,
        )
        if resp.status_code != 201:
            failures.append(f"{resource} create status={resp.status_code} body={resp.text}")
            continue
        body = resp.json()
        if body.get("echo") != payload:
            failures.append(f"{resource} forwarded body mismatch: got {body!r}")
        else:
            print(f"[ok] {resource} passthrough forwards body to vapi_client")

    # files: multipart upload
    resp = client.post(
        f"/api/brands/{brand_id}/vapi/files",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    if resp.status_code != 201:
        failures.append(f"files create status={resp.status_code} body={resp.text}")
    else:
        body = resp.json()
        if body.get("name") != "hello.txt" or body.get("size") != 11:
            failures.append(f"files forwarded body mismatch: {body!r}")
        else:
            print("[ok] files multipart passthrough forwards file to vapi_client")


def t_analytics_query(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    tracker = VapiCallTracker()
    _patch_vapi(
        tracker,
        query_analytics=lambda key, cfg: {"rows": [], "echo": cfg},
    )
    payload = {
        "queries": [
            {
                "table": "call",
                "name": "Total Duration",
                "operations": [{"operation": "sum", "column": "duration"}],
            }
        ]
    }
    resp = client.post(
        f"/api/brands/{brand_id}/vapi/analytics/query",
        json=payload,
    )
    if resp.status_code != 200:
        failures.append(f"analytics_query status={resp.status_code} body={resp.text}")
        return
    body = resp.json()
    if body.get("echo") != payload:
        failures.append(f"analytics_query forwarded body mismatch: {body!r}")
        return
    if tracker.count("query_analytics") != 1:
        failures.append(
            f"analytics_query expected 1 call, got {tracker.count('query_analytics')}"
        )
        return
    print("[ok] analytics query passthrough forwards body to vapi_client")


def t_insight_create_and_run(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    tracker = VapiCallTracker()
    _patch_vapi(
        tracker,
        create_insight=lambda key, cfg: {"id": "ins_1", "echo": cfg},
        run_insight=lambda key, ins_id, cfg=None: {
            "results": [],
            "ran": True,
            "insight_id": ins_id,
            "config": cfg,
        },
    )
    create_payload = {
        "type": "bar",
        "name": "Calls per assistant",
        "queries": [
            {
                "type": "vapiql-json",
                "table": "call",
                "column": "id",
                "operation": "count",
                "name": "calls",
            }
        ],
        "groupBy": "assistantId",
    }
    r1 = client.post(
        f"/api/brands/{brand_id}/vapi/insights",
        json=create_payload,
    )
    if r1.status_code != 201:
        failures.append(f"insight_create status={r1.status_code} body={r1.text}")
        return
    body = r1.json()
    if body.get("id") != "ins_1" or body.get("echo") != create_payload:
        failures.append(f"insight_create echo mismatch: {body!r}")
        return
    print("[ok] insight create passthrough forwards body to vapi_client")

    r2 = client.post(
        f"/api/brands/{brand_id}/vapi/insights/ins_1/run",
        json={"formatPlan": {"format": "raw"}},
    )
    if r2.status_code != 200:
        failures.append(f"insight_run status={r2.status_code} body={r2.text}")
        return
    body2 = r2.json()
    if not body2.get("ran") or body2.get("insight_id") != "ins_1":
        failures.append(f"insight_run body={body2!r}")
        return
    if tracker.count("run_insight") != 1:
        failures.append(
            f"insight_run expected 1 call, got {tracker.count('run_insight')}"
        )
        return
    print("[ok] insight {id}/run forwards optional body to vapi_client")


def t_insight_preview(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    tracker = VapiCallTracker()
    _patch_vapi(
        tracker,
        preview_insight=lambda key, cfg: {"preview": True, "echo": cfg},
    )
    payload = {
        "type": "text",
        "queries": [
            {
                "type": "vapiql-json",
                "table": "call",
                "column": "id",
                "operation": "count",
                "name": "n",
            }
        ],
    }
    resp = client.post(
        f"/api/brands/{brand_id}/vapi/insights/preview",
        json=payload,
    )
    if resp.status_code != 200:
        failures.append(f"insight_preview status={resp.status_code} body={resp.text}")
        return
    body = resp.json()
    if body.get("echo") != payload or not body.get("preview"):
        failures.append(f"insight_preview body={body!r}")
        return
    print("[ok] insight preview passthrough forwards body to vapi_client")


def t_assistants_vapi_extension(client: TestClient, failures: list[str]) -> None:
    FAKE_DB.reset()
    brand_id = _make_brand_id()
    a_synced = _seed_assistant(brand_id, vapi_assistant_id="asst_remote")
    a_unsynced = _seed_assistant(brand_id, vapi_assistant_id=None)
    tracker = VapiCallTracker()
    _patch_vapi(
        tracker,
        get_assistant=lambda key, vid: {"id": vid, "name": "remote"},
    )
    r_ok = client.get(
        f"/api/brands/{brand_id}/voice-ai/assistants/{a_synced}/vapi",
    )
    if r_ok.status_code != 200:
        failures.append(f"vapi_view ok status={r_ok.status_code} body={r_ok.text}")
    elif r_ok.json().get("id") != "asst_remote":
        failures.append(f"vapi_view payload={r_ok.json()!r}")
    else:
        print("[ok] /assistants/{id}/vapi returns Vapi view for synced assistant")

    r_404 = client.get(
        f"/api/brands/{brand_id}/voice-ai/assistants/{a_unsynced}/vapi",
    )
    if r_404.status_code != 404:
        failures.append(f"vapi_view unsynced status={r_404.status_code} body={r_404.text}")
    else:
        print("[ok] /assistants/{id}/vapi returns 404 when assistant not synced")


# ============================================================================
# Entrypoint
# ============================================================================


def main() -> int:
    _install_fake_db()
    brands_svc.get_twilio_creds = _fake_get_twilio_creds  # type: ignore[assignment]
    vapi_phone_numbers_router.brands_svc.get_twilio_creds = _fake_get_twilio_creds  # type: ignore[assignment]

    # Lifespan tries to initialize a real psycopg pool — neuter it.
    async def _noop() -> None:
        return None

    db_module.init_pool = _noop  # type: ignore[assignment]
    db_module.close_pool = _noop  # type: ignore[assignment]
    import app.main as app_main_module
    app_main_module.init_pool = _noop  # type: ignore[assignment]
    app_main_module.close_pool = _noop  # type: ignore[assignment]

    failures: list[str] = []
    with TestClient(app) as client:
        t_phone_number_import_happy(client, failures)
        t_phone_number_import_already_imported(client, failures)
        t_phone_number_import_no_creds(client, failures)
        t_phone_number_bind(client, failures)
        t_vapi_outbound_happy(client, failures)
        t_vapi_outbound_missing_idempotency_key(client, failures)
        t_vapi_outbound_idempotency_replay(client, failures)
        t_vapi_outbound_provider_error(client, failures)
        t_passthrough_resources(client, failures)
        t_analytics_query(client, failures)
        t_insight_create_and_run(client, failures)
        t_insight_preview(client, failures)
        t_assistants_vapi_extension(client, failures)

    # Sanity: confirm vapi_key resolves from settings.VAPI_API_KEY
    try:
        _vapi_key_resolver()
    except Exception as exc:  # noqa: BLE001
        failures.append(f"vapi_key resolver raised: {exc!r}")

    if failures:
        print("\nFAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
