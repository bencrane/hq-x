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

    # ---------------------------------------------------------------- Lob
    # Single global API key (no per-org credentials). The webhook secret is
    # also global — Lob signs centrally so per-org overrides have no value.
    LOB_API_KEY: str | None = None
    # Optional test-mode key. When a request opts in via `test_mode=true`,
    # the route uses this key instead of LOB_API_KEY. Lets prd mint zero-cost
    # test pieces / address verifies without burning credits.
    LOB_API_KEY_TEST: str | None = None
    LOB_WEBHOOK_SECRET: str | None = None
    LOB_WEBHOOK_SIGNATURE_MODE: str = "permissive_audit"
    LOB_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS: int = 300
    LOB_WEBHOOK_SCHEMA_VERSIONS: str = "v1"

    # SLO thresholds (rates expressed as 0.01 = 1%). Negative disables.
    LOB_SLO_SIGNATURE_REJECT_RATE_THRESHOLD: float = 0.01
    LOB_SLO_DEAD_LETTER_RATE_THRESHOLD: float = 0.01
    LOB_SLO_REPLAY_FAILURE_RATE_THRESHOLD: float = 0.05
    LOB_SLO_PROJECTION_FAILURE_RATE_THRESHOLD: float = 0.01
    LOB_SLO_DUPLICATE_IGNORE_RATE_THRESHOLD: float = 0.2


settings = Settings()


def assert_production_safe(s: Settings = settings) -> None:
    """Refuse to boot in production with insecure webhook signature modes.

    Mirrors outbound-engine-x's `_INSECURE_WEBHOOK_MODES` startup guard. If
    APP_ENV=prd and LOB_WEBHOOK_SIGNATURE_MODE is not `enforce`, the app will
    not start.
    """
    if s.APP_ENV != "prd":
        return
    mode = (s.LOB_WEBHOOK_SIGNATURE_MODE or "").strip().lower()
    if mode in _INSECURE_LOB_WEBHOOK_MODES:
        raise RuntimeError(
            f"LOB_WEBHOOK_SIGNATURE_MODE={mode!r} is insecure when APP_ENV=prd; "
            "set LOB_WEBHOOK_SIGNATURE_MODE=enforce and provide LOB_WEBHOOK_SECRET"
        )
