"""A scripted agent model that reads its prompt, and an independent reader of the agent prompt.

`parse_agent_blocks` reads a user message the way the system prompt tells the model to: only a
line that is exactly a marker with the real boundary is one. Written apart from
`agent/prompt.py` on purpose (as tests/answer/providers.py is for the answer prompt), so a bug in
the prompt's layout cannot be mirrored by the code that checks it.

`BlockCheckingProvider` is a fake provider that, before it answers each call, parses the prompt
and fails the test if a block is unclosed, a boundary appears inside a block's text, or a marker
line stands outside any block; then it plays its script of step payloads (JSON) or errors.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.types import TextBlock, Usage

BOUNDARY_LINE: Final = re.compile(r"^Boundary: ([0-9a-f]{16})$", re.MULTILINE)


@dataclass(frozen=True)
class AgentBlock:
    kind: Literal["MESSAGE", "RESULT"]
    step: int | None
    body: str


def boundary_of(request: ProviderRequest) -> str:
    assert request.system is not None
    found = BOUNDARY_LINE.search(request.system)
    assert found is not None, "no boundary line in the system prompt"
    return found.group(1)


def parse_agent_blocks(user: str, boundary: str) -> tuple[list[AgentBlock], list[str]]:
    """The blocks of an agent user message, and the lines outside any block."""
    b = re.escape(boundary)
    message_start = re.compile(rf"^<<<MESSAGE {b}>>>$")
    result_start = re.compile(rf"^<<<RESULT {b} step=([1-9][0-9]*)>>>$")
    ends = {"MESSAGE": f"<<<END MESSAGE {boundary}>>>", "RESULT": f"<<<END RESULT {boundary}>>>"}
    blocks: list[AgentBlock] = []
    outside: list[str] = []
    current: tuple[Literal["MESSAGE", "RESULT"], int | None, list[str]] | None = None
    for line in user.split("\n"):
        if current is None:
            if message_start.fullmatch(line):
                current = ("MESSAGE", None, [])
            elif found := result_start.fullmatch(line):
                current = ("RESULT", int(found.group(1)), [])
            else:
                assert not line.startswith("<<<"), (
                    f"a marker-shaped line outside any block: {line!r}"
                )
                outside.append(line)
        elif line == ends[current[0]]:
            blocks.append(AgentBlock(current[0], current[1], "\n".join(current[2])))
            current = None
        else:
            assert boundary not in line, "the boundary inside a block's text"
            current[2].append(line)
    assert current is None, "a block was never closed"
    return blocks, outside


def step_json(payload: Mapping[str, object]) -> str:
    return json.dumps({"step": payload}, ensure_ascii=False)


def call_json(tool: str, **arguments: object) -> str:
    return step_json({"action": "call_tool", "tool": tool, "arguments": arguments})


def reply_json(text: str) -> str:
    return step_json({"action": "reply", "text": text})


def handoff_json(reason: str) -> str:
    return step_json({"action": "handoff", "reason": reason})


@dataclass
class BlockCheckingProvider:
    """Plays `script` (step JSON texts, or gateway errors to raise), one per call, after checking
    the prompt's blocks. `seen` holds each call's parsed blocks; `calls` each request."""

    script: list[str | GatewayError]
    calls: list[ProviderRequest] = field(default_factory=list)
    seen: list[list[AgentBlock]] = field(default_factory=list)
    closed: bool = False

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        blocks, _ = parse_agent_blocks(request.messages[-1].content, boundary_of(request))
        self.seen.append(blocks)
        if not self.script:
            raise AssertionError(f"BlockCheckingProvider: call {len(self.calls)} has no script")
        item = self.script.pop(0)
        if isinstance(item, GatewayError):
            raise item
        return ProviderResponse(
            content=(TextBlock(text=item),),
            stop_reason="end_turn",
            usage=Usage(input_tokens=100, output_tokens=20),
            model=request.model,
            request_id=f"agent-req-{len(self.calls)}",
            inference_geo="global",
        )

    async def aclose(self) -> None:
        self.closed = True
