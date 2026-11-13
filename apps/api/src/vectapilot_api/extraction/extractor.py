"""The extraction's model call and its own LLM gateway (ADR 0017 decision 3, decided D3 (a);
E1, E11; M6 PR 2).

**Its own gateway, from the existing constructor.** Extraction does not share `/v1/answer`'s
gateway (M3's measurement window freezes that one) nor the agent's: a document needs its own
time limits [proposal: an attempt of `EXTRACT_ATTEMPT_TIMEOUT_S` inside a deadline of
`EXTRACT_DEADLINE_S`]. `build_extraction_gateway` builds a separate `LLMGateway` with the same
catalog (`models.toml` unchanged), retry policy and repair limit as Settings give. Nothing in
`gateway/` changes. Not measured latencies: both are safety bounds.

**No response cache (E11).** `cache=None`, always: the answer holds personal data (names, dates
of birth, policy numbers), and a cached answer would outlive the stored extraction's expiry (E10).

**No paid call by default (E11).** The builder takes the provider from its caller and never calls
`build_provider(settings)`, so no setting can make it reach a paid model: PR 3's route passes the
fake provider in M6, and only `extracteval --live` (PR 4, shesha's) passes a real one, behind its
cap.

**The call.** `complete_structured` with alias `small` and operation `extract-fields`, scoped by
the business and the request's trace id; the schema is the business's kind's closed output model
(shape only, `config.compile_output_model`). Every `GatewayError` propagates unchanged: the
service turns each one into an all-review extraction (E3). Nothing here logs; the gateway writes
its metadata-only `llm_call` record.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from pydantic import BaseModel

from vectapilot_api.config import Settings
from vectapilot_api.extraction.config import KindConfig
from vectapilot_api.extraction.prompt import (
    EXTRACT_ALIAS,
    ExtractPrompt,
    build_extract_prompt,
    build_extract_request,
)
from vectapilot_api.extraction.text import DocumentText
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import LLMResult

EXTRACT_ATTEMPT_TIMEOUT_S: Final = 15.0
"""One attempt's limit [proposal]: a bound, not a measured latency."""
EXTRACT_DEADLINE_S: Final = 30.0
"""The whole call's limit, retries and the repair included [proposal]; at least one attempt."""


@asynccontextmanager
async def build_extraction_gateway(
    settings: Settings, *, provider: Provider
) -> AsyncIterator[LLMGateway]:
    """Yield the extraction's gateway on `provider`; close the provider on exit. The caller picks
    the provider (module docstring, E11); the cache is always off."""
    catalog = load_catalog()
    if EXTRACT_ALIAS not in catalog.aliases:
        raise ValueError(f"the model catalog has no {EXTRACT_ALIAS!r} alias for extraction")
    gateway = LLMGateway(
        provider,
        catalog,
        attempt_timeout_s=EXTRACT_ATTEMPT_TIMEOUT_S,
        deadline_s=EXTRACT_DEADLINE_S,
        retry_policy=settings.llm_retry_policy(),
        max_repairs=settings.llm_max_repairs,
        cache=None,
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()


@dataclass(frozen=True)
class Proposal:
    """The model's proposal for one document: one `{value, quote, unsure}` per field, validated
    for shape only. Untrusted until PR 2's signals check it (E1, E2)."""

    answer: BaseModel = field(repr=False)
    result: LLMResult = field(repr=False)
    """Usage, cost and the model that answered, for the eval (PR 4); its text is never logged."""
    prompt: ExtractPrompt


class FieldExtractor:
    """One structured call per document, through the extraction's own gateway."""

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway

    async def __call__(
        self, kind: KindConfig, document: DocumentText, *, business_id: str, trace_id: str
    ) -> Proposal:
        """The model's proposal for `document` under `kind`. Raises the gateway's typed errors
        unchanged.

        Preconditions, checked before any model call (a caller bug, never a fail-closed review):
        `kind` is `business_id`'s own (context is injected, E8: one business's request never runs
        with another's configuration), a slug business_id and a W3C trace_id (ValueError)."""
        if kind.business_id != business_id:
            raise ValueError("the kind's configuration belongs to another business")
        prompt = build_extract_prompt(kind, document.pages)
        request = build_extract_request(prompt, business_id=business_id, trace_id=trace_id)
        structured = await self._gateway.complete_structured(request, kind.output_model)
        return Proposal(answer=structured.value, result=structured.result, prompt=prompt)
