"""The masking path's runner (M7 PR 3; `plan.CasePath.MASKING`): category E in process, against
H2's `mask_pii` with every business's locales (`guards.locales.DEFAULT_LOCALE_IDS`).

**What it runs.** The case's filled input goes through `normalize.clean`, as a guarded route
will hand it over, then through the masker. A masker that raises is G3's hand-off: `handoff`
decided by `guard_error`, with the exception's class name only. While H2 is the stub that is
`NotImplementedError`, so `run_case` makes the case `pending:H2`.

**The control is the masked text itself** (`scoring.model_facing`: E has no model). The runner
renders the case's template with each placeholder replaced by the label the generator's type
should get (`pii.LABEL_OF`), then compares the two texts with every placeholder taken out. Equal
means every value was replaced exactly, span for span, whatever labels were chosen; the outcome
is then `masked:<labels>` from `Masked.counts`, and the scorer compares the labels. Different
means some value, or part of one, is still visible, or text that was not a value was masked: the
outcome is `leak` [proposal: an E case is a hard functional check, so a wrong span is never
softened into a label question, and the runner never prints which]. The texts and the values
stay in memory; the result holds codes only.
"""

import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Final

from vectapilot_api.guards.locales import Locale, PiiKind, locales
from vectapilot_api.guards.normalize import clean
from vectapilot_api.guards.pii import PLACEHOLDERS, Masked, mask_pii
from vectapilot_api.guards.types import Clean, DecidedBy
from vectapilot_api.redteam.cases import MASKED_PREFIX
from vectapilot_api.redteam.pii import LABEL_OF, PLACEHOLDER, PiiType
from vectapilot_api.redteam.scoring import CaseResult

if TYPE_CHECKING:
    # Annotations only: `harness` imports this module to register the runner.
    from vectapilot_api.redteam.harness import FilledCase, RunEnv, Runner

Masker = Callable[[Clean, Sequence[Locale]], Masked]

MASK_LOCALES: Final = locales()
_ANY_PLACEHOLDER: Final = re.compile("|".join(re.escape(p) for p in PLACEHOLDERS.values()))


def _kind(kind: PiiType) -> PiiKind:
    return PiiKind(LABEL_OF[kind].value.upper())


def expected_text(template: str) -> Clean:
    """The case's template with each `{{pii:...}}` placeholder as the mask it should get."""
    return clean(PLACEHOLDER.sub(lambda m: PLACEHOLDERS[_kind(PiiType(m.group(1)))], template))


def _without_placeholders(text: str) -> str:
    return _ANY_PLACEHOLDER.sub("", text)


def outcome_of(masked: Masked, expected: Clean) -> str:
    """`leak` unless every value was replaced exactly; else `masked:<labels>` (module
    docstring), or `allowed` when nothing was masked."""
    if _without_placeholders(masked.text) != _without_placeholders(expected):
        return "leak"
    labels = sorted(kind.value.lower() for kind in masked.counts)
    return MASKED_PREFIX + ",".join(labels) if labels else "allowed"


def masking_runner(masker: Masker) -> "Runner":
    """The runner over `masker` (H2's `mask_pii` when registered; a stand-in in tests)."""

    def run(filled: "FilledCase", env: "RunEnv") -> CaseResult:
        case_id = filled.case.id
        try:
            masked = masker(clean(filled.input), MASK_LOCALES)
        except Exception as error:  # G3: a raising guard is a hand-off, by class name only
            return CaseResult(
                case_id, "handoff", DecidedBy.GUARD_ERROR, error_type=type(error).__name__
            )
        return CaseResult(case_id, outcome_of(masked, expected_text(filled.case.input)))

    return run


run_masking: Final = masking_runner(mask_pii)
"""The registered runner (`harness.RUNNERS`)."""
