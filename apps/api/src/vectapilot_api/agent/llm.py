"""The agent's own LLM gateway (ADR 0016 decision 4; M3's measurement window; M5 PR 4).

The agent does not share `/v1/answer`'s gateway: a step needs a shorter attempt timeout and
deadline (`agent_llm_attempt_timeout_seconds`, `agent_llm_deadline_seconds`, validated in
Settings so that `deadline x agent_max_steps` fits the turn), and `/v1/answer`'s instance must
stay exactly as M3's window froze it. So this builds a second `LLMGateway` through the existing
constructor and the existing `build_provider`, with the same catalog (`models.toml` unchanged),
retry policy and repair limit. Nothing in `gateway/` changes.

With the Anthropic provider, `build_provider` sets the SDK's own backstop from
`llm_attempt_timeout_seconds` (plus 1 s). With the defaults (15 s against the agent's 6 s) that is
above the agent's attempt timeout, so the gateway's own limit ends an attempt first (one retry
layer, ADR 0006). Not a measured latency: both are safety bounds.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from vectapilot_api.agent.prompt import AGENT_ALIAS
from vectapilot_api.config import Settings
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import LLMGateway, build_provider
from vectapilot_api.gateway.providers.base import Provider


@asynccontextmanager
async def build_agent_gateway(
    settings: Settings, *, provider: Provider | None = None
) -> AsyncIterator[LLMGateway]:
    """Yield the agent's gateway; close its provider on exit. `provider` replaces the one Settings
    name (tests pass a scripted fake), as in `build_gateway`."""
    catalog = load_catalog()
    if AGENT_ALIAS not in catalog.aliases:
        raise ValueError(f"the model catalog has no {AGENT_ALIAS!r} alias for agent steps")
    retry_policy = settings.llm_retry_policy()
    gateway = LLMGateway(
        build_provider(settings) if provider is None else provider,
        catalog,
        attempt_timeout_s=settings.agent_llm_attempt_timeout_seconds,
        deadline_s=settings.agent_llm_deadline_seconds,
        retry_policy=retry_policy,
        max_repairs=settings.llm_max_repairs,
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()
