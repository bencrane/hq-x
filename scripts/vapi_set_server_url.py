"""Idempotent: ensure every Vapi assistant + phone number has the right
server.url + server.secret pointing at hq-x.

Vapi resolves the server URL in this order:
    call.server > assistant.server > phoneNumber.server > org.server

`/org` is dashboard-only — not exposed via the standard private API key —
so this script applies the config to every assistant and phone number
the operator owns. Safe to re-run; only PATCHes resources whose current
config doesn't match the expected URL.

Usage:
    doppler run --project hq-x --config <env> -- uv run python -m scripts.vapi_set_server_url

Required env (Doppler):
    VAPI_API_KEY_PRIVATE   — private API key (server-side; works on /assistant)
    VAPI_WEBHOOK_SECRET    — the secret hq-x compares X-Vapi-Secret against
    HQX_API_BASE_URL       — public base URL of the deployed hq-x app

The expected webhook URL is `${HQX_API_BASE_URL}/api/v1/vapi/webhook`.

Note: Vapi never echoes server.secret back in GET responses (just an
`isServerUrlSecretSet` boolean). To detect drift in the secret value
itself, this script always PATCHes the secret unless explicitly told
to skip via --skip-secret-update — use that flag for routine reconciles
where you only care about the URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


VAPI_BASE = "https://api.vapi.ai"


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"FATAL: {name} not set in environment", file=sys.stderr)
        sys.exit(2)
    return value


def _request(
    method: str,
    path: str,
    *,
    api_key: str,
    body: dict | None = None,
) -> tuple[int, dict | list | None]:
    url = f"{VAPI_BASE}{path}"
    # Vapi's CDN rejects requests with the default Python urllib User-Agent
    # ("Python-urllib/3.x") with a 403; override with a generic UA.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "hq-x/vapi-set-server-url",
        "Accept": "application/json",
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            parsed = json.loads(raw) if raw else None
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return exc.code, parsed


def reconcile_assistants(
    *,
    api_key: str,
    expected_url: str,
    secret: str,
    skip_secret_update: bool,
) -> dict[str, int]:
    status, body = _request("GET", "/assistant?limit=200", api_key=api_key)
    if status != 200 or not isinstance(body, list):
        print(f"FATAL: GET /assistant returned {status}: {body}", file=sys.stderr)
        sys.exit(3)

    counts = {"checked": 0, "patched": 0, "skipped": 0, "failed": 0}
    for assistant in body:
        counts["checked"] += 1
        aid = assistant["id"]
        name = assistant.get("name", "(unnamed)")
        current = assistant.get("server") or {}
        current_url = current.get("url")
        secret_set = bool(assistant.get("isServerUrlSecretSet"))

        url_ok = current_url == expected_url
        secret_ok = secret_set if skip_secret_update else False
        if url_ok and secret_ok:
            print(f"  [skip] assistant {name} ({aid}) — already configured")
            counts["skipped"] += 1
            continue

        patch_body: dict[str, dict[str, str]] = {"server": {"url": expected_url}}
        if not skip_secret_update:
            patch_body["server"]["secret"] = secret

        s, b = _request("PATCH", f"/assistant/{aid}", api_key=api_key, body=patch_body)
        if s == 200:
            print(f"  [ok]   assistant {name} ({aid}) — patched")
            counts["patched"] += 1
        else:
            print(f"  [FAIL] assistant {name} ({aid}) — HTTP {s}: {b}")
            counts["failed"] += 1
    return counts


def reconcile_phone_numbers(
    *,
    api_key: str,
    expected_url: str,
    secret: str,
    skip_secret_update: bool,
) -> dict[str, int]:
    status, body = _request("GET", "/phone-number?limit=200", api_key=api_key)
    if status != 200 or not isinstance(body, list):
        # Empty 200 / 404 / no phone numbers — not fatal.
        if status == 200:
            return {"checked": 0, "patched": 0, "skipped": 0, "failed": 0}
        print(f"WARN: GET /phone-number returned {status}: {body}", file=sys.stderr)
        return {"checked": 0, "patched": 0, "skipped": 0, "failed": 0}

    counts = {"checked": 0, "patched": 0, "skipped": 0, "failed": 0}
    for pn in body:
        counts["checked"] += 1
        pid = pn["id"]
        number = pn.get("number") or pn.get("e164") or "(no number)"
        current = pn.get("server") or {}
        current_url = current.get("url")
        secret_set = bool(pn.get("isServerUrlSecretSet"))

        url_ok = current_url == expected_url
        secret_ok = secret_set if skip_secret_update else False
        if url_ok and secret_ok:
            print(f"  [skip] phone {number} ({pid}) — already configured")
            counts["skipped"] += 1
            continue

        patch_body: dict[str, dict[str, str]] = {"server": {"url": expected_url}}
        if not skip_secret_update:
            patch_body["server"]["secret"] = secret

        s, b = _request("PATCH", f"/phone-number/{pid}", api_key=api_key, body=patch_body)
        if s == 200:
            print(f"  [ok]   phone {number} ({pid}) — patched")
            counts["patched"] += 1
        else:
            print(f"  [FAIL] phone {number} ({pid}) — HTTP {s}: {b}")
            counts["failed"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--skip-secret-update",
        action="store_true",
        help=(
            "Only PATCH server.url, leave server.secret as-is. Use for routine "
            "reconciles where you only care about URL drift."
        ),
    )
    args = parser.parse_args()

    api_key = _env("VAPI_API_KEY_PRIVATE")
    secret = _env("VAPI_WEBHOOK_SECRET")
    api_base = _env("HQX_API_BASE_URL").rstrip("/")
    expected_url = f"{api_base}/api/v1/vapi/webhook"

    print(f"expected server.url: {expected_url}")
    print(f"server.secret update: {'skipped' if args.skip_secret_update else 'forced'}")
    print()

    print("Reconciling assistants...")
    a = reconcile_assistants(
        api_key=api_key,
        expected_url=expected_url,
        secret=secret,
        skip_secret_update=args.skip_secret_update,
    )
    print(
        f"  assistants: checked={a['checked']} patched={a['patched']} "
        f"skipped={a['skipped']} failed={a['failed']}"
    )
    print()

    print("Reconciling phone numbers...")
    p = reconcile_phone_numbers(
        api_key=api_key,
        expected_url=expected_url,
        secret=secret,
        skip_secret_update=args.skip_secret_update,
    )
    print(
        f"  phone numbers: checked={p['checked']} patched={p['patched']} "
        f"skipped={p['skipped']} failed={p['failed']}"
    )

    if a["failed"] or p["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
