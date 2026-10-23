"""Application settings, read from `VECTAPILOT_*` environment variables or a local `.env`."""

import logging
import os
from typing import Final, Literal, Self

from pydantic import Field, PostgresDsn, RedisDsn, Secret, SecretStr, model_validator
from pydantic_core import PydanticCustomError, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from vectapilot_api.embeddings.catalog import EmbeddingsProvider
from vectapilot_api.gateway.types import OpaqueId, RetryPolicy, Seconds
from vectapilot_api.retrieval.types import MAX_K

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

TLS_ENV_VARS: Final = ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")
"""TLS variables the HTTP client reads (decided 2026-10-15, ADR 0007). With the anthropic provider
(and, ADR 0008, the voyage embeddings provider: the same httpx2 client), none may be set:
`SSL_CERT_FILE` and `SSL_CERT_DIR` replace the OS trust store (a proxy that holds a planted
certificate could read the key and the prompts; `httpx2/_config.py:35-38`), and only on that path
`SSLKEYLOGFILE` writes the TLS session keys to a file. Proxies (`HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, the macOS system proxy) are not refused: they keep
working."""

PROCESS_ENV_ONLY: Final = {
    "anthropic_api_key": "VECTAPILOT_ANTHROPIC_API_KEY",
    "voyage_api_key": "VECTAPILOT_VOYAGE_API_KEY",
}
"""API keys read from the process environment only: in the `.env` file they stop startup (ADR 0007,
ADR 0008). Field name → variable name."""

API_PAID_PROVIDERS: Final = ("llm_provider", "embeddings_provider")
"""Every setting that picks a provider the API server builds (ADR 0006 Exposure, D9 2026-10-23).
Any value but 'fake' counts as paid, so the rule fails closed: a new paid value is refused until
opted in. A later provider setting (a paid reranker) is added here; a test fails while a
`*_provider` setting is missing from this tuple."""

API_PAID_REFUSED: Final = (
    "api_allow_paid_llm (VECTAPILOT_API_ALLOW_PAID_LLM) must be true for the API server to use a"
    " paid {setting} (any but 'fake'): it has no budgets until M7"
)
"""The API server's refusal (decided 2026-10-22, extended by D9 2026-10-23), filled with a name
from `API_PAID_PROVIDERS`. Fixed text: it names settings and the rule, never a value."""

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
    api_allow_paid_llm: bool = False
    """`VECTAPILOT_API_ALLOW_PAID_LLM`: the API server's opt-in to every paid provider it builds,
    `llm_provider` (decided 2026-10-22, ADR 0006 Exposure) and `embeddings_provider` (D9,
    2026-10-23; the name stays). Until M7 adds budgets, every answer with evidence and every search
    is an uncapped paid call, so `create_app` refuses any provider in `API_PAID_PROVIDERS` but the
    fake while this is false (`require_api_paid_provider_opt_in`). The measurement CLI and the eval
    harness never read it: their `--live` runs have their own `--max-usd` reservation cap."""

    # ── Embeddings (ADR 0008) ──
    embeddings_provider: EmbeddingsProvider = "fake"
    embeddings_model: OpaqueId | None = None
    """A model id from `embeddings/models.toml`; None = that provider's `default_model` there."""
    embeddings_dimension: int = Field(default=1024, gt=0)
    """Must be one the model supports and the `chunks.embedding` column's (checked when built)."""
    embeddings_timeout_seconds: Seconds = Field(default=30.0, le=120)
    """Each embedding request's limit (a safety bound, not a latency target)."""
    voyage_api_key: SecretStr | None = None
    """`VECTAPILOT_VOYAGE_API_KEY`, from the process environment only, as the Anthropic key."""

    # ── Cited answers (M2 PR 6, `answer/`) ──
    # PLATFORM DEFAULTS until per-business configuration exists (rule 9: nothing here names a
    # business). PROVISIONAL and UNMEASURED: starting values chosen before any eval data, to be
    # tuned on eval v0's dev split (PR 7/8), never on the held-out test split. Not a result.
    rag_min_score: float = Field(default=0.3, ge=0.0, le=1.0, allow_inf_nan=False)
    """The abstention threshold: a hit is evidence only if its score (cosine similarity, H1) is at
    least this. With no evidence the model is not called and the answer is the weak-evidence
    outcome. 0.3 is an unmeasured starting value, not a calibrated one."""
    rag_context_k: int = Field(default=5, ge=1, le=MAX_K)
    """How many hits one answer retrieves (at most this many reach the prompt)."""
    rag_weak_evidence_outcome: Literal["dont_know", "handoff"] = "dont_know"
    """What weak or unusable evidence becomes. `dont_know` (the default) is an honest "I don't
    know"; `handoff` is business-context §3.5's proposed default, but nothing notifies staff until
    M8, so it is only a label for now."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Keep pydantic-settings' default sources and order; refuse an API key in a `.env` file.

        pydantic-settings builds one source per place a value can come from and calls this hook
        before reading any of them. Calling the `.env` source returns what the file holds, keyed
        by field name (the prefix and the letter case already handled), so the check needs no
        parsing of its own. It refuses the key even when the process environment also has one:
        a file in the repo tree is readable by every tool and agent (ADR 0007).

        Its limit: it sees only the `PROCESS_ENV_ONLY` names in the one `.env` file read here. A
        key under another name (the SDK's `ANTHROPIC_API_KEY`), in another file, or loaded into
        the environment by a tool (`uv run --env-file`) is not detected by this check.
        """
        in_file = dotenv_settings()
        if found := [name for name in PROCESS_ENV_ONLY if name in in_file]:
            # A ValidationError like every other settings rule. Built from fixed text: the key
            # is never passed in, so no form of this error can carry it.
            raise ValidationError.from_exception_data(
                cls.__name__,
                [
                    {
                        "type": PydanticCustomError(
                            "key_in_env_file",
                            f"{PROCESS_ENV_ONLY[name]} must come from the process environment"
                            " only, never a .env file: remove it from the file",
                        ),
                        "loc": (name,),
                        "input": None,
                    }
                    for name in found
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
            if found := [name for name in TLS_ENV_VARS if name in os.environ]:
                problems.append(
                    f"{', '.join(found)} must not be set when llm_provider is 'anthropic'"
                    " (they replace the OS trust store or write TLS session keys): unset them"
                )
        if self.llm_deadline_seconds < self.llm_attempt_timeout_seconds:
            problems.append("llm_deadline_seconds must be >= llm_attempt_timeout_seconds")
        if self.llm_backoff_cap_seconds < self.llm_backoff_base_seconds:
            problems.append("llm_backoff_cap_seconds must be >= llm_backoff_base_seconds")
        problems += self._embedding_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _embedding_problems(self) -> list[str]:
        """ADR 0008's fail-fast rules, as ADR 0006's for the LLM provider. Never a value."""
        problems: list[str] = []
        if self.environment in ("test", "ci") and self.embeddings_provider != "fake":
            problems.append("embeddings_provider must be 'fake' when environment is 'test' or 'ci'")
        if self.environment == "production" and self.embeddings_provider == "fake":
            problems.append(
                "embeddings_provider must not be 'fake' when environment is 'production'"
            )
        if self.embeddings_provider == "voyage":
            key = self.voyage_api_key
            if key is None or not key.get_secret_value().strip():
                problems.append(
                    "voyage_api_key (VECTAPILOT_VOYAGE_API_KEY) is required when"
                    " embeddings_provider is 'voyage'"
                )
            if found := [name for name in TLS_ENV_VARS if name in os.environ]:
                problems.append(
                    f"{', '.join(found)} must not be set when embeddings_provider is 'voyage'"
                    " (they replace the OS trust store or write TLS session keys): unset them"
                )
        return problems

    def require_api_paid_provider_opt_in(self) -> None:
        """The API server's startup rule (2026-10-22, D9 2026-10-23): a paid provider, any setting
        in `API_PAID_PROVIDERS`, needs `api_allow_paid_llm`.

        Not one of `_llm_rules`: the measurement CLI and the eval harness build the same Settings,
        and their `--live` runs must keep working under their own cap, so only `create_app` calls
        this. Every value but the fake counts as paid, so a provider added later is refused until
        opted in. Raised as a ValidationError like every other settings rule, one error per paid
        setting, from fixed text: only setting names from the tuple are passed in, never a value.
        """
        paid = [name for name in API_PAID_PROVIDERS if getattr(self, name) != "fake"]
        if not paid or self.api_allow_paid_llm:
            return
        raise ValidationError.from_exception_data(
            type(self).__name__,
            [
                {
                    "type": PydanticCustomError(
                        "paid_provider_not_opted_in", API_PAID_REFUSED, {"setting": name}
                    ),
                    "loc": ("api_allow_paid_llm",),
                    "input": None,
                }
                for name in paid
            ],
            hide_input=True,
        )

    def llm_retry_policy(self) -> RetryPolicy:
        """The retry settings as H2's `RetryPolicy` (valid by construction: see `_llm_rules`)."""
        return RetryPolicy(
            max_retries=self.llm_max_retries,
            base_delay_s=self.llm_backoff_base_seconds,
            max_delay_s=self.llm_backoff_cap_seconds,
        )


class MigrationSettings(BaseSettings):
    """What Alembic and `vectapilot-db-provision` read; the API never does (ADR 0009).

    The owner's URL is a separate variable from the API's, so the API process can be started
    without it: the API logs in as `vectapilot_app`, never as the owner.
    """

    model_config = SettingsConfigDict(
        env_prefix="VECTAPILOT_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    migration_database_url: Secret[PostgresDsn]
    """`VECTAPILOT_MIGRATION_DATABASE_URL`: the role that owns the schema and runs migrations."""
    database_url: Secret[PostgresDsn] | None = None
    """The API's URL (`vectapilot_app` and its password). Only `vectapilot-db-provision` needs it:
    it sets that password on the role, so the password has one source."""
