"""Application settings, read from `VECTAPILOT_*` environment variables or a local `.env`."""

import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import Field, PostgresDsn, RedisDsn, Secret, SecretStr, model_validator
from pydantic_core import PydanticCustomError, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from vectapilot_api.auth.origins import Origin, is_loopback_http
from vectapilot_api.embeddings.catalog import EmbeddingsProvider
from vectapilot_api.gateway.types import OpaqueId, RetryPolicy, Seconds
from vectapilot_api.retrieval.fusion import RRF_K
from vectapilot_api.retrieval.types import CANDIDATE_K, MAX_K, RetrievalMode

Environment = Literal["local", "test", "ci", "production"]
"""Where the process runs. `production`, and any environment added later, is refused by every
local-only path (`business.HEADER_ENVIRONMENTS`, `tools.sandbox.SANDBOX_ENVIRONMENTS`)."""

MIN_CANDIDATE_K: Final = 10
"""`rag_candidate_k`'s floor (M3 plan PR 4): PR 5's headroom count reads hit@10 on the fused list,
so it must hold at least 10 hits when that many chunks qualify."""

AGENT_ROUTER_TURN_BOUND_SECONDS: Final = 60.0
"""The most `/v1/agent`'s router and turn may take together [proposal, not a measurement;
decided 2026-11-11 that one exists]: `agent_router_timeout_seconds + agent_turn_timeout_seconds`
must not exceed it (checked at startup). Why 60 s: a hand-off is only useful if it arrives, and
60 s is the default idle or read timeout of common reverse proxies and load balancers (nginx's
`proxy_read_timeout`, AWS ALB's idle timeout); M10 picks the real proxy, revisit then.

**Not a bound on the whole request** [router-timeout review, zara]: `/v1/agent` has no outer
timeout. Outside this bound are storing a proposal (one database insert), the `agent_turn` record
and, under `agent_router = on`, a `rag` route's answer, which keeps `/v1/answer`'s own limits:
its search's embedding (`batch_deadline_s`: 3 x 30 s attempts + 2 x 8 s waits = 106 s with the
defaults) and `llm_deadline_seconds` (30 s). So a `rag` route under `on` can take up to about
15 + 106 + 30 = 151 s plus the search's database time with the defaults, and a proxy error is
still possible there. Whether to bound it too is open for shesha. Also: the router's 15 s ends
inside one 30 s embedding attempt, so a single slow embedding request always ends the router
(`router_failed`, fail closed) rather than being retried."""

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

GUARD_OFF_ENVIRONMENTS: Final = frozenset({"local", "test", "ci"})
"""Where `guard_routes = "off"` is allowed (M7 PR 1, D2 (a)): `business.HEADER_ENVIRONMENTS`, the
environments without customers (a test keeps the two equal; `business` imports this module, so it
cannot be imported here). Anywhere else, and in any environment added later, it is refused."""

TURN_TIMEOUT_RULE: Final = (
    "agent_turn_timeout_seconds must be >= agent_llm_deadline_seconds x agent_max_steps"
    " + 2 x agent_max_steps x guard_redis_timeout_seconds"
)
"""The turn's rule (M5 PR 4, with M7 PR 5's Redis bound, G9): every step's model call fits the
turn, and so do its budget reserve and settle, each bounded by the Redis timeout."""

ROUTER_TIMEOUT_RULE: Final = (
    "agent_router_timeout_seconds must be >= agent_llm_deadline_seconds"
    " + 2 x guard_redis_timeout_seconds (the router's one fallback call, with its budget reserve"
    " and settle)"
)
"""The router's rule (M5 PR 4, with M7 PR 5's Redis bound, G9; diego's PR 5 review [proposal]):
the same arithmetic as `TURN_TIMEOUT_RULE` for the router's one fallback call."""

GUARD_CONFIG_REFUSED: Final = (
    "guard_config_dir (VECTAPILOT_GUARD_CONFIG_DIR) is required outside local, test and ci when a"
    " guarded route serves businesses (tools_config_dir or extraction_config_dir set with"
    " guard_routes 'on', or guard_answer 'on')"
)
"""`create_app`'s refusal (M7 PR 1, G4): fixed text, settings names only."""

INSECURE_COOKIE_ENVIRONMENTS: Final = frozenset({"local", "test"})
"""Where `cookie_insecure_dev` is allowed (M8 PR 1): local development over plain http and the
tests. Not ci (its tests use an https base URL), never production."""

WIDGET_HTTP_ORIGIN_ENVIRONMENTS: Final = frozenset({"local"})
"""Where a widget file's `allowed_origins` may name `http://localhost` or `http://127.0.0.1` (M8
PR 1, as the plan says: "https except `localhost` and `127.0.0.1` in local"): a local demo page
that frames the widget. https everywhere else, test and ci included; the committed sample files
name https origins only, so they load in every environment."""

CORS_DEV_ENVIRONMENTS: Final = frozenset({"local"})
"""Where `cors_dev_origins` is allowed (M8 PR 1, cut 6): only a developer's machine."""

PUBLIC_ORIGIN_REFUSED: Final = (
    "public_origin (VECTAPILOT_PUBLIC_ORIGIN) is required when environment is 'production':"
    " session routes compare the browser's Origin with it (S5)"
)
"""`create_app`'s refusal (M8 PR 1): fixed text, the setting's name only."""

WIDGET_CLIENT_MARGIN_SECONDS: Final = 5.0
"""P7 (D15 (a)): the widget waits the server's turn bound plus this, so the server's
`turn_timeout` hand-off arrives before the client gives up [proposal, not a measurement]."""
CONFIRM_WRITE_BACKSTOP_SECONDS: Final = 5.0
"""`tools.spec.WRITE_TIMEOUT_S` (a test compares): the confirm route's write backstop, which the
widget's confirm timeout must exceed (P7). Not imported, so this module loads no tool code."""

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
    " paid {setting} (any but 'fake'): embeddings, /v1/answer and /v1/kb/search have no budget"
)
"""The API server's refusal (decided 2026-10-22, extended by D9 2026-10-23; reworded by M7 PR 5,
G9: the guarded routes' model calls have a budget, the rest does not), filled with a name from
`API_PAID_PROVIDERS`. Fixed text: it names settings and the rule, never a value."""

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

    environment: Environment = "local"
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
    fake while this is false (`require_api_paid_provider_opt_in`). M7's daily budget
    (`guard_budget_usd_per_business_per_day`, G9) covers only the model calls of guarded routes;
    embeddings, and `/v1/answer` and `/v1/kb/search` while `guard_answer` is off, stay
    unbudgeted, so the opt-in stays. The measurement CLI and the eval harness never read it:
    their `--live` runs have their own `--max-usd` reservation cap."""

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

    # ── Hybrid retrieval (M3 PR 4, ADR 0014) ──
    # Platform defaults, unmeasured (rule 3). `vector` stays the default until a paired dev
    # comparison against M2's vector baseline meets D15's adoption rule.
    rag_retrieval_mode: RetrievalMode = "vector"
    """`vector`: M2's path, unchanged. `hybrid`: the vector and full-text legs at
    `rag_candidate_k` each, in one REPEATABLE READ READ ONLY transaction, fused by RRF; both
    `GET /v1/kb/search` and `POST /v1/answer` follow it (ADR 0014 decisions 5-7). The eval
    harness refuses `hybrid` until M3 PR 5 adds `--retrieval-mode` (decided 2026-10-25)."""
    rag_candidate_k: int = Field(default=CANDIDATE_K, ge=MIN_CANDIDATE_K, le=MAX_K)
    """Each leg's k and the fused list's length in hybrid mode (D6, provisional). At least
    `rag_context_k` (checked in `_llm_rules`) and 10, at most `MAX_K`. Checked in either mode
    (decided 2026-10-25): in vector mode too, a `rag_context_k` above it stops startup."""
    rag_rrf_k: int = Field(default=RRF_K, ge=1)
    """RRF's k (`rrf_fuse`'s `k`): 60, fixed, not tuned (ADR 0014 decision 4). At least 1, no upper
    bound (decided 2026-10-25)."""

    # ── Tools (M5, ADR 0016 decision 1) ──
    tools_config_dir: Path | None = None
    """`VECTAPILOT_TOOLS_CONFIG_DIR`: the folder of per-business tool configurations and sandbox
    seeds (`data/sandbox/` in the repository; a read-only bind mount under compose, so the image is
    unchanged). When set, `create_app` reads and validates the whole folder before it serves
    anything (`tools.files.load_tools_dir`), and any missing or invalid business file stops
    startup. None: no business has tools, and `/v1/agent` hands off for every business. Required
    when `agent_router` is `on` (M5 PR 4), and needs `corpus_dir` with it."""
    action_lifetime_seconds: int = Field(default=900, gt=0, le=3600)
    """`VECTAPILOT_ACTION_LIFETIME_SECONDS`: how long a proposed write waits for its confirm call
    (W6) [proposal: 15 minutes, a configured value, not a measurement]. The database sets
    `expires_at` to `now()` plus this. Bounded to migration 0006's CHECK (more than zero, at most
    one hour), so a typo stops startup instead of refusing every proposal with SQLSTATE 23514
    (M5 PR 1 review, diego)."""
    corpus_dir: Path | None = None
    """`VECTAPILOT_CORPUS_DIR`: the corpus folder (`data/sample/` in the repository; a read-only
    bind mount under compose). Required with `tools_config_dir` (M5 PR 4): every business with
    tools needs its corpus manifest, whose `business_type` must equal its `tools.json`'s, so a
    typo in either can never switch W11 off for a clinic (`agent/profiles.py`). Only each
    manifest's business type is read, never a document. Required with `extraction_config_dir`
    too (M6 PR 1), for the same check against each extraction business file."""

    # ── Document extraction (M6, ADR 0017 decision 2) ──
    extraction_config_dir: Path | None = None
    """`VECTAPILOT_EXTRACTION_CONFIG_DIR`: the folder of extraction templates per business type
    and one file per business (`data/extraction/` in the repository; a read-only bind mount under
    compose). When set, `create_app` reads, validates and compiles the whole folder before it
    serves anything (`extraction.files.load_extraction_dir`), and any invalid file, a business
    type that differs from the corpus manifest's, a business file that loosens its template (E5),
    a date-format set mixing day-first and month-first, or a kind enabled without
    `synthetic: true` stops startup. Optional in PR 1; PR 3's route needs it. Needs
    `corpus_dir`."""

    # ── The agent (M5 PR 4, ADR 0016 decisions 4, 5, 7) ──
    # SAFETY BOUNDS [proposal], not latency targets: estimates, never measurements. The agent has
    # its own gateway instance with these limits; `/v1/answer`'s gateway keeps the llm_* ones.
    # Under `agent_router = on` the router (its embedding and at most one LLM fallback call) runs
    # BEFORE the turn, inside its own `agent_router_timeout_seconds` [decided 2026-11-11, PR 4
    # review; built in the router-timeout follow-up]. So the router and the turn together take at
    # most `agent_router_timeout_seconds + agent_turn_timeout_seconds`, checked at startup against
    # `AGENT_ROUTER_TURN_BOUND_SECONDS` (with the defaults 15 + 35 = 50 s of 60 s). Not the whole
    # request: a `rag` route's answer under `on` is outside it (that constant's docstring).
    agent_llm_attempt_timeout_seconds: Seconds = Field(default=6.0, le=60)
    """Each attempt's limit for an agent step (the agent's own `LLMGateway`)."""
    agent_llm_deadline_seconds: Seconds = Field(default=8.0, le=120)
    """One step's whole call: attempts, backoff waits and the one repair."""
    agent_max_steps: int = Field(default=4, ge=1, le=10)
    """Model steps per turn (`LoopLimits.max_steps`; at most `agent.types.MAX_STEPS_LIMIT`)."""
    agent_turn_timeout_seconds: Seconds = Field(default=35.0, le=300)
    """The whole turn's limit around the loop (`turn_timeout`). At least
    `agent_llm_deadline_seconds x agent_max_steps` plus each step's two Redis calls
    (`TURN_TIMEOUT_RULE`, M7 PR 5), so a turn of slow but successful steps is never cut off by its
    own budget, and with `agent_router_timeout_seconds` at most
    `AGENT_ROUTER_TURN_BOUND_SECONDS` (both checked in `_agent_problems`), so 300 is a field bound
    only."""
    agent_router: Literal["off", "on"] = "off"
    """`VECTAPILOT_AGENT_ROUTER` (decided D7 (a)): `off` (the default) fails health businesses
    closed on `/v1/agent` with their emergency guidance (W11); `on` puts M4's router first and
    needs `agent_router_model_dir` (a live model, checked at startup) and `tools_config_dir`.
    The eval harness's `router=none` is not a setting (`AgentService.respond_unrouted`)."""
    agent_router_model_dir: Path | None = None
    """`VECTAPILOT_AGENT_ROUTER_MODEL_DIR`: M4's model folder (`evals/reports/intents/<version>/
    <UTC>-local-model`) the router scores with under `agent_router = on`."""
    agent_router_timeout_seconds: Seconds = Field(default=15.0, le=60)
    """`VECTAPILOT_AGENT_ROUTER_TIMEOUT_SECONDS` [proposal: 15 s, not a measurement]: the router's
    whole limit under `agent_router = on` (the message's embedding, with the runner's retries,
    and at most one LLM fallback call). Its expiry hands off as `router_failed`, fail closed:
    before any score, urgent for a health business and high for any other [decided 2026-11-11,
    shesha]; after scoring, urgent when the classifier's top-1 scored as an urgent hand-off, else
    high; with the emergency guidance for a health business or an urgent hand-off
    (`AgentService`). A router exception hands off by the same rule, so the kind of failure
    never decides urgency [decided 2026-11-11, shesha]. At least
    `agent_llm_deadline_seconds` plus the fallback's budget reserve and settle
    (`ROUTER_TIMEOUT_RULE`, M7 PR 5), so a slow but successful fallback call is not always cut off,
    and with `agent_turn_timeout_seconds` within `AGENT_ROUTER_TURN_BOUND_SECONDS`. Both are
    checked whatever `agent_router` says, so switching the router on never meets a new refusal."""

    # ── Guards (M7, ADR 0018; D2 (a)) ──
    guard_routes: Literal["off", "on"] = "on"
    """`VECTAPILOT_GUARD_ROUTES`: the guards on `/v1/agent`, its confirm route and
    `/v1/extractions`. `off` only in local, test and ci (`GUARD_OFF_ENVIRONMENTS`), for tests of
    the unguarded path. Wired into those routes by PR 6 and PR 7: until then it changes nothing."""
    guard_answer: Literal["off", "on"] = "off"
    """`VECTAPILOT_GUARD_ANSWER`: the guards on `/v1/answer` and `/v1/kb/search`, off during M3's
    measurement window (G11). Wired by PR 8 (`guards.answer`): while off both routes are
    byte-identical to the base commit and M3's `AnswerService` serves; `on` builds a separate one
    over the chunk-screening search and a `BudgetedGateway`. PR 10 turns it on after shesha's last
    M3 live answer run (and then refuses `off` in production)."""
    guard_config_dir: Path | None = None
    """`VECTAPILOT_GUARD_CONFIG_DIR`: the guard folder (`data/safety/` from PR 4; a read-only mount
    under compose). When set, `create_app` reads and validates it and runs the term self-test
    (G4, G14) before serving anything; any problem stops startup. Needs `corpus_dir` (each
    business's type and languages come from its manifest). When it is None but a guarded route
    serves businesses (`guarded_routes_serve_businesses`), `create_app` refuses to start outside
    local, test and ci (`require_guard_config`); there, those routes hand off every message
    (`guards_not_configured`, wired by PR 6-8). Not a Settings rule: the MCP server and every CLI
    build `Settings()` without it."""

    # ── Limits and budgets (M7 PR 5, G9; D4 (a)) ──
    # PLACEHOLDERS [proposal], unmeasured: starting values while every business is synthetic,
    # tuned only on measured traffic (rule 3). Read by `guards.limits.LimitPolicy`; wired into
    # the routes by PR 6 (agent, confirm, extractions) and PR 8 (answer, search).
    guard_limit_business_per_minute: int = Field(default=120, ge=1, le=100_000)
    """`VECTAPILOT_GUARD_LIMIT_BUSINESS_PER_MINUTE`: requests per business per route per minute,
    the real control until M8 authenticates customers."""
    guard_limit_customer_per_minute: int = Field(default=10, ge=1, le=100_000)
    """`VECTAPILOT_GUARD_LIMIT_CUSTOMER_PER_MINUTE`: requests per customer reference per route
    per minute. Advisory: `customer_ref` is client-asserted until M8. At most the business's."""
    guard_budget_usd_per_business_per_day: Decimal = Field(
        default=Decimal("0.50"), gt=0, le=Decimal(100), max_digits=12, decimal_places=6
    )
    """`VECTAPILOT_GUARD_BUDGET_USD_PER_BUSINESS_PER_DAY`: the model spend one business may
    reserve per UTC day on guarded routes, in dollars (kept as integer micro-USD in Redis). $0.50
    while every business is synthetic."""
    guard_redis_timeout_seconds: float = Field(default=0.25, gt=0, le=10, allow_inf_nan=False)
    """`VECTAPILOT_GUARD_REDIS_TIMEOUT_SECONDS` [proposal: 0.25 s, a safety bound, not a
    measurement]: each limiter and budget call's limit; expiry fails closed. At most
    `readiness_timeout_seconds`, and inside the agent's turn (`TURN_TIMEOUT_RULE`)."""

    # ── Sessions and staff access (M8 PR 1, ADR 0019; D1, D3, D4, D12 (b)) ──
    # Lifetimes are configuration (P1), never measurements; each is bounded by migration 0008's
    # CHECK, so a typo stops startup instead of refusing every session with SQLSTATE 23514.
    public_origin: Origin | None = None
    """`VECTAPILOT_PUBLIC_ORIGIN`: the API's own origin as a browser names it
    (`scheme://host[:port]`, strict: `auth.origins`). S5's CSRF check compares every non-GET
    staff request's `Origin` with it, and session creation too. Required in production
    (`require_public_origin`, `create_app` only: the MCP server and the CLIs never serve a
    session), and https there. None elsewhere means no `Origin` matches: those routes answer 403.
    Under D12 (b) the widget and the dashboard share it; separate origins are an M10
    requirement."""
    widget_config_dir: Path | None = None
    """`VECTAPILOT_WIDGET_CONFIG_DIR`: the folder of per-business widget files
    (`data/widget/businesses/<business_id>.json`, read-only under compose). Optional: without it no
    business has a widget, and a business without a file has none either. Read whole and validated
    at startup (`widget.config.load_widget_dir`, `create_app`): any problem stops the app. Needs
    `corpus_dir` (each business's type and languages)."""
    ui_dir: Path | None = None
    """`VECTAPILOT_UI_DIR`: the read-only folder of `apps/web`'s product build that the API serves
    (D2 (a) with (i)). Served from PR 2; until then it changes nothing."""
    customer_session_lifetime_seconds: int = Field(default=43_200, gt=0, le=86_400)
    """`VECTAPILOT_CUSTOMER_SESSION_LIFETIME_SECONDS` [P1: 12 hours]: a customer session's
    absolute lifetime, set by the database's clock. At most migration 0008's 24 hours."""
    staff_session_lifetime_seconds: int = Field(default=28_800, gt=0, le=86_400)
    """`VECTAPILOT_STAFF_SESSION_LIFETIME_SECONDS` [P1: 8 hours]: a staff session's absolute
    lifetime (no idle timeout in M8: logout revokes). At most migration 0008's 24 hours."""
    staff_link_lifetime_seconds: int = Field(default=900, gt=0, le=3_600)
    """`VECTAPILOT_STAFF_LINK_LIFETIME_SECONDS` [P1: 15 minutes]: a one-time login link's lifetime
    (the owner CLI writes it). At most migration 0008's hour."""
    session_limit_business_per_minute: int = Field(default=60, ge=1, le=100_000)
    """`VECTAPILOT_SESSION_LIMIT_BUSINESS_PER_MINUTE` [proposal: 60, a placeholder, unmeasured]:
    new customer sessions per business per minute (S14: the per-business creation window, in
    Redis, failing closed): `POST /v1/widget/sessions` (`RouteSlug.SESSION`). Staff login has no
    window (step B3, zara's review): one keyed by a caller-written token prefix would only be a
    lockout switch; the link's 256 bits and single use are the control until M10's edge limits."""
    session_lookup_timeout_seconds: float = Field(default=2.0, gt=0, le=10, allow_inf_nan=False)
    """`VECTAPILOT_SESSION_LOOKUP_TIMEOUT_SECONDS` [proposal: 2 s, a safety bound, not a
    measurement]: one session lookup's limit, the role check and the transaction included. Past
    it the request gets S14's typed 503, never a hang."""
    cookie_insecure_dev: bool = False
    """`VECTAPILOT_COOKIE_INSECURE_DEV`: the staff cookie without `Secure` (and so named
    `vp_staff`, not `__Host-vp_staff`) for plain `http://localhost` (ADR 0019, Consequences).
    Local and test only (`INSECURE_COOKIE_ENVIRONMENTS`); anywhere else startup stops."""
    cors_dev_origins: tuple[Origin, ...] = ()
    """`VECTAPILOT_CORS_DEV_ORIGINS`: origins a local `next dev` server may call the API from
    (cut 6). Local only (`CORS_DEV_ENVIRONMENTS`); anywhere else a non-empty list stops startup.
    CORS stays off otherwise (D2 (a): same origin). Not wired in PR 1: no CORS middleware is
    installed whatever its value (cut 6; PR 4 decides whether `next dev` needs it)."""
    widget_turn_bound_seconds: float = Field(default=60.0, gt=0, le=120, allow_inf_nan=False)
    """`VECTAPILOT_WIDGET_TURN_BOUND_SECONDS` [D15 (a), P7: router 15 + turn 35 + margin 10 = 60 s,
    arithmetic over the defaults, not a measurement]: the outer bound of `POST
    /v1/widget/messages` (PR 2), past which the customer gets a `turn_timeout` hand-off. At least
    `agent_router_timeout_seconds + agent_turn_timeout_seconds`, so it only catches an overrun.
    The session response (PR 1) gives the widget its client timeout: this plus
    `WIDGET_CLIENT_MARGIN_SECONDS` (P7: 65 s)."""
    widget_confirm_timeout_seconds: float = Field(default=15.0, gt=0, le=60, allow_inf_nan=False)
    """`VECTAPILOT_WIDGET_CONFIRM_TIMEOUT_SECONDS` [P7: 15 s, proposed, not a measurement]: the
    widget's client timeout for a confirm, handed over in the session response. Above the confirm
    route's write backstop (`CONFIRM_WRITE_BACKSTOP_SECONDS`), so the client never gives up on a
    write the server is still allowed to finish."""

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
        if self.rag_candidate_k < self.rag_context_k:
            # M3 PR 4: the answer takes its rag_context_k hits from the top of the fused list.
            problems.append("rag_candidate_k must be >= rag_context_k")
        problems += self._embedding_problems()
        problems += self._agent_problems()
        if self.extraction_config_dir is not None and self.corpus_dir is None:
            problems.append(
                "corpus_dir (VECTAPILOT_CORPUS_DIR) is required with extraction_config_dir: each"
                " business's type is checked in both"
            )
        problems += self._guard_problems()
        problems += self._session_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _session_problems(self) -> list[str]:
        """M8 PR 1's fail-fast rules for sessions. Every message names fields, never a value."""
        problems: list[str] = []
        production = self.environment == "production"
        if production and self.public_origin is not None and is_loopback_http(self.public_origin):
            problems.append("public_origin must be https when environment is 'production'")
        if self.cookie_insecure_dev and self.environment not in INSECURE_COOKIE_ENVIRONMENTS:
            problems.append(
                "cookie_insecure_dev may be true only when environment is local or test"
            )
        if self.cors_dev_origins and self.environment not in CORS_DEV_ENVIRONMENTS:
            problems.append("cors_dev_origins may be set only when environment is local")
        if self.widget_config_dir is not None and self.corpus_dir is None:
            problems.append(
                "corpus_dir (VECTAPILOT_CORPUS_DIR) is required with widget_config_dir: each"
                " business's type and languages come from its corpus manifest"
            )
        turn = self.agent_router_timeout_seconds + self.agent_turn_timeout_seconds
        if self.widget_turn_bound_seconds < turn:
            problems.append(
                "widget_turn_bound_seconds must be >= agent_router_timeout_seconds +"
                " agent_turn_timeout_seconds (D15: the bound only catches an overrun)"
            )
        if self.widget_confirm_timeout_seconds <= CONFIRM_WRITE_BACKSTOP_SECONDS:
            problems.append(
                "widget_confirm_timeout_seconds must be above the confirm route's write backstop"
                f" ({CONFIRM_WRITE_BACKSTOP_SECONDS:g} s, P7)"
            )
        return problems

    def _guard_problems(self) -> list[str]:
        """M7 PR 1's fail-fast rules for the guards. Every message names fields, never a value."""
        problems: list[str] = []
        if self.guard_routes == "off" and self.environment not in GUARD_OFF_ENVIRONMENTS:
            problems.append("guard_routes may be 'off' only when environment is local, test or ci")
        if self.guard_config_dir is not None and self.corpus_dir is None:
            problems.append(
                "corpus_dir (VECTAPILOT_CORPUS_DIR) is required with guard_config_dir: each"
                " business's type and languages come from its corpus manifest"
            )
        if self.guard_limit_customer_per_minute > self.guard_limit_business_per_minute:
            problems.append(
                "guard_limit_customer_per_minute must be <= guard_limit_business_per_minute"
            )
        if self.guard_redis_timeout_seconds > self.readiness_timeout_seconds:
            problems.append("guard_redis_timeout_seconds must be <= readiness_timeout_seconds")
        return problems

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

    def _agent_problems(self) -> list[str]:
        """M5 PR 4's fail-fast rules for the agent. Every message names fields, never a value."""
        problems: list[str] = []
        if self.agent_llm_deadline_seconds < self.agent_llm_attempt_timeout_seconds:
            problems.append(
                "agent_llm_deadline_seconds must be >= agent_llm_attempt_timeout_seconds"
            )
        # M7 PR 5 (G9): each step may also reserve and settle its budget, each call bounded by
        # the Redis timeout. With the defaults: 8 x 4 + 2 x 4 x 0.25 = 34 <= 35 (arithmetic).
        steps = self.agent_max_steps
        turn = self.agent_llm_deadline_seconds * steps
        turn += 2 * steps * self.guard_redis_timeout_seconds
        if turn > self.agent_turn_timeout_seconds:
            problems.append(TURN_TIMEOUT_RULE)
        router_call = self.agent_llm_deadline_seconds + 2 * self.guard_redis_timeout_seconds
        if self.agent_router_timeout_seconds < router_call:
            problems.append(ROUTER_TIMEOUT_RULE)
        if (
            self.agent_router_timeout_seconds + self.agent_turn_timeout_seconds
            > AGENT_ROUTER_TURN_BOUND_SECONDS
        ):
            problems.append(
                "agent_router_timeout_seconds + agent_turn_timeout_seconds must be <="
                f" {AGENT_ROUTER_TURN_BOUND_SECONDS:g} (AGENT_ROUTER_TURN_BOUND_SECONDS)"
            )
        if self.agent_router == "on":
            if self.agent_router_model_dir is None:
                problems.append("agent_router_model_dir is required when agent_router is 'on'")
            if self.tools_config_dir is None:
                problems.append("tools_config_dir is required when agent_router is 'on'")
        if self.tools_config_dir is not None and self.corpus_dir is None:
            problems.append(
                "corpus_dir (VECTAPILOT_CORPUS_DIR) is required with tools_config_dir: W11 checks"
                " each business's type in both"
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

    def guarded_routes_serve_businesses(self) -> bool:
        """Whether a guarded route serves any business: `/v1/agent` (its confirm route included)
        with a tool folder or `/v1/extractions` with an extraction folder while `guard_routes` is
        on, or `/v1/answer` and `/v1/kb/search` while `guard_answer` is on."""
        configured = self.tools_config_dir is not None or self.extraction_config_dir is not None
        return (self.guard_routes == "on" and configured) or self.guard_answer == "on"

    def guards_not_configured(self) -> bool:
        """Whether guarded routes serve businesses without a guard folder: allowed only in local,
        test and ci, where those routes hand off every message (`guards_not_configured`)."""
        return self.guarded_routes_serve_businesses() and self.guard_config_dir is None

    def require_guard_config(self) -> None:
        """The API server's startup rule (M7 PR 1, G4): outside local, test and ci, guarded routes
        that serve businesses need the guard folder. Like `require_api_paid_provider_opt_in`, only
        `create_app` calls it (the MCP server and the CLIs build Settings without the folder).
        Raised as a ValidationError from fixed text."""
        if not self.guards_not_configured() or self.environment in GUARD_OFF_ENVIRONMENTS:
            return
        raise ValidationError.from_exception_data(
            type(self).__name__,
            [
                {
                    "type": PydanticCustomError("guards_not_configured", GUARD_CONFIG_REFUSED),
                    "loc": ("guard_config_dir",),
                    "input": None,
                }
            ],
            hide_input=True,
        )

    def require_public_origin(self) -> None:
        """The API server's startup rule (M8 PR 1): production needs `public_origin`. Like
        `require_guard_config`, only `create_app` calls it (the MCP server and the CLIs build
        Settings without it). Raised as a ValidationError from fixed text."""
        if self.environment != "production" or self.public_origin is not None:
            return
        raise ValidationError.from_exception_data(
            type(self).__name__,
            [
                {
                    "type": PydanticCustomError("public_origin_required", PUBLIC_ORIGIN_REFUSED),
                    "loc": ("public_origin",),
                    "input": None,
                }
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
