"""S11: customer text at rest is masked, and NULL whenever it cannot be (M8 PR 2; D6 (a)).

`handoffs.message_masked` is the first column where a customer's own words rest. The guards mask
a message only on the way to the service (M7 G1, step 7): a red-flag hit returns before that
step, and a hand-off the red-flag screen made is exactly the one a person must read. So the route
masks independently, through this one helper, on the same input the guard reads and in the same
way: `mask_pii(prepare(raw).clean, locales())` (`guards.screens.prepare`, H2's
`guards.pii.mask_pii`, the registered `guards.locales.locales()`).

**None, never raw and never truncated** (S11). The result is None, which the store writes as
NULL, when:
- the raw text is longer than `TEXT_MAX` (`guards.caps.OverCap`'s rule for a message: the route's
  own validation already refuses a longer one, so this only bounds a later caller's work);
- cleaning or masking raises anything at all (`OverCap`, `MaskingIncomplete`, a bug): fail closed
  into "no text", never into the unmasked text;
- the masked text is empty, or longer than `TEXT_MAX`: a placeholder can be longer than the span
  it replaces (`[PASSPORT]` for an eight-character passport number), and migration 0009 bounds
  the column at the message's own bound. A shortened text could cut a placeholder in half and
  read as something else, so it is dropped whole.

Pure: no I/O, no logging and nothing kept between calls; the caller records only whether text
was stored. `mask` is H2's `mask_pii`; tests pass stand-ins.
"""

from typing import Final

from vectapilot_api.guards.locales import locales
from vectapilot_api.guards.pii import mask_pii
from vectapilot_api.guards.screens import Masker, prepare
from vectapilot_api.retrieval.types import MAX_QUESTION_CHARS

TEXT_MAX: Final = MAX_QUESTION_CHARS
"""Migration 0009's `MESSAGE_MAX`: the longest text kept, before and after masking (a test
compares them)."""


def mask_at_rest(raw: str, *, mask: Masker = mask_pii) -> str | None:
    """`raw` cleaned and masked for storage, or None (module docstring)."""
    if len(raw) > TEXT_MAX:
        return None
    try:
        masked: str = mask(prepare(raw).clean, locales()).text
    except Exception:  # fail closed into no text; a cancel is not an Exception
        return None
    if not masked or len(masked) > TEXT_MAX:
        return None
    return masked
