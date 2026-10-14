"""Application settings, read from `VECTAPILOT_*` environment variables or a local `.env`."""

from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, Secret
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration.

    Required values have no default, so a missing one stops the app at startup with a clear
    validation error (fail fast) instead of failing later on the first request.
    """

    model_config = SettingsConfigDict(
        env_prefix="VECTAPILOT_",
        env_file=".env",
        extra="ignore",
        # Hides the bad value from str()/repr() of a ValidationError (a DSN may hold a password).
        # `.errors()` and `.json()` still carry it: log `errors(include_input=False)` only.
        # It does not hide text a validator writes into its own message: validator messages name
        # fields and rules, never values.
        hide_input_in_errors=True,
    )

    environment: Literal["local", "test", "ci", "production"] = "local"
    # A DSN can hold a password. Secret[...] still validates the URL but prints as '**********'
    # (repr, str, JSON, log extras); call `.get_secret_value()` only where a client is built.
    database_url: Secret[PostgresDsn]
    redis_url: Secret[RedisDsn]
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
