"""The screens a guarded route runs, and what they share within one request (M7 PR 6; G1, G3, G5,
G7, G14).

**Pure screens.** `prepare` counts what a text hides (on the raw text, before `clean` removes it),
cleans it for the model and folds it for the screens (G5). `red_flag_category` and
`injection_findings` run the matcher (H1, or the test seam) and never trust it: every hit goes
through `selftest.verified_hits` first, so a hit for a term or a category the matcher was not given
is `UnexpectedHit` (G14), which the route turns into `guard_error` like any other failure (G3).
`screen_chunk` is the retrieved-chunk surface's screen (G7: a hit drops the chunk).

**Fail-closed exceptions that cross a service.** The seams inside a service (the chunk-screening
search, the ticket-text store, the tool-result executor) cannot hand off themselves: the service
would turn any exception into its own hand-off (`agent_error`, `proposal_not_stored`). They raise
one of these instead, and `AgentService` passes them through its existing `propagate`
(`GUARD_PROPAGATE`), so the route decides:
- `GuardFailed`: a guard raised (a pending stub's `NotImplementedError` included): `guard_error`;
- `GuardWithheld`: the ticket-text screen withheld a proposal: `guard_output`.

A seam's failure is also **recorded in the tally** (`seam_failed`) before it is raised, and the
route checks the tally after the service, whatever the service returned: the agent loop turns any
exception from its tools into its own `tool_failure` hand-off (M5 H1 rule 7), so a failure inside
the executor wrapper would otherwise never reach the route as `guard_error`.

Both carry codes and a class name only; their messages are fixed, so `log.py` (which writes a
chained exception's text) can never print a customer's words through them. They are raised
outside `except`.

**One tally per request** (`track_screens`, a context variable as `budget.track_budget`): the
route sets the business's guards and the clean message, the seams add their findings, the dropped
chunks, the withheld tool-result fields, the first failure and the clean text of every chunk they
let through (H3's grounding for an answer's citations). Outside a tracked request the seams fail
closed (`GuardFailed`): nothing guarded may run without the route that records it.

`screen_tool_field` is the tool-result surface's screen (G5, G6, G7): one free-text field, over
its cap or with an injection signal, becomes `WITHHELD_TEXT`; otherwise its clean text is masked.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final
from uuid import UUID

from vectapilot_api.agent.prompt import (
    MESSAGE_END,
    MESSAGE_START,
    RESULT_END,
    RESULT_START,
    SYSTEM_PROMPT,
)
from vectapilot_api.answer.prompt import SYSTEM_PROMPT as ANSWER_PROMPT
from vectapilot_api.guards.caps import Surface, within_cap
from vectapilot_api.guards.config import BusinessGuards
from vectapilot_api.guards.locales import Locale, locales
from vectapilot_api.guards.normalize import InvisibleCounts, clean, count_invisible, fold
from vectapilot_api.guards.output import (
    OUTPUT_FINDINGS,
    Matcher,
    OutputContext,
    OutputKind,
)
from vectapilot_api.guards.pii import Masked
from vectapilot_api.guards.selftest import verified_hits
from vectapilot_api.guards.types import (
    Check,
    Clean,
    DecidedBy,
    Finding,
    Folded,
    TermCategory,
    TermSet,
)

_ERROR_TYPE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
NO_GUARD_SCOPE: Final = "NoGuardScope"
"""The class name `GuardFailed` carries when a seam runs outside a request the route tracks."""
WITHHELD_TEXT: Final = "[withheld]"
"""What a tool-result field with an injection signal, or over its cap, reads as (G7)."""

Masker = Callable[[Clean, Sequence[Locale]], Masked]
"""H2's signature (`pii.mask_pii`). `create_app` always passes H2; unit tests may pass a stand-in
to reach the steps after masking."""

RED_FLAG_FINDINGS: Final[Mapping[TermCategory, Finding]] = MappingProxyType(
    {
        TermCategory.EMERGENCY: Finding.RED_FLAG_EMERGENCY,
        TermCategory.PROFESSIONAL_JUDGEMENT: Finding.RED_FLAG_PROFESSIONAL_JUDGEMENT,
    }
)
RED_FLAG_DECIDED_BY: Final[Mapping[TermCategory, DecidedBy]] = MappingProxyType(
    {
        TermCategory.EMERGENCY: DecidedBy.RED_FLAG_EMERGENCY,
        TermCategory.PROFESSIONAL_JUDGEMENT: DecidedBy.RED_FLAG_PROFESSIONAL_JUDGEMENT,
    }
)
OUTPUT_DECIDED_BY: Final[Mapping[Finding, DecidedBy]] = MappingProxyType(
    {
        Finding.OUTPUT_SUCCESS_CLAIM: DecidedBy.OUTPUT_SUCCESS_CLAIM,
        Finding.OUTPUT_MEDICAL_ADVICE: DecidedBy.OUTPUT_MEDICAL_ADVICE,
        Finding.OUTPUT_DIAGNOSIS: DecidedBy.OUTPUT_DIAGNOSIS,
        Finding.OUTPUT_ABUSE: DecidedBy.OUTPUT_ABUSE,
        Finding.OUTPUT_PII: DecidedBy.OUTPUT_PII,
        Finding.OUTPUT_BOUNDARY: DecidedBy.OUTPUT_BOUNDARY,
        Finding.OUTPUT_UNGROUNDED_DIGITS: DecidedBy.OUTPUT_UNGROUNDED_DIGITS,
    }
)
"""Each output finding's `decided_by`; also the closed set the route accepts from H3 (G14)."""


# ── Exceptions that cross a service ──────────────────────────────────────────────────────────


class GuardFailed(Exception):
    """A guard inside a service's seam raised: fail closed into `guard_error` (G3). Carries the
    original exception's class name only."""

    def __init__(self, error_type: str) -> None:
        if not _ERROR_TYPE.fullmatch(error_type):
            raise ValueError("error_type is a class name")
        super().__init__("a guard failed")
        self.error_type = error_type


class GuardWithheld(Exception):
    """The ticket-text screen withheld a proposal (G7): the rule that decided and the findings, as
    codes, and the id of the action it was stored as, already expired (`guards.store`), once it
    was stored."""

    def __init__(
        self,
        decided_by: DecidedBy,
        findings: Mapping[Finding, int],
        *,
        action_id: UUID | None = None,
    ) -> None:
        if not isinstance(decided_by, DecidedBy) or decided_by is DecidedBy.NONE:
            raise ValueError("a withheld proposal names the rule that withheld it")
        if not all(isinstance(code, Finding) and n >= 1 for code, n in findings.items()):
            raise ValueError("findings are codes with positive counts")
        if action_id is not None and not isinstance(action_id, UUID):
            raise ValueError("an action id is a UUID")
        super().__init__("a proposal was withheld")
        self.decided_by = decided_by
        self.findings: Mapping[Finding, int] = MappingProxyType(dict(findings))
        self.action_id = action_id

    def stored_as(self, action_id: UUID) -> "GuardWithheld":
        """The same withholding, naming the action it was stored as."""
        return GuardWithheld(self.decided_by, self.findings, action_id=action_id)


GUARD_PROPAGATE: Final[tuple[type[Exception], ...]] = (GuardFailed, GuardWithheld)
"""What the guarded `AgentService`s pass through to the route, with `limits.GuardRefused`."""


# ── One request's tally ──────────────────────────────────────────────────────────────────────


@dataclass
class ScreenTally:
    """What the screens of one request found (module docstring). Codes, counts and the clean
    texts of chunks the prompt held; never a term, a hit's text or the raw message."""

    business: BusinessGuards | None = None
    """The business's guards, set by the route before the service runs."""
    health: bool = True
    """Whether the business is a health type for the route (either source, or unresolved): set
    by the route; true until then (fail towards safety)."""
    message: Clean | None = None
    """The clean message (before masking), set by the route: H3's PII exemption."""
    findings: Counter[Finding] = field(default_factory=Counter)
    checks: list[Check] = field(default_factory=list)
    """The checks the seams ran (the chunk screen: `injection`; a ticket: `injection`, `output`)."""
    dropped_chunks: int = 0
    kept_chunks: dict[str, Clean] = field(default_factory=dict)
    """Chunk id (as `Citation.chunk_id`) -> its clean content, for every chunk let through."""
    withheld_fields: int = 0
    """Tool-result fields the executor wrapper replaced by `WITHHELD_TEXT` (G7)."""
    failed: str | None = None
    """The class name of the first guard that raised inside a seam (`seam_failed`)."""


_SCREENS: ContextVar[ScreenTally | None] = ContextVar("vectapilot_screen_tally", default=None)


@contextmanager
def track_screens() -> Iterator[ScreenTally]:
    """One fresh tally for everything screened inside this block (module docstring)."""
    tally = ScreenTally()
    token = _SCREENS.set(tally)
    try:
        yield tally
    finally:
        _SCREENS.reset(token)


def current_screens() -> ScreenTally | None:
    return _SCREENS.get()


def seam_failed(tally: ScreenTally | None, error_type: str) -> GuardFailed:
    """The `GuardFailed` a seam raises, after recording it in `tally` (the first one stays): the
    route then fails closed even if the service swallowed the exception (module docstring)."""
    failure = GuardFailed(error_type)
    if tally is not None and tally.failed is None:
        tally.failed = failure.error_type
    return failure


def noted(tally: ScreenTally, *checks: Check) -> None:
    """Record that a seam ran `checks` (each once, in order)."""
    for check in checks:
        if check not in tally.checks:
            tally.checks.append(check)


# ── Pure screens ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Prepared:
    """One text, as each consumer sees it (G5)."""

    clean: Clean
    folded: Folded
    invisible: InvisibleCounts
    """Counted on the raw text, before `clean` (`normalize.count_invisible`)."""


def prepare(text: str) -> Prepared:
    counts = count_invisible(text)
    cleaned = clean(text)
    return Prepared(cleaned, fold(cleaned), counts)


def red_flag_category(
    folded: Folded, terms: TermSet, matcher: Matcher, findings: Counter[Finding]
) -> TermCategory | None:
    """The most urgent red-flag category that hit (`emergency` over `professional_judgement`,
    G1), or None. Each hit is counted in `findings` by its category, never by its term."""
    hits = verified_hits(matcher(folded, terms), terms)
    for hit in hits:
        findings[RED_FLAG_FINDINGS[hit.category]] += 1
    categories = {hit.category for hit in hits}
    for category in (TermCategory.EMERGENCY, TermCategory.PROFESSIONAL_JUDGEMENT):
        if category in categories:
            return category
    return None


def injection_findings(prepared: Prepared, terms: TermSet, matcher: Matcher) -> Counter[Finding]:
    """The injection signals of one text (G5, G7): phrase hits and the invisible-character hit."""
    found: Counter[Finding] = Counter()
    hits = verified_hits(matcher(prepared.folded, terms), terms)
    if hits:
        found[Finding.INJECTION_PHRASE] += len(hits)
    if prepared.invisible.hit:
        found[Finding.INJECTION_INVISIBLE] += 1
    return found


def screen_chunk(
    content: str, terms: TermSet, matcher: Matcher
) -> tuple[Clean | None, Counter[Finding]]:
    """A retrieved chunk's screen (G7): its clean content, or None when it is dropped (over its cap,
    an injection signal, or nothing left once cleaned), with what was found."""
    if not within_cap(Surface.CHUNK, content):
        return None, Counter({Finding.OVER_CAP: 1})
    prepared = prepare(content)
    found = injection_findings(prepared, terms, matcher)
    if found or not prepared.clean:
        return None, found
    return prepared.clean, found


def screen_tool_field(
    text: str, terms: TermSet, matcher: Matcher, mask: Masker
) -> tuple[str, Counter[Finding]]:
    """One free-text field of a tool result (G5, G6, G7): `WITHHELD_TEXT` when it is over its cap
    or holds an injection signal, else its clean text masked (H2), with what was found. The
    screen runs on the unmasked clean text; a withheld field is never masked (it is gone)."""
    if not within_cap(Surface.TOOL_FIELD, text):
        return WITHHELD_TEXT, Counter({Finding.OVER_CAP: 1})
    prepared = prepare(text)
    found = injection_findings(prepared, terms, matcher)
    if found:
        return WITHHELD_TEXT, found
    return mask(prepared.clean, locales()).text, found


# ── Output checks (H3) around the route and the ticket store ─────────────────────────────────


class UnexpectedFinding(ValueError):
    """H3 returned something that is not one of its codes (G14): a `guard_error` at the route."""


SENTENCE_END: Final = re.compile(r"(?<=[.:])\s+")
"""Where a system-prompt line splits into sentences: after a full stop or a colon."""
MIN_BOUNDARY_WORDS: Final = 4
"""A prompt sentence shorter than this ("Rules:", a list number) is too common to flag."""


def boundary_lines() -> tuple[str, ...]:
    """The fixed prompt text whose echo H3 flags: each boundary marker's stem, and every sentence
    of at least `MIN_BOUNDARY_WORDS` words of each system-prompt line that holds no per-turn value
    (`agent/prompt.py`, read, never edited). Sentences, not whole lines: a model that leaks its
    rules quotes a sentence or two (i-005), never a whole line."""
    stems = [marker.split("{", 1)[0] for marker in (MESSAGE_START, MESSAGE_END, RESULT_START)]
    stems.append(RESULT_END.split("{", 1)[0])
    lines = [line.strip() for line in SYSTEM_PROMPT.splitlines() if "{" not in line]
    sentences = [part.strip() for line in lines for part in SENTENCE_END.split(line)]
    long_enough = [text for text in sentences if len(text.split()) >= MIN_BOUNDARY_WORDS]
    return tuple(dict.fromkeys(text for text in (*stems, *long_enough) if text))


BOUNDARY_LINES: Final = boundary_lines()


def answer_boundary_lines() -> tuple[str, ...]:
    """The answer prompt's fixed text whose echo H3 flags (G8), as `boundary_lines` does for the
    agent's: each marker's stem, and every sentence of at least `MIN_BOUNDARY_WORDS` words of each
    system-prompt line that holds no per-request value (`answer/prompt.py`, read, never edited).
    Here, not in `guards.answer`, because `guards.agent` needs it for `/v1/agent`'s answer kind
    and `guards.answer` imports `guards.agent` (`red_flag_priority`): a leaf both import."""
    stems = [f"{stem} " for stem in re.findall(r'"(<<<[A-Z ]+?) \{boundary\}', ANSWER_PROMPT)]
    lines = [line.strip() for line in ANSWER_PROMPT.splitlines() if "{" not in line]
    sentences = [part.strip() for line in lines for part in SENTENCE_END.split(line)]
    long_enough = [text for text in sentences if len(text.split()) >= MIN_BOUNDARY_WORDS]
    return tuple(dict.fromkeys(text for text in (*stems, *long_enough) if text))


ANSWER_BOUNDARY_LINES: Final = answer_boundary_lines()


def output_context(
    tally: ScreenTally, kind: OutputKind, matcher: Matcher, cited: tuple[Clean, ...] = ()
) -> OutputContext:
    """H3's context for the request `tally` tracks (G8). Raises `GuardFailed` outside one."""
    business, message = tally.business, tally.message
    if business is None or message is None:
        raise GuardFailed(NO_GUARD_SCOPE)
    return OutputContext(
        business_type=business.business_type,
        health=tally.health,
        message=message,
        kind=kind,
        cited=cited,
        guidance=business.guidance,
        terms=business.output,
        matcher=matcher,
        boundary_lines=BOUNDARY_LINES,
        locales=locales(),
    )


def output_withheld(findings: object) -> GuardWithheld | None:
    """What H3's result means (G8, G14): None for no finding, else the withholding decided by its
    first finding in `Finding`'s order, with each finding counted once. Anything but a tuple of
    H3's own codes is `UnexpectedFinding`."""
    if not isinstance(findings, tuple) or not all(
        isinstance(f, Finding) and f in OUTPUT_FINDINGS for f in findings
    ):
        raise UnexpectedFinding("the output check returned something that is not its finding")
    if not findings:
        return None
    order = list(Finding)
    first = min(findings, key=order.index)
    return GuardWithheld(OUTPUT_DECIDED_BY[first], dict.fromkeys(findings, 1))
