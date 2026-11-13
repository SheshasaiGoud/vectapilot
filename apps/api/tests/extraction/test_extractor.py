"""M6 PR 2: the extraction's own gateway and its one structured call (`extraction/extractor.py`;
D3 (a), E3, E8, E11). Fake provider only: no network, no key, $0."""

import json
import logging
from pathlib import Path
from typing import Any, Final

import pytest

from tests.conftest import make_settings
from tests.extraction.fakes import (
    CANARY,
    PAWS_ID,
    REFERRAL_PAGE,
    SUNRISE_ID,
    TRACE,
    pages,
    referral,
)
from tests.intents.fakes import every_gateway_error
from vectapilot_api.extraction import extractor as extractor_module
from vectapilot_api.extraction.extractor import (
    EXTRACT_ATTEMPT_TIMEOUT_S,
    EXTRACT_DEADLINE_S,
    FieldExtractor,
    build_extraction_gateway,
)
from vectapilot_api.extraction.prompt import EXTRACT_ALIAS, EXTRACT_PROMPT_VERSION
from vectapilot_api.extraction.text import DocumentText
from vectapilot_api.gateway import gateway as gateway_module
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.errors import (
    GatewayError,
    OutputTruncated,
    StructuredOutputInvalid,
)
from vectapilot_api.gateway.gateway import LLM_CALL
from vectapilot_api.gateway.providers.fake import INVALID_JSON_TEXT, FakeProvider, FakeReply

pytestmark = pytest.mark.anyio

SETTINGS: Final = make_settings(llm_max_repairs=1, llm_max_retries=0)
"""No retries, so a scripted error reaches the caller after one attempt (no backoff waits)."""
DOCUMENT: Final = DocumentText(pages("\n".join(REFERRAL_PAGE)), "0" * 64)


def answer(**overrides: Any) -> dict[str, Any]:
    """A shape-valid answer for the referral template: every field null unless overridden."""
    fields = {spec.name: None for spec in referral().template.fields}
    body: dict[str, dict[str, Any]] = {
        name: {"value": None, "quote": None, "unsure": False} for name in fields
    }
    for name, value in overrides.items():
        body[name] = {"value": value, "quote": f"{name}: {value}", "unsure": False}
    return body


def reply(**overrides: Any) -> FakeReply:
    return FakeReply(text=json.dumps(answer(**overrides)))


async def extract(provider: FakeProvider, business_id: str = SUNRISE_ID) -> Any:
    async with build_extraction_gateway(SETTINGS, provider=provider) as gateway:
        return await FieldExtractor(gateway)(
            referral(), DOCUMENT, business_id=business_id, trace_id=TRACE
        )


def llm_calls(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, LLM_CALL) for r in caplog.records if r.getMessage() == LLM_CALL]


# ── Its own gateway (E11) ──────────────────────────────────────────────────────────────────────


def test_the_time_limits_are_the_proposal_and_an_attempt_fits_the_deadline() -> None:
    assert (EXTRACT_ATTEMPT_TIMEOUT_S, EXTRACT_DEADLINE_S) == (15.0, 30.0)
    assert EXTRACT_ATTEMPT_TIMEOUT_S <= EXTRACT_DEADLINE_S


async def test_the_gateway_has_its_own_limits_settings_retries_and_no_cache() -> None:
    provider = FakeProvider()
    async with build_extraction_gateway(SETTINGS, provider=provider) as gateway:
        assert (gateway._attempt_timeout_s, gateway._deadline_s) == (15.0, 30.0)
        assert (gateway._retry_policy, gateway._max_repairs) == (SETTINGS.llm_retry_policy(), 1)
        assert gateway._cache is None
        assert gateway._provider is provider
    assert provider.closed


async def test_no_setting_can_pick_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # E11: the caller passes the provider; `build_provider(settings)` is never called.
    def refuse(*_: object) -> None:
        raise AssertionError("build_provider must not be called for extraction")

    monkeypatch.setattr(gateway_module, "build_provider", refuse)
    provider = FakeProvider([reply(patient_name="Asha")])

    await extract(provider)

    assert len(provider.calls) == 1


async def test_a_catalog_without_the_extraction_alias_is_refused_before_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_catalog()
    without = catalog.model_copy(
        update={"aliases": {k: v for k, v in catalog.aliases.items() if k != EXTRACT_ALIAS}}
    )
    assert without.aliases and EXTRACT_ALIAS not in without.aliases
    monkeypatch.setattr(extractor_module, "load_catalog", lambda: without)
    built: list[object] = []
    monkeypatch.setattr(extractor_module, "LLMGateway", lambda *a, **k: built.append(a))
    provider = FakeProvider()

    with pytest.raises(ValueError, match=f"no {EXTRACT_ALIAS!r} alias"):
        async with build_extraction_gateway(SETTINGS, provider=provider):
            pass

    assert built == [] and provider.calls == []


def test_the_module_never_builds_a_provider_itself() -> None:
    source = Path(extractor_module.__file__).read_text("utf-8")
    assert "build_provider" not in source.split('"""', 2)[2]  # code, not the docstring


async def test_two_identical_extractions_both_call_the_model() -> None:
    # No response cache: the answer is personal data (E11).
    provider = FakeProvider([reply(patient_name="Asha"), reply(patient_name="Asha")])
    async with build_extraction_gateway(SETTINGS, provider=provider) as gateway:
        extractor = FieldExtractor(gateway)
        for _ in range(2):
            await extractor(referral(), DOCUMENT, business_id=SUNRISE_ID, trace_id=TRACE)
    assert len(provider.calls) == 2


# ── The call ───────────────────────────────────────────────────────────────────────────────────


async def test_one_structured_call_on_small_with_the_prompt_and_the_schema(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    provider = FakeProvider([reply(patient_name=f"Asha {CANARY}")])

    proposal = await extract(provider)

    assert isinstance(proposal.answer, referral().output_model)
    assert proposal.answer.patient_name.value == f"Asha {CANARY}"  # type: ignore[attr-defined]
    assert proposal.prompt.version == EXTRACT_PROMPT_VERSION
    [call] = provider.calls
    assert call.max_tokens == 1024  # the `small` alias's cap, unchanged (M3's window)
    assert call.system is not None and call.system.startswith(proposal.prompt.system)
    assert '"patient_name"' in call.system  # the gateway quotes the output model's schema
    assert [m.content for m in call.messages] == [proposal.prompt.user]
    [record] = llm_calls(caplog)
    assert (record["operation"], record["alias"], record["business_id"]) == (
        "extract-fields",
        "small",
        SUNRISE_ID,
    )
    assert CANARY.casefold() not in caplog.text.casefold()
    assert CANARY not in repr(proposal)


async def test_another_businesss_configuration_is_refused_before_any_call() -> None:
    # E8: context is injected; a request for one business never runs with another's template.
    provider = FakeProvider()
    with pytest.raises(ValueError, match="another business"):
        await extract(provider, business_id=PAWS_ID)
    assert provider.calls == []


@pytest.mark.parametrize("error", every_gateway_error(), ids=lambda e: type(e).__name__)
async def test_gateway_errors_propagate_unchanged(error: GatewayError) -> None:
    # E3: the service turns each into an all-review extraction; the extractor never hides one.
    with pytest.raises(type(error)) as caught:
        await extract(FakeProvider([error]))
    assert caught.value is error


async def test_a_truncated_answer_is_output_truncated() -> None:
    provider = FakeProvider([FakeReply(text='{"patient_name": ', stop_reason="max_tokens")])
    with pytest.raises(OutputTruncated):
        await extract(provider)


async def test_invalid_output_twice_is_structured_output_invalid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    extra = answer() | {"business_id": {"value": CANARY, "quote": None, "unsure": False}}
    provider = FakeProvider([FakeReply(text=INVALID_JSON_TEXT), FakeReply(text=json.dumps(extra))])

    with pytest.raises(StructuredOutputInvalid) as caught:
        await extract(provider)

    assert len(provider.calls) == 2  # one repair, never more
    assert CANARY not in str(caught.value) and CANARY not in caplog.text
