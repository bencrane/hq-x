from typing import Literal

from pydantic import HttpUrl, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_LOB_WEBHOOK_MODES = {"permissive_audit", "disabled"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=True)

    HQX_DB_URL_POOLED: PostgresDsn
    HQX_DB_URL_DIRECT: PostgresDsn
    HQX_SUPABASE_URL: HttpUrl
    HQX_SUPABASE_SERVICE_ROLE_KEY: SecretStr
    HQX_SUPABASE_PUBLISHABLE_KEY: SecretStr
    HQX_SUPABASE_PROJECT_REF: str

    # APP_ENV is set inside each Doppler config (dev/stg/prd) and injected
    # at runtime by `doppler run`. The Doppler service token is scoped to a
    # single config, so Railway only needs DOPPLER_TOKEN — APP_ENV comes
    # along automatically with the right value for that environment.
    APP_ENV: Literal["dev", "stg", "prd"]
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    CAL_WEBHOOK_SECRET: str = ""
    EMAILBISON_WEBHOOK_PATH_TOKEN: str | None = None
    EMAILBISON_WEBHOOK_ALLOWED_ORIGINS: str = "app.emailbison.com,emailbison.com"
    EMAILBISON_API_BASE: str = "https://app.outboundsolutions.com"
    EMAILBISON_API_KEY: str | None = None
    EMAILBISON_DEFAULT_FROM_NAME: str | None = None

    TRIGGER_SHARED_SECRET: str | None = None

    # ── Voice infrastructure (Twilio + Vapi + ClickHouse) ───────────────────
    # Public-facing API base URL — used to construct Twilio status-callback
    # URLs and Vapi inbound webhook URLs.
    HQX_API_BASE_URL: str = "http://localhost:8000"

    # Master key for encrypting brands.twilio_account_sid_enc /
    # twilio_auth_token_enc via pgcrypto pgp_sym_encrypt.
    BRAND_CREDS_ENCRYPTION_KEY: SecretStr | None = None

    # Vapi — global account (single operator). VAPI_API_KEY is used by
    # outbound REST calls; VAPI_WEBHOOK_SECRET signs inbound webhooks.
    VAPI_API_KEY: SecretStr | None = None
    VAPI_WEBHOOK_SECRET: SecretStr | None = None
    # Default `strict` (drift fix §7.8 — was permissive_audit in OEX code).
    VAPI_WEBHOOK_SIGNATURE_MODE: Literal["strict", "permissive_audit", "disabled"] = "strict"

    # Twilio webhook signature mode. `enforce` rejects on mismatch.
    TWILIO_WEBHOOK_SIGNATURE_MODE: Literal["enforce", "permissive_audit", "disabled"] = "enforce"

    # ClickHouse (analytics dual-write). All optional — analytics is
    # fire-and-forget and skips when unconfigured.
    CLICKHOUSE_URL: str | None = None
    CLICKHOUSE_USER: str | None = None
    CLICKHOUSE_PASSWORD: SecretStr | None = None
    CLICKHOUSE_DATABASE: str = "default"

    # data-engine-x base URL — for Vapi `lookup_carrier` tool (drift fix §7.4).
    DEX_BASE_URL: str | None = None

    # ── Lob (direct mail) ───────────────────────────────────────────────────
    # Single global API key (no per-org credentials).
    LOB_API_KEY: str | None = None
    # Optional test-mode key. When a request opts in via `test_mode=true`,
    # the route uses this key instead of LOB_API_KEY. Lets prd mint zero-cost
    # test pieces / address verifies without burning credits.
    LOB_API_KEY_TEST: str | None = None
    # Lob runs separate webhook subscriptions for live vs test mode, each
    # with its own signing secret. Pieces created with LOB_API_KEY trigger
    # webhooks signed with LOB_WEBHOOKS_SECRET_LIVE; pieces created with
    # LOB_API_KEY_TEST get signed with LOB_WEBHOOKS_SECRET_TEST. The
    # receiver tries both — whichever verifies is the environment we
    # record on the event.
    LOB_WEBHOOKS_SECRET_LIVE: str | None = None
    LOB_WEBHOOKS_SECRET_TEST: str | None = None
    LOB_WEBHOOK_SIGNATURE_MODE: str = "permissive_audit"
    LOB_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS: int = 300
    LOB_WEBHOOK_SCHEMA_VERSIONS: str = "v1"

    # SLO thresholds (rates expressed as 0.01 = 1%). Negative disables.
    LOB_SLO_SIGNATURE_REJECT_RATE_THRESHOLD: float = 0.01
    LOB_SLO_DEAD_LETTER_RATE_THRESHOLD: float = 0.01
    LOB_SLO_REPLAY_FAILURE_RATE_THRESHOLD: float = 0.05
    LOB_SLO_PROJECTION_FAILURE_RATE_THRESHOLD: float = 0.01
    LOB_SLO_DUPLICATE_IGNORE_RATE_THRESHOLD: float = 0.2

    # ── DMaaS MCP server ────────────────────────────────────────────────────
    # Bearer token managed-agent clients present in the Authorization header
    # to call /mcp/dmaas/*. When None in dev/stg, the MCP mount accepts any
    # request (convenient for local testing). Production refuses to boot
    # without it (see `assert_production_safe`).
    DMAAS_MCP_BEARER_TOKEN: SecretStr | None = None

    # ── Dub.co link attribution ─────────────────────────────────────────────
    # API key from app.dub.co/settings/tokens. Required in prd.
    DUB_API_KEY: SecretStr | None = None
    # Default short-link domain (e.g. "dub.sh" or our custom "go.hq-x.com").
    # When None, dub uses the workspace's primary domain.
    DUB_DEFAULT_DOMAIN: str | None = None
    # Default tenantId stamped on every link create — lets us segment
    # attribution by environment / brand later. None = no stamping.
    DUB_DEFAULT_TENANT_ID: str | None = None
    # HMAC secret for verifying inbound Dub webhook calls (Dub-Signature
    # header = hex HMAC-SHA256 over the raw body). Required in prd whenever
    # DUB_WEBHOOK_SIGNATURE_MODE != 'disabled'.
    DUB_WEBHOOK_SECRET: SecretStr | None = None
    # Signature enforcement mode. Mirrors LOB_WEBHOOK_SIGNATURE_MODE:
    #   enforce          - reject anything that doesn't verify
    #   permissive_audit - audit + log failures but accept the event
    #   disabled         - do not verify (insecure; refused at boot in prd)
    DUB_WEBHOOK_SIGNATURE_MODE: Literal["enforce", "permissive_audit", "disabled"] = (
        "permissive_audit"
    )
    # Override base URL for tests / self-hosted dub. Defaults to api.dub.co.
    DUB_API_BASE_URL: str | None = None

    # ── Entri (custom domains for DMaaS landing pages) ─────────────────────
    # Single partner account. `applicationId` is public-shippable;
    # `secret` is server-only and used to mint short-lived (60-min) JWTs.
    # The webhook secret is separate and used to verify HMAC-SHA256 V2
    # signatures on inbound events.
    #
    # When ENTRI_APPLICATION_ID is unset, all /api/v1/entri/* endpoints
    # return 503 entri_not_configured — the integration is fully built
    # but inert until we sign up for a paid Entri plan.
    ENTRI_APPLICATION_ID: str | None = None
    ENTRI_SECRET: SecretStr | None = None
    ENTRI_WEBHOOK_SECRET: SecretStr | None = None
    # Hostname we own (e.g. "domains.dmaas.ourcompany.com") which has been
    # CNAMEd to power.goentri.com and registered as cname_target in the
    # Entri dashboard. Customer subdomains CNAME here.
    ENTRI_CNAME_TARGET: str | None = None
    # Origin URL that Entri Power proxies traffic to. Per-campaign paths
    # are appended at session-creation time (e.g. <base>/lp/<step_id>).
    ENTRI_APPLICATION_URL_BASE: str | None = None
    # API base. Override for tests.
    ENTRI_API_BASE: str = "https://api.goentri.com"
    # Webhook signature mode — same semantics as LOB/DUB.
    ENTRI_WEBHOOK_SIGNATURE_MODE: Literal["enforce", "permissive_audit", "disabled"] = (
        "permissive_audit"
    )
    # Replay-window for V2 signature timestamp check (seconds).
    ENTRI_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS: int = 300


settings = Settings()


def _strict_signature_modes() -> None:
    """Production safety check — refuse to boot with relaxed signature modes."""
    if settings.APP_ENV != "prd":
        return
    if settings.VAPI_WEBHOOK_SIGNATURE_MODE != "strict":
        raise RuntimeError(
            "VAPI_WEBHOOK_SIGNATURE_MODE must be 'strict' in production "
            f"(got '{settings.VAPI_WEBHOOK_SIGNATURE_MODE}')"
        )
    if settings.TWILIO_WEBHOOK_SIGNATURE_MODE != "enforce":
        raise RuntimeError(
            "TWILIO_WEBHOOK_SIGNATURE_MODE must be 'enforce' in production "
            f"(got '{settings.TWILIO_WEBHOOK_SIGNATURE_MODE}')"
        )


_strict_signature_modes()


def assert_production_safe(s: Settings = settings) -> None:
    """Refuse to boot in production with insecure Lob webhook signature modes.

    If APP_ENV=prd and LOB_WEBHOOK_SIGNATURE_MODE is not `enforce`, the app
    will not start. The live webhook secret is also required in prd.
    """
    if s.APP_ENV != "prd":
        return
    mode = (s.LOB_WEBHOOK_SIGNATURE_MODE or "").strip().lower()
    if mode in _INSECURE_LOB_WEBHOOK_MODES:
        raise RuntimeError(
            f"LOB_WEBHOOK_SIGNATURE_MODE={mode!r} is insecure when APP_ENV=prd; "
            "set LOB_WEBHOOK_SIGNATURE_MODE=enforce and provide LOB_WEBHOOKS_SECRET_LIVE"
        )
    if not s.LOB_WEBHOOKS_SECRET_LIVE:
        raise RuntimeError("LOB_WEBHOOKS_SECRET_LIVE must be set when APP_ENV=prd")
    if not s.DMAAS_MCP_BEARER_TOKEN:
        raise RuntimeError(
            "DMAAS_MCP_BEARER_TOKEN must be set when APP_ENV=prd; "
            "the /mcp/dmaas server refuses to start unauthenticated in production"
        )
    if not s.DUB_API_KEY:
        raise RuntimeError("DUB_API_KEY must be set when APP_ENV=prd")
    dub_mode = (s.DUB_WEBHOOK_SIGNATURE_MODE or "").strip().lower()
    if dub_mode in _INSECURE_LOB_WEBHOOK_MODES:
        raise RuntimeError(
            f"DUB_WEBHOOK_SIGNATURE_MODE={dub_mode!r} is insecure when APP_ENV=prd; "
            "set DUB_WEBHOOK_SIGNATURE_MODE=enforce and provide DUB_WEBHOOK_SECRET"
        )
    if not s.DUB_WEBHOOK_SECRET:
        raise RuntimeError("DUB_WEBHOOK_SECRET must be set when APP_ENV=prd")
    # Entri is opt-in: if ENTRI_APPLICATION_ID is set we're committing to
    # the integration in this env, so demand a verifying webhook config.
    # If unset, the entri router self-disables via 503 and that's fine.
    if s.ENTRI_APPLICATION_ID:
        if not s.ENTRI_SECRET:
            raise RuntimeError(
                "ENTRI_SECRET must be set when ENTRI_APPLICATION_ID is set in prd"
            )
        entri_mode = (s.ENTRI_WEBHOOK_SIGNATURE_MODE or "").strip().lower()
        if entri_mode in _INSECURE_LOB_WEBHOOK_MODES:
            raise RuntimeError(
                f"ENTRI_WEBHOOK_SIGNATURE_MODE={entri_mode!r} is insecure when APP_ENV=prd; "
                "set ENTRI_WEBHOOK_SIGNATURE_MODE=enforce and provide ENTRI_WEBHOOK_SECRET"
            )
        if not s.ENTRI_WEBHOOK_SECRET:
            raise RuntimeError("ENTRI_WEBHOOK_SECRET must be set when APP_ENV=prd")
