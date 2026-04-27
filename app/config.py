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

    APP_ENV: Literal["dev", "stg", "prd"]
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


settings = Settings()
