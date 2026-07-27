"""Application configuration — the only place that reads from the environment.

Everything else imports `settings` from here instead of touching os.environ.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str
    REDIS_URL: str

    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str
    TWILIO_REGION: str = "ie1"
    PUBLIC_BASE_URL: str

    AI_SERVICE_URL: str
    AI_TIMEOUT_SECONDS: float = 8.0

    SENTRY_DSN: str = ""


settings = Settings()  # type: ignore[call-arg]  # fields are supplied via env/.env at runtime
