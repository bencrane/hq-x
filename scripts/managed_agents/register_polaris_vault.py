"""Register the Polaris MCP bearer credential in an Anthropic Managed Agents vault.

The Anthropic SDK splits this into two calls:

  1. ``client.beta.vaults.create(display_name=...)`` — creates an empty
     container. Vaults can hold multiple credentials (e.g. for credential
     rotation overlap or per-MCP-server scoping).
  2. ``client.beta.vaults.credentials.create(vault_id, auth={...})`` —
     adds a credential whose ``mcp_server_url`` is the SCOPE. Anthropic
     uses the URL to pick which credential to inject when the agent calls
     a given MCP server.

The operator-spec'd one-call ``vaults.create(name=..., values=...)`` shape
is not how the SDK works — that was a doc-level guess. The two-call shape
is honored here.

Usage::

    POLARIS_MCP_AUTH_TOKEN=<token> \\
    POLARIS_MCP_URL=https://polaris-mcp.opsengine.run/mcp \\
        doppler run --project hq-all --config prd -- \\
        uv run python -m scripts.managed_agents.register_polaris_vault

Sequence:

  1. Generate a strong bearer (``openssl rand -base64 48``).
  2. ``doppler secrets set --project hq-all --config prd POLARIS_MCP_AUTH_TOKEN=<token>``
  3. Deploy polaris-mcp Railway service (Dockerfile.polaris-mcp). It will
     boot only if POLARIS_MCP_AUTH_TOKEN is injected.
  4. ``doppler secrets set --project hq-all --config prd POLARIS_MCP_URL=<url>``
  5. Run this script — creates vault (or reuses) + creates credential (or reuses).
  6. ``doppler secrets set --project hq-all --config prd MANAGED_VAULT_ID_POLARIS=<vault_id>``
  7. ``doppler run … -- uv run python -m scripts.managed_agents.bump_agent``
     mints agent v5 with the polaris MCP toolset wired.

Idempotent at both layers:
  * Vault: matched by ``display_name=polaris_auth`` (errors on duplicates).
  * Credential: matched by ``mcp_server_url=POLARIS_MCP_URL`` inside the
    vault (errors on duplicates for the same URL). To rotate the bearer,
    delete the credential and re-run — Anthropic stores tokens write-only.
"""
from __future__ import annotations

import argparse
import os
import sys

from anthropic import Anthropic

VAULT_DISPLAY_NAME = "polaris_auth"
CREDENTIAL_DISPLAY_NAME = "polaris_static_bearer"


def _api_key() -> str:
    key = os.environ.get("ANTHROPIC_MANAGED_AGENTS_API_KEY")
    if not key:
        sys.exit(
            "ERROR: ANTHROPIC_MANAGED_AGENTS_API_KEY not set. "
            "Run inside `doppler run --project hq-all --config prd -- ...`"
        )
    return key


def _find_vault(client: Anthropic, display_name: str):
    vaults = list(client.beta.vaults.list(limit=100))
    matches = [v for v in vaults if getattr(v, "display_name", None) == display_name]
    if len(matches) > 1:
        sys.exit(
            f"ERROR: found {len(matches)} vaults with display_name {display_name!r}; "
            "delete the duplicates before re-running."
        )
    return matches[0] if matches else None


def _find_credential(client: Anthropic, vault_id: str, mcp_url: str):
    creds = list(client.beta.vaults.credentials.list(vault_id, limit=100))
    matches = []
    for c in creds:
        auth = getattr(c, "auth", None)
        cred_url = getattr(auth, "mcp_server_url", None) if auth is not None else None
        if cred_url == mcp_url:
            matches.append(c)
    if len(matches) > 1:
        sys.exit(
            f"ERROR: found {len(matches)} credentials for mcp_server_url={mcp_url!r} "
            f"inside vault {vault_id}; delete the duplicates."
        )
    return matches[0] if matches else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report current state and what would happen; no writes.",
    )
    p.add_argument(
        "--vault-only",
        action="store_true",
        help="Create / reuse the vault but skip credential creation "
             "(useful when POLARIS_MCP_URL isn't known yet because the "
             "Railway service hasn't been deployed).",
    )
    args = p.parse_args()

    client = Anthropic(api_key=_api_key())

    print(f"# Polaris vault registration (dry_run={args.dry_run}, vault_only={args.vault_only})")
    print()

    # ── Vault ─────────────────────────────────────────────────────────
    existing_vault = _find_vault(client, VAULT_DISPLAY_NAME)
    if args.dry_run:
        if existing_vault:
            print(f"vault: EXISTS id={existing_vault.id}    # [would reuse]")
        else:
            print(f"vault: MISSING                          # [would create]")
    else:
        if existing_vault:
            vault_id = existing_vault.id
            print(f"MANAGED_VAULT_ID_POLARIS={vault_id}    # [reused]")
        else:
            vault = client.beta.vaults.create(display_name=VAULT_DISPLAY_NAME)
            vault_id = vault.id
            print(f"MANAGED_VAULT_ID_POLARIS={vault_id}    # [created]")
    print()

    # ── Credential ────────────────────────────────────────────────────
    if args.vault_only:
        print("(skipping credential — re-run without --vault-only after "
              "polaris-mcp is deployed and POLARIS_MCP_URL is set in Doppler)")
        return 0

    url = os.environ.get("POLARIS_MCP_URL")
    token = os.environ.get("POLARIS_MCP_AUTH_TOKEN")
    if not url or not token:
        print(
            "credential: SKIPPED — set POLARIS_MCP_URL + POLARIS_MCP_AUTH_TOKEN "
            "in Doppler and re-run to attach the static_bearer credential.\n"
            "(The vault itself has been created and can hold credentials added later.)"
        )
        return 0

    if args.dry_run or existing_vault is None:
        # In dry-run or pre-vault-create, we can't check existing credentials
        # against a vault that doesn't exist yet.
        if args.dry_run:
            print(f"credential: WOULD CREATE for mcp_server_url={url!r}")
            return 0

    existing_cred = _find_credential(client, vault_id, url)
    if existing_cred:
        print(f"CREDENTIAL_ID={existing_cred.id}    # [reused, scoped to {url}]")
    else:
        cred = client.beta.vaults.credentials.create(
            vault_id=vault_id,
            display_name=CREDENTIAL_DISPLAY_NAME,
            auth={
                "type": "static_bearer",
                "token": token,
                "mcp_server_url": url,
            },
        )
        print(f"CREDENTIAL_ID={cred.id}    # [created, scoped to {url}]")

    print()
    print("Next steps:")
    print(f"  1. doppler secrets set --project hq-all --config prd \\")
    print(f"       MANAGED_VAULT_ID_POLARIS={vault_id}")
    print(f"  2. doppler run --project hq-all --config prd -- \\")
    print(f"       uv run python -m scripts.managed_agents.bump_agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
