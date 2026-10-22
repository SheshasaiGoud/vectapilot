"""The dry run's answer model: canned, obviously fake, free (M2 PR 8).

A dry run exercises the whole path (ingest, search, the answer service's prompt, the gateway's
structured output and its records, the citation check) with no paid call. `DryRunAnswerProvider`
reads the prompt the way the system prompt tells a model to (only lines carrying the real
boundary are markers), cites the first document block, and answers `DRY_RUN_ANSWER`. Like M1's dry
run it is a fresh `FakeProvider` per request, scripted with that one reply: no network, no key.
Its text marks every answer as a dry run, so a dry-run report can never pass for model output.
"""

import json
import re
from typing import Final

from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply

DRY_RUN_ANSWER: Final = "DRY RUN: a canned answer; no model was called."

_BOUNDARY: Final = re.compile(r"^Boundary: ([0-9a-f]{16})$", re.MULTILINE)


def first_document_id(request: ProviderRequest) -> str | None:
    """The id in the first real `<<<DOCUMENT <boundary> id=...>>>` line of the user message."""
    found = _BOUNDARY.search(request.system or "")
    if found is None or not request.messages:
        return None
    start = re.compile(rf"^<<<DOCUMENT {found.group(1)} id=([0-9a-f-]{{36}})>>>$", re.MULTILINE)
    first = start.search(request.messages[-1].content)
    return None if first is None else first.group(1)


def dry_run_reply(request: ProviderRequest) -> str:
    """The JSON `ModelAnswer` the dry run gives: answered, citing the first document; or
    dont_know when the prompt holds none (never expected: no evidence means no model call)."""
    chunk_id = first_document_id(request)
    if chunk_id is None:
        return json.dumps({"outcome": "dont_know", "answer": "", "citations": []})
    return json.dumps({"outcome": "answered", "answer": DRY_RUN_ANSWER, "citations": [chunk_id]})


class DryRunAnswerProvider:
    """A `Provider` that answers every request with `dry_run_reply`, through a fresh fake."""

    def __init__(self) -> None:
        self.requests = 0

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests += 1
        fake = FakeProvider([FakeReply(text=dry_run_reply(request))])
        return await fake.send(request)

    async def aclose(self) -> None:
        """Nothing to release."""
