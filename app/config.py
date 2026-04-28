from typing import Literal

from pydantic import HttpUrl, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
