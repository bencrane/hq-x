import httpx

from app.config import settings
from app.main import app


async def _post(headers: dict[str, str] | None = None, body: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/internal/scheduler/tick",
            json=body or {},
            headers=headers or {},
        )


async def test_trigger_tick_requires_secret() -> None:
    resp = await _post()
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_trigger_secret"


async def test_trigger_tick_rejects_wrong_secret() -> None:
    resp = await _post({"Authorization": "Bearer wrong-secret"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "invalid_trigger_secret"


async def test_trigger_tick_accepts_valid_secret() -> None:
    resp = await _post(
        {"Authorization": "Bearer test-trigger-secret"},
        body={"trigger_run_id": "run_abc"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "received_at" in body


async def test_trigger_tick_returns_503_when_secret_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TRIGGER_SHARED_SECRET", None)
    resp = await _post({"Authorization": "Bearer test-trigger-secret"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "trigger_secret_not_configured"
