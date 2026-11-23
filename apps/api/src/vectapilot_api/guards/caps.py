"""Input caps per surface (G3) [proposal]: synchronous work is bounded by construction.

`asyncio.timeout` cannot interrupt pure code and `re` has no timeout, so every text a guard reads
is first checked against its surface's cap. Over a cap fails closed (the surface's own tightening
response, wired in PR 6-8), never truncated; only a log field is truncated instead (G10).

Each cap is an existing bound of the platform, so a valid input never meets it:
- `message`: `MAX_QUESTION_CHARS` (2,000), which request validation already enforces (G1);
- `reply`: `REPLY_MAX` (1,500), an agent reply's own bound;
- `ticket_text`: `TICKET_SUMMARY_MAX` (500), `create_ticket`'s summary argument;
- `chunk`: 8,000, the upper bound of `ChunkingConfig.max_chars` (`ingestion/types.py`);
- `tool_field`: 500, the longest free-text field of a sandbox result (`tools.sandbox.NOTE_MAX`);
- `observation`: 65,536, the upper bound of `ObservationCaps.max_bytes` (`tools/executor.py`):
  a character is at least one byte, so a valid observation has at most that many characters;
- `page_text`: 20,000, every page's text summed (`extraction.text.MAX_CHARACTERS`).

The last four are copied, not imported (their modules pull in a database or PDF library);
`tests/guards/test_caps.py` pins each one to its source. `/v1/answer`'s answer text (PR 8) is
read against `reply`'s cap, as H3's contract reads an answer kind (`guards.answer`).
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vectapilot_api.agent.types import REPLY_MAX
from vectapilot_api.retrieval.types import MAX_QUESTION_CHARS
from vectapilot_api.tools.config import TICKET_SUMMARY_MAX


class Surface(StrEnum):
    MESSAGE = "message"
    REPLY = "reply"
    TICKET_TEXT = "ticket_text"
    CHUNK = "chunk"
    TOOL_FIELD = "tool_field"
    OBSERVATION = "observation"
    PAGE_TEXT = "page_text"


INPUT_CAPS: Final[Mapping[Surface, int]] = MappingProxyType(
    {
        Surface.MESSAGE: MAX_QUESTION_CHARS,
        Surface.REPLY: REPLY_MAX,
        Surface.TICKET_TEXT: TICKET_SUMMARY_MAX,
        Surface.CHUNK: 8_000,
        Surface.TOOL_FIELD: 500,
        Surface.OBSERVATION: 65_536,
        Surface.PAGE_TEXT: 20_000,
    }
)
"""Characters per surface (module docstring). A bound, never a measurement."""


class OverCap(ValueError):
    """A text longer than its surface's cap. The message names the surface and the cap only."""


def within_cap(surface: Surface, text: str) -> bool:
    return len(text) <= INPUT_CAPS[surface]


def require_within_cap(surface: Surface, text: str) -> None:
    """Raise `OverCap` unless `text` fits `surface`'s cap."""
    if not within_cap(surface, text):
        raise OverCap(f"{surface.value} text is longer than its cap of {INPUT_CAPS[surface]}")
