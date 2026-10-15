"""Application settings, read from `VECTAPILOT_*` environment variables or a local `.env`."""

import logging
import os
from typing import Final, Literal, Self

from pydantic import Field, PostgresDsn, RedisDsn, Secret, SecretStr, model_validator
from pydantic_core import PydanticCustomError, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from vectapilot_api.gateway.types import RetryPolicy, Seconds

ANTHROPIC_SDK_ENV_VARS: Final = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_LOG",
    "ANTHROPIC_CUSTOM_HEADERS",
)
"""Variables the Anthropic SDK reads by itself (ADR 0006). With the anthropic provider, none may be
set: the key and the URL reach the client only from `Settings`, `ANTHROPIC_LOG` could turn on
transport logs (ADR 0007), and `ANTHROPIC_CUSTOM_HEADERS` is read even with an explicit key and
sent after it, so it could replace the key or turn on beta features (anthropic 1.12.0
`_client.py:685-692`, `_base_client.py:729-730`)."""

# With PYDANTIC_SETTINGS_DEBUG set, pydantic-settings logs every raw source value (the API key and
# the DSN passwords included) at DEBUG on this logger, before any validator runs. Pinned at WARNING
# when this module is imported, so no logging setup (ours, pytest's, uvicorn's) turns it on.
logging.getLogger("pydantic_settings").setLevel(logging.WARNING)


class Settings(BaseSettings):
    """Typed configuration.

    Required values have no default, so a missing one stops the app at startup with a clear
    validation error (fail fast) instead of failing later on the first request. The cross-field
    rules for the LLM gateway (ADR 0006, Settings) are checked in `_llm_rules`.
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

    # ── LLM gateway (ADR 0006) ──
    # The time limits, retries and backoff below are SAFETY BOUNDS, not latency targets: they stop
    # a hung or failing provider from holding a request (or a budget) forever.
    llm_provider: Literal["fake", "anthropic"] = "fake"
    # Upper limits (decided 2026-10-14) turn a typo such as 3000 into a startup error.
    llm_attempt_timeout_seconds: Seconds = Field(default=15.0, le=120)
    """Each attempt's limit; when it expires, the attempt is a `Transient` failure."""
    llm_deadline_seconds: Seconds = Field(default=30.0, le=300)
    """The whole call's limit: every attempt, backoff wait and repair included."""
    llm_max_retries: int = Field(default=2, ge=0, le=10)
    llm_backoff_base_seconds: Seconds = 0.5
    llm_backoff_cap_seconds: Seconds = Field(default=8.0, le=60)
    llm_max_repairs: int = Field(default=1, ge=0, le=1)
    """Structured-output repairs per call: 0 or 1 (ADR 0006: at most one)."""
    anthropic_api_key: SecretStr | None = None
    """`VECTAPILOT_ANTHROPIC_API_KEY`, from the process environment only (a `.env` one stops
    startup, see `settings_customise_sources`). Prints as '**********'."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Keep pydantic-settings' default sources and order; refuse the API key in a `.env` file.

        pydantic-settings builds one source per place a value can come from and calls this hook
        before reading any of them. Calling the `.env` source returns what the file holds, keyed
        by field name (the prefix and the letter case already handled), so the check needs no
        parsing of its own. It refuses the key even when the process environment also has one:
        a file in the repo tree is readable by every tool and agent (ADR 0007).

        Its limit: it sees only `VECTAPILOT_ANTHROPIC_API_KEY` in the one `.env` file read here.
        A key under another name (the SDK's `ANTHROPIC_API_KEY`), in another file, or loaded into
        the environment by a tool (`uv run --env-file`) is not detected by this check.
        """
        if "anthropic_api_key" in dotenv_settings():
            # A ValidationError like every other settings rule. Built from fixed text: the key
            # is never passed in, so no form of this error can carry it.
            raise ValidationError.from_exception_data(
                cls.__name__,
                [
                    {
                        "type": PydanticCustomError(
                            "key_in_env_file",
                            "VECTAPILOT_ANTHROPIC_API_KEY must come from the process environment"
                            " only, never a .env file: remove it from the file",
                        ),
                        "loc": ("anthropic_api_key",),
                        "input": None,
                    }
                ],
                hide_input=True,
            )
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _llm_rules(self) -> Self:
        """ADR 0006's fail-fast rules. Every message names fields and rules, never a value."""
        problems: list[str] = []
        if self.environment in ("test", "ci") and self.llm_provider != "fake":
            problems.append("llm_provider must be 'fake' when environment is 'test' or 'ci'")
        if self.environment == "production" and self.llm_provider == "fake":
            problems.append("llm_provider must not be 'fake' when environment is 'production'")
        if self.llm_provider == "anthropic":
            key = self.anthropic_api_key
            if key is None or not key.get_secret_value().strip():
                problems.append(
                    "anthropic_api_key (VECTAPILOT_ANTHROPIC_API_KEY) is required when"
                    " llm_provider is 'anthropic'"
                )
            if found := [name for name in ANTHROPIC_SDK_ENV_VARS if name in os.environ]:
                problems.append(
                    f"{', '.join(found)} must not be set when llm_provider is 'anthropic'"
                    " (the SDK would read them): unset them"
                )
        if self.llm_deadline_seconds < self.llm_attempt_timeout_seconds:
            problems.append("llm_deadline_seconds must be >= llm_attempt_timeout_seconds")
        if self.llm_backoff_cap_seconds < self.llm_backoff_base_seconds:
            problems.append("llm_backoff_cap_seconds must be >= llm_backoff_base_seconds")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def llm_retry_policy(self) -> RetryPolicy:
        """The retry settings as H2's `RetryPolicy` (valid by construction: see `_llm_rules`)."""
        return RetryPolicy(
            max_retries=self.llm_max_retries,
            base_delay_s=self.llm_backoff_base_seconds,
            max_delay_s=self.llm_backoff_cap_seconds,
        )
