"""M7 PR 1: the input cap per surface (G3) [proposal]. Each cap is pinned to its source, so a
source that grows makes this fail instead of making a valid input fail closed."""

from typing import Any

import pytest
from pydantic import BaseModel

from vectapilot_api.agent.types import REPLY_MAX
from vectapilot_api.extraction.text import MAX_CHARACTERS
from vectapilot_api.guards.caps import INPUT_CAPS, OverCap, Surface, require_within_cap, within_cap
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.retrieval.types import MAX_QUESTION_CHARS
from vectapilot_api.tools.config import TICKET_SUMMARY_MAX
from vectapilot_api.tools.executor import ObservationCaps
from vectapilot_api.tools.sandbox import NOTE_MAX


def upper_bound(model: type[BaseModel], name: str) -> Any:
    (le,) = [m.le for m in model.model_fields[name].metadata if getattr(m, "le", None) is not None]
    return le


def test_every_surface_has_a_cap() -> None:
    assert set(INPUT_CAPS) == set(Surface)
    assert all(isinstance(cap, int) and cap > 0 for cap in INPUT_CAPS.values())


def test_each_cap_is_its_source_s_bound() -> None:
    assert INPUT_CAPS[Surface.MESSAGE] == MAX_QUESTION_CHARS == 2000
    assert INPUT_CAPS[Surface.REPLY] == REPLY_MAX == 1500
    assert INPUT_CAPS[Surface.TICKET_TEXT] == TICKET_SUMMARY_MAX == 500
    assert INPUT_CAPS[Surface.CHUNK] == upper_bound(ChunkingConfig, "max_chars")
    assert INPUT_CAPS[Surface.TOOL_FIELD] == NOTE_MAX
    assert INPUT_CAPS[Surface.OBSERVATION] == upper_bound(ObservationCaps, "max_bytes")
    assert INPUT_CAPS[Surface.PAGE_TEXT] == MAX_CHARACTERS


def test_the_caps_cannot_be_changed_at_runtime() -> None:
    with pytest.raises(TypeError):
        INPUT_CAPS[Surface.MESSAGE] = 10**6  # type: ignore[index]


@pytest.mark.parametrize("surface", list(Surface))
def test_a_text_at_its_cap_fits_and_one_more_character_does_not(surface: Surface) -> None:
    cap = INPUT_CAPS[surface]
    assert within_cap(surface, "x" * cap)
    require_within_cap(surface, "x" * cap)
    assert not within_cap(surface, "x" * (cap + 1))
    with pytest.raises(OverCap):
        require_within_cap(surface, "x" * (cap + 1))


def test_the_over_cap_error_names_the_surface_and_cap_never_the_text() -> None:
    text = "SECRET-CANARY " * 200
    with pytest.raises(OverCap) as raised:
        require_within_cap(Surface.MESSAGE, text)
    assert str(raised.value) == "message text is longer than its cap of 2000"
    assert isinstance(raised.value, ValueError)


def test_a_cap_counts_characters_not_bytes() -> None:
    hindi = "द" * INPUT_CAPS[Surface.MESSAGE]  # three UTF-8 bytes each
    assert within_cap(Surface.MESSAGE, hindi)
