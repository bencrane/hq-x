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
    # HMAC secret for verifying inbound Dub webhook calls. Required in prd
    # once the webhook receiver lands; this directive only stores it.
    DUB_WEBHOOK_SECRET: SecretStr | None = None
    # Override base URL for tests / self-hosted dub. Defaults to api.dub.co.
    DUB_API_BASE_URL: str | None = None


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
