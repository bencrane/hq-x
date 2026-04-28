"""Auth-shape tests for the internal voice-callback endpoints.

These don't hit Postgres — they verify only that ``require_flexible_auth``
gates the endpoints. Real DB-backed behavior is exercised by
``scripts/smoke_voice_callbacks.py`` against a live Doppler dev DB.
"""

import httpx
import pytest

from app.main import app


async def _post(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json={}, headers=headers or {})


@pytest.mark.parametrize(
    "path",
    [
        "/internal/voice/callback/send-reminders",
        "/internal/voice/callback/run-due-callbacks",
    ],
)
async def test_internal_callback_endpoints_require_auth(path: str) -> None:
    resp = await _post(path)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize(
    "path",
    [
        "/internal/voice/callback/send-reminders",
        "/internal/voice/callback/run-due-callbacks",
    ],
)
async def test_internal_callback_endpoints_reject_wrong_secret(path: str) -> None:
    resp = await _post(path, {"Authorization": "Bearer not-the-secret"})
    assert resp.status_code == 401, resp.text
