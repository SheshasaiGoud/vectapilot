"""Application settings, read from `VECTAPILOT_*` environment variables or a local `.env`."""

from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration.

    Required values have no default, so a missing one stops the app at startup with a clear
    validation error (fail fast) instead of failing later on the first request.
    """

    model_config = SettingsConfigDict(env_prefix="VECTAPILOT_", env_file=".env", extra="ignore")

    environment: Literal["local", "test", "ci", "production"] = "local"
    database_url: PostgresDsn
    redis_url: RedisDsn
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
