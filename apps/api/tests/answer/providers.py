"""Test doubles for the answer model, and an independent reader of the prompt's blocks.

`parse_blocks` reads a prompt the way the system prompt tells the model to: only lines with the
real boundary are markers. It is written apart from `answer/prompt.py` on purpose, so a bug in the
prompt's layout cannot be mirrored by the code that checks it.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.types import TextBlock, Usage

BOUNDARY_LINE: Final = re.compile(r"^Boundary: ([0-9a-f]{16})$", re.MULTILINE)
UUID_TEXT: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class Block:
    kind: Literal["DOCUMENT", "QUESTION"]
    chunk_id: str | None
    body: str


def parse_blocks(user: str, boundary: str) -> tuple[list[Block], list[str]]:
    """The blocks of a user message, and the lines outside any block."""
    b = re.escape(boundary)
    starts = {
        "DOCUMENT": re.compile(rf"^<<<DOCUMENT {b} id=([0-9a-f-]{{36}})>>>$"),
        "QUESTION": re.compile(rf"^<<<QUESTION {b}>>>$"),
    }
    ends = {kind: f"<<<END {kind} {boundary}>>>" for kind in starts}
    blocks: list[Block] = []
    outside: list[str] = []
    current: tuple[Literal["DOCUMENT", "QUESTION"], str | None, list[str]] | None = None
    for line in user.split("\n"):
        if current is None:
            for kind in ("DOCUMENT", "QUESTION"):
                found = starts[kind].fullmatch(line)
                if found:
                    current = (kind, found.group(1) if kind == "DOCUMENT" else None, [])
                    break
            else:
                # Our closing line names the boundary; a marker-shaped line here would be a bug.
                assert not line.startswith("<<<"), f"a stray marker outside any block: {line!r}"
                outside.append(line)
        elif line == ends[current[0]]:
            blocks.append(Block(current[0], current[1], "\n".join(current[2])))
            current = None
        else:
            assert boundary not in line, "the boundary inside a block's text"
            current[2].append(line)
    assert current is None, "a block was never closed"
    return blocks, outside


def boundary_of(request: ProviderRequest) -> str:
    assert request.system is not None
    found = BOUNDARY_LINE.search(request.system)
    assert found is not None, "no boundary line in the system prompt"
    return found.group(1)


def reply(outcome: str = "answered", answer: str = "", citations: Sequence[str] = ()) -> str:
    return json.dumps({"outcome": outcome, "answer": answer, "citations": list(citations)})


@dataclass
class CitingProvider:
    """A provider that reads its prompt and answers `answer`, citing:

    - `cite="markers"`: every id from a real start marker (what an obedient model does);
    - `cite="everything"`: every UUID-shaped text anywhere in the user message, block text
      included (a model that followed an injected or forged id).

    `calls` records every request; `aclose` only records that it was called.
    """

    answer: str = "A scripted, cited answer."
    cite: Literal["markers", "everything"] = "markers"
    calls: list[ProviderRequest] = field(default_factory=list)
    closed: bool = False

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        user = request.messages[-1].content
        if self.cite == "markers":
            blocks, _ = parse_blocks(user, boundary_of(request))
            ids = [block.chunk_id for block in blocks if block.chunk_id is not None]
        else:
            ids = UUID_TEXT.findall(user)
        return ProviderResponse(
            content=(TextBlock(text=reply("answered", self.answer, ids)),),
            stop_reason="end_turn",
            usage=Usage(input_tokens=100, output_tokens=20),
            model=request.model,
            request_id=f"citing-req-{len(self.calls)}",
            inference_geo="global",
        )

    async def aclose(self) -> None:
        self.closed = True
