"""Test doubles for the router: a scripted fallback, made-up scores, every `GatewayError` kind, the
real gateway on a fake provider, and an independent reader of the router prompt's block.

None of this decides a route: `route_message` is shesha's H1, and nothing here mirrors it.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from tests.embeddings.scripted import FakeClock
from vectapilot_api.embeddings.base import InputTooLarge, RateLimited
from vectapilot_api.gateway.cache import ResponseCache
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    BillingError,
    DeadlineExceeded,
    GatewayError,
    InvalidRequest,
    ModelRefused,
    OutputTruncated,
    ProviderError,
    SpendLimitReached,
    StructuredOutputInvalid,
    Transient,
)
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import RetryPolicy
from vectapilot_api.intents.classifier import IntentScores
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

BUSINESS: Final = "sunrise-clinic"
TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
MESSAGE: Final = "TEST-ONLY message kq4router: may I bring my own blanket?"
"""Made up. The marker `kq4router` must never appear in a log line."""


def scores(probabilities: Mapping[Intent, float]) -> IntentScores:
    """Scores with every intent not named at 0. Use dyadic values (1/2, 1/4, 3/8, ...): they are
    exact floats, so sums and comparisons at a threshold are exact too."""
    return IntentScores({i: probabilities.get(i, 0.0) for i in DECISION_ORDER})


FallbackStep = Intent | BaseException


@dataclass
class ScriptedFallback:
    """An `IntentFallback` that plays `script`, one step per call: an intent is returned, an
    exception raised. `calls` records `(message, business_id, trace_id)` of every call; a call
    beyond the script fails the test."""

    script: list[FallbackStep] = field(default_factory=list)
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def __call__(self, message: str, *, business_id: str, trace_id: str) -> Intent:
        self.calls.append((message, business_id, trace_id))
        if len(self.calls) > len(self.script):
            raise AssertionError(f"ScriptedFallback: call {len(self.calls)} was not expected")
        step = self.script[len(self.calls) - 1]
        if isinstance(step, BaseException):
            raise step
        return step


def every_gateway_error() -> list[GatewayError]:
    """One instance of every `GatewayError` class in the package, the base included."""
    return [
        GatewayError(),
        SpendLimitReached(status_code=429),
        BillingError(status_code=402),
        Transient(status_code=529),
        RateLimited(status_code=429, retry_after=3.0),
        AuthenticationFailed(status_code=401),
        InvalidRequest(status_code=400),
        InputTooLarge(status_code=413),
        ProviderError(status_code=500),
        DeadlineExceeded(deadline_s=10.0),
        ModelRefused(),
        OutputTruncated(stop_reason="max_tokens"),
        StructuredOutputInvalid(problems=(("intent", "enum"),)),
    ]


def package_gateway_error_classes() -> set[type[GatewayError]]:
    """Every subclass of `GatewayError` defined in `vectapilot_api` (imported so far), and it."""
    found: set[type[GatewayError]] = {GatewayError}
    pending = [GatewayError]
    while pending:
        for sub in pending.pop().__subclasses__():
            if sub.__module__.startswith("vectapilot_api.") and sub not in found:
                found.add(sub)
                pending.append(sub)
    return found


def gateway_for(provider: Provider, cache: ResponseCache | None = None) -> LLMGateway:
    """The real gateway and the packaged catalog on `provider`; backoff waits move a fake clock."""
    clock = FakeClock()
    return LLMGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=5.0,
        deadline_s=10.0,
        retry_policy=RetryPolicy(max_retries=0, base_delay_s=0.5, max_delay_s=8.0),
        max_repairs=1,
        cache=cache,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )


BOUNDARY_LINE: Final = re.compile(r"^Boundary: ([0-9a-f]{16})$", re.MULTILINE)


def boundary_in(system: str | None) -> str:
    assert system is not None
    found = BOUNDARY_LINE.search(system)
    assert found is not None, "no boundary line in the system prompt"
    return found.group(1)


def message_blocks(user: str, boundary: str) -> tuple[list[str], list[str]]:
    """The bodies of the MESSAGE blocks of a user message, and the lines outside any block.

    Written apart from `intents/fallback.py`, the way its system prompt tells the model to read:
    only the exact start and end lines with the real boundary are markers."""
    start, end = f"<<<MESSAGE {boundary}>>>", f"<<<END MESSAGE {boundary}>>>"
    bodies: list[str] = []
    outside: list[str] = []
    current: list[str] | None = None
    for line in user.split("\n"):
        if current is None:
            if line == start:
                current = []
            else:
                assert not line.startswith("<<<"), "a stray marker outside the block"
                outside.append(line)
        elif line == end:
            bodies.append("\n".join(current))
            current = None
        else:
            assert boundary not in line, "the boundary inside the block's text"
            current.append(line)
    assert current is None, "a block was never closed"
    return bodies, outside


def intent_reply(intent: str) -> str:
    return '{"intent": "' + intent + '"}'


def names(errors: Iterable[BaseException]) -> list[str]:
    return [type(e).__name__ for e in errors]
