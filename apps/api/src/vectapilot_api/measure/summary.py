"""The summary of one gateway measurement run, and its Markdown and JSON renderings (PR 8).

Pure: `summarise_run` takes the run's facts (`RunInfo`), the dataset's cases and the calls paired
with their `llm_call` records (`records.join_records`), and returns a typed `RunSummary`. No I/O,
no clock, no model call. Every latency figure comes from H3's `latency_summary`; money is Decimal.

The rules, from docs/plans/m1-pr-checks.md (PR 8), ADR 0007 (Measurement) and the step 8 review:

- **Buckets.** Every call is in exactly one: *distinct* (a case's first run, `repeat` 0), *repeat*
  (`repeat` >= 1) or *cache hit*. The per-alias, per-language and per-business figures use the
  distinct calls only. Repeats and cache hits are reported on their own and never count as
  distinct prompts. Nothing is dropped: a timed-out call is a distinct call like any other. Two
  cases that send the same prompt on one alias are refused (`check_distinct_prompts`): they would
  count twice toward n.
- **Per alias.** n distinct prompts; latency median, min and max, and the inclusive p95 only when
  n >= 20 (`P95_MIN_DISTINCT_PROMPTS`); timeout rate = calls whose outcome is `deadline_exceeded`
  or whose `attempt_timeouts` > 0, as k/n, with `deadline_exceeded` also on its own; refusals and
  truncations k/n; median and max `output_tokens` per request, beside the alias's cap; first-try
  validity (Maya's variant, below); the forced-repair case on its own; cost; latency against
  `output_tokens`. The rules fixed before the live run are printed beside their figures.
- **Repeats** get the same k/n figures as an alias (timeouts, refusals, truncations, first-try
  validity, the forced repair), kept apart from the distinct ones, so a failure in a repeat is
  never hidden and never counts as a distinct prompt.
- **First-try validity** (decided 2026-10-15, Maya's variant). Over the alias's non-forced
  structured cases (from the case metadata: a null `first_try_valid` alone cannot tell structured
  from plain): n = those whose first answer was validated (`first_try_valid` true or false), k =
  those with true. Cases with null are not in n; they are counted beside k/n by outcome.
- **Per language and per business.** One row per (language or business, alias): an alias is one
  model, so no row pools two models. n, median, min, max (no p95, by type), input tokens per
  prompt character, cost. Tiny n: descriptive only.
- **Cost.** Decimal sums. "k/n cost-incomplete"; with any incomplete call the total is a lower
  bound (`CostSummary.is_lower_bound`).
- **Partial run** (decided by shesha, 2026-10-15). Partial only when the run did not make every
  planned call: the CLI stopped it early (`RunInfo.stopped`: the cost cap, `--max-calls`, an
  account error such as `spend_limit`, `billing` or `auth_failed`, an `invalid_request`, a `us`
  inference geo, a call billed beyond its worst-case bound, an unexpected error, an interrupt) or
  fewer calls were made than planned. A
  cost-incomplete call does not make a run partial: it is counted as "k/n cost-incomplete" and the
  cost it is part of is labelled a lower bound (it still keeps its reservation against the cap).
- **The note.** Only a complete live run feeds the "what I measured" note, and only when nothing in
  `not_for_note` disqualifies it: never a partial run, a dry run, a run with unreviewed prompts,
  uncommitted changes or a dataset that is not as committed (`RunInfo.dataset_committed`), or
  (decided by shesha, 2026-10-15) a run that a stop ended after its last planned call. A run that
  fails the CLI's record-count check gets no summary at all (`records.join_records` raises), so
  it never feeds the note either.
- **Labels and config.** "synthetic prompts, single home network" on a live run; the gateway
  limits the run used (`RunLimits`, required: a figure is never shown without them); per-call
  latency is never compared with business-context §6's message-to-reply target; no target is
  claimed as met.
"""

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal, Self, TypedDict

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from vectapilot_api.gateway.types import (
    CallOutcome,
    CallRecord,
    Count,
    OpaqueId,
    Seconds,
    Slug,
    Usd,
)
from vectapilot_api.measure.dataset import DatasetVersion, SmokeCase
from vectapilot_api.measure.latency import LatencySummary, latency_summary
from vectapilot_api.measure.records import MeasuredCall

P95_MIN_DISTINCT_PROMPTS: Final = 20
"""ADR 0007: a p95 only when an alias has at least this many distinct prompts."""

MEAN_USD_QUANTUM: Final = Decimal("1E-10")
"""The mean cost per call is rounded (half to even) to this; every sum stays exact."""

SYNTHETIC_LABEL: Final = "synthetic prompts, single home network"
DRY_RUN_LABEL: Final = (
    "dry run: fake provider, synthetic prompts; these numbers are not measurements"
)
LATENCY_LABEL: Final = (
    "Latency is per gateway call (`latency_ms`: the whole call, retries and repair included), not"
    " message-to-reply latency; it is not compared with any target in business-context §6."
)
NO_TARGET_LABEL: Final = "No target is claimed as met."

TIMEOUT_RULE: Final = "counts deadline_exceeded or any attempt timeout (decided 2026-10-14)"
TRUNCATION_RULE: Final = (
    "rule fixed before the live run: any truncation ⇒ raise this alias's `max_output_tokens`"
    " (new `catalog_version`) and re-run"
)
FIRST_TRY_RULE: Final = (
    "rule fixed before the live run: any non-forced first-try failure ⇒ revisit native"
    " `output_config.format` in M3"
)

RunStop = Literal["cost_cap", "max_calls", "error", "interrupted"]
"""Why the CLI stopped a run: before a call (`cost_cap`, `max_calls`) or after one (`error`, which
covers account errors and a call billed beyond its bound, and `interrupted`)."""

STOP_DETAIL_MAX_CHARS: Final = 500
"""A stop detail is one short line in the CLI's own words; anything longer is refused."""

StopDetail = Annotated[
    str, Field(min_length=1, max_length=STOP_DETAIL_MAX_CHARS, pattern=r"^[^\r\n]*$")
]

Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$")]
OutputCap = Annotated[int, Field(gt=0, strict=True)]


class SummaryInputError(ValueError):
    """The run's inputs contradict each other; no summary is built from them."""


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


# ── The run's facts (from the CLI) ───────────────────────────────────────────────────────────


class RunLimits(_Closed):
    """The gateway limits the run used: latency, the timeout rate and the forced-repair result all
    depend on them, so the summary names them (team rule 3: numbers come with their config).

    From Settings (which the environment can override) and the catalog.
    """

    max_retries: Count
    """`llm_max_retries`: retries per provider request."""
    max_repairs: int = Field(ge=0, le=1, strict=True)
    """`llm_max_repairs`: structured-output repairs per call."""
    attempt_timeout_s: Seconds
    """`llm_attempt_timeout_seconds`: each attempt's limit."""
    deadline_s: Seconds
    """`llm_deadline_seconds`: the whole call's limit, every attempt, wait and repair included."""
    backoff_base_s: Seconds
    """`llm_backoff_base_seconds`: the first wait before a retry."""
    backoff_cap_s: Seconds
    """`llm_backoff_cap_seconds`: the longest wait before a retry."""
    max_output_tokens: dict[Slug, OutputCap] = Field(min_length=1)
    """Each alias's `max_output_tokens` in the catalog the run used: the per-request cap that the
    output-token figures and the truncation rule are read against."""


class RunInfo(_Closed):
    """What the CLI knows about the run, besides the calls: provenance, config, plan and end."""

    dataset: str = Field(min_length=1)
    dataset_version: DatasetVersion
    dataset_review: Literal["pending", "done"]
    """The manifest's native-speaker review status of the hi/te and code-mixed prompts."""
    dataset_committed: bool
    """True when the dataset files the run read are, byte for byte, the files at the same paths in
    `commit`. False for a dataset outside the repository, untracked, git-ignored or changed: the
    commit and the manifest's name and version then do not name the prompts that ran."""
    commit: Commit
    uncommitted_changes: bool
    """True when the working tree differed from `commit`, which then does not name the code."""
    provider: Literal["fake", "anthropic"]
    started_at: AwareDatetime
    catalog_version: OpaqueId
    alias_models: dict[Slug, OpaqueId] = Field(min_length=1)
    """Each alias's model id in the catalog the run used."""
    limits: RunLimits
    """The gateway limits the run used (the CLI's `run_limits`, from the Settings and catalog the
    gateway read). Required: latency, timeouts and the forced repair cannot be read without them."""
    planned_calls: int = Field(ge=0, strict=True)
    """Every call the full plan needs: each case once, plus any repeats."""
    max_usd: Usd | None = None
    """The `--max-usd` cap of a live run; None for a dry run."""
    stopped: RunStop | None = None
    """Why the CLI stopped the run; None when it made every planned call without a stop."""
    stop_detail: StopDetail | None = None
    """What stopped the run, in the CLI's words (its "Run stopped" line), for the run table. Only
    case ids, dollar amounts, outcome names and exception type names: never prompt or completion
    text. One line, at most `STOP_DETAIL_MAX_CHARS` characters, and only with `stopped`."""

    @model_validator(mode="after")
    def _limits_name_every_alias(self) -> Self:
        if set(self.limits.max_output_tokens) != set(self.alias_models):
            raise ValueError(
                "limits.max_output_tokens must name exactly the aliases of alias_models"
            )
        return self

    @model_validator(mode="after")
    def _stop_detail_needs_a_stop(self) -> Self:
        if self.stop_detail is not None and self.stopped is None:
            raise ValueError("stop_detail needs stopped: a detail of a run that was not stopped")
        return self


# ── The summary's parts ──────────────────────────────────────────────────────────────────────


class Fraction(_Closed):
    """k out of n, always shown as counts ("k/n"), never only as a percentage."""

    k: int = Field(ge=0, strict=True)
    n: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _k_at_most_n(self) -> Self:
        if self.k > self.n:
            raise ValueError("k must be <= n")
        return self

    def __str__(self) -> str:
        return f"{self.k}/{self.n}"


class LatencyRange(_Closed):
    """n, min, median and max in milliseconds, from H3. No p95 field: per language, per business
    and for repeats a p95 is never reported (ADR 0007), so it cannot be."""

    n: int
    min_ms: float
    median_ms: float
    max_ms: float


class CostSummary(_Closed):
    """Cost of a set of calls, in US dollars."""

    calls: int
    total_usd: Usd
    """Exact sum of `cost_usd`; a lower bound when any call is cost-incomplete."""
    mean_usd_per_call: Usd
    """`total_usd / calls`, rounded to `MEAN_USD_QUANTUM`; 0 when there are no calls."""
    max_usd_per_call: Usd
    cost_incomplete: Fraction
    is_lower_bound: bool


class OutputTokens(_Closed):
    """`output_tokens` per request: over the calls that got an answer and made no repair.

    A repaired call's `output_tokens` adds two answers (the first and the repair's), so it is left
    out (counted in `repaired_left_out`): `max` stays comparable with the per-request cap.
    """

    calls: int
    median: float
    max: int
    repaired_left_out: int


class TokensPerChar(_Closed):
    """Prompt tokens per prompt character, over plain calls that got an answer, on one alias.

    Prompt tokens = input + cache read + cache write tokens (the whole prompt). Characters are
    Unicode code points of the case's prompt. Structured calls are left out: their input includes
    the gateway's schema instruction. The prompt tokens may include a fixed per-request overhead.
    One alias is one model, so one tokenizer: never pooled across models.
    """

    calls: int
    prompt_tokens: int
    prompt_chars: int
    per_char: float


class FirstTryValidity(_Closed):
    """Maya's variant (decided 2026-10-15): k/n over the non-forced structured cases whose first
    answer was validated; the cases whose first answer never was (null) are counted beside it."""

    valid: Fraction
    not_validated: dict[str, int]
    """Outcome -> number of non-forced structured cases with a null `first_try_valid`."""


class ForcedRepairResult(StrEnum):
    REPAIR_WORKED = "repair worked"
    REPAIR_FAILED = "repair failed"
    """The repaired answer was invalid too (`validation_failed`)."""
    REPAIR_UNFINISHED = "repair did not finish"
    """The first answer was invalid and a repair was sent, but it ended otherwise (a deadline,
    a refusal, a truncation, an error)."""
    NOT_REPAIRED = "first answer invalid, no repair made"
    """Repairs are turned off (`max_repairs` 0)."""
    FIRST_ANSWER_VALID = "not exercised: the first answer was valid"
    NO_FIRST_ANSWER = "not exercised: no first answer was validated"


class ForcedRepairReport(_Closed):
    case_id: str
    outcome: CallOutcome
    repairs: int
    result: ForcedRepairResult


class LatencyPoint(_Closed):
    """One distinct call's latency against its output tokens."""

    case_id: str
    output_tokens: int
    latency_ms: float
    outcome: CallOutcome


class AliasSummary(_Closed):
    alias: str
    model_requested: str | None
    models_answered: tuple[str, ...]
    distinct_prompts: int
    latency: LatencySummary
    """From H3 with `min_n=P95_MIN_DISTINCT_PROMPTS`: `p95_ms` is None when n < 20."""
    deadline_cut: int
    """Calls in `latency` that the deadline cut off: their real latency was longer."""
    outcomes: dict[str, int]
    timeout_rate: Fraction
    deadline_exceeded: Fraction
    refusals: Fraction
    truncated: Fraction
    first_try: FirstTryValidity | None
    """None when the alias ran no non-forced structured case."""
    forced_repairs: tuple[ForcedRepairReport, ...]
    cost: CostSummary
    max_output_tokens: int
    """The alias's per-request cap (`RunInfo.limits`)."""
    output_tokens: OutputTokens | None
    latency_vs_output_tokens: tuple[LatencyPoint, ...]
    """Sorted by output tokens, then latency."""


class GroupSummary(_Closed):
    """One language or one business on one alias, so on one model: never a p95."""

    key: str
    alias: str
    n: int
    latency: LatencyRange
    tokens_per_char: TokensPerChar | None
    cost: CostSummary


class RepeatSummary(_Closed):
    """Repeat calls of one alias: the same prompts again, so never distinct prompts, never a p95.

    The same k/n figures as `AliasSummary`, so a failure in a repeat is never hidden.
    """

    alias: str
    calls: int
    cases: int
    latency: LatencyRange
    deadline_cut: int
    outcomes: dict[str, int]
    timeout_rate: Fraction
    deadline_exceeded: Fraction
    refusals: Fraction
    truncated: Fraction
    first_try: FirstTryValidity | None
    forced_repairs: tuple[ForcedRepairReport, ...]
    cost: CostSummary


class CacheHit(_Closed):
    case_id: str
    repeat: int
    alias: str


class RunSummary(_Closed):
    summary_version: Literal[1] = 1
    labels: tuple[str, ...]
    run: RunInfo
    calls_made: int
    distinct_calls: int
    repeat_calls: int
    cache_hit_calls: int
    partial: bool
    """True only when the run did not make every planned call; cost-incomplete calls never make
    it partial (they make its cost a lower bound)."""
    partial_reasons: tuple[str, ...]
    feeds_note: bool
    """True only for a complete live run that nothing else disqualifies (`not_for_note`)."""
    not_for_note: tuple[str, ...]
    models_answered: tuple[str, ...]
    run_cost: CostSummary
    """Every call made: distinct, repeats and cache hits."""
    per_alias: tuple[AliasSummary, ...]
    per_language: tuple[GroupSummary, ...]
    """One row per (language group, alias)."""
    per_business: tuple[GroupSummary, ...]
    """One row per (business, alias)."""
    repeats: tuple[RepeatSummary, ...]
    cache_hits: tuple[CacheHit, ...]
    cache_hit_rate: Fraction
    """Cache hits over every call made. The cache is off in a live run, so expected 0."""


# ── Summarising ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Call:
    case: SmokeCase
    repeat: int
    record: CallRecord


def summarise_run(
    run: RunInfo, cases: Sequence[SmokeCase], calls: Sequence[MeasuredCall]
) -> RunSummary:
    """Summarise one run. Raises `SummaryInputError` when the inputs contradict each other."""
    run = _checked(run)
    joined = _join(run, cases, calls)
    distinct = [c for c in joined if c.repeat == 0 and not c.record.cache_hit]
    repeats = [c for c in joined if c.repeat > 0 and not c.record.cache_hit]
    hits = [c for c in joined if c.record.cache_hit]
    partial_reasons = _partial_reasons(run, len(joined))
    not_for_note = _not_for_note(run, partial_reasons)
    return RunSummary(
        labels=(
            SYNTHETIC_LABEL if run.provider != "fake" else DRY_RUN_LABEL,
            LATENCY_LABEL,
            NO_TARGET_LABEL,
        ),
        run=run,
        calls_made=len(joined),
        distinct_calls=len(distinct),
        repeat_calls=len(repeats),
        cache_hit_calls=len(hits),
        partial=bool(partial_reasons),
        partial_reasons=partial_reasons,
        feeds_note=not not_for_note,
        not_for_note=not_for_note,
        models_answered=_models_answered(joined),
        run_cost=_cost(joined),
        per_alias=tuple(
            _alias_summary(run, alias, group)
            for alias, group in _grouped(distinct, lambda c: c.case.alias)
        ),
        per_language=_per_alias_groups(distinct, lambda c: c.case.language_group),
        per_business=_per_alias_groups(distinct, lambda c: c.case.business_id),
        repeats=tuple(
            _repeat_summary(alias, group)
            for alias, group in _grouped(repeats, lambda c: c.case.alias)
        ),
        cache_hits=tuple(
            CacheHit(case_id=c.case.id, repeat=c.repeat, alias=c.case.alias) for c in hits
        ),
        cache_hit_rate=Fraction(k=len(hits), n=len(joined)),
    )


def check_distinct_prompts(cases: Iterable[SmokeCase]) -> None:
    """Refuse two cases that send the same prompt on one alias: they would count twice toward n.

    The same prompt on two aliases is allowed (each alias has its own n). `summarise_run` calls
    this; the CLI can call it too, before any call is made. Raises `SummaryInputError` naming the
    case ids and the alias, never the prompt.
    """
    first_case: dict[tuple[str, str], str] = {}
    for case in cases:
        first = first_case.setdefault((case.alias, case.prompt), case.id)
        if first != case.id:
            raise SummaryInputError(
                f"cases {first} and {case.id} send the same prompt on alias {case.alias}; a"
                " prompt counts once toward n distinct prompts"
            )


def _checked(run: RunInfo) -> RunInfo:
    """The run's facts, validated again: `model_copy(update=...)`, which the CLI uses to add how
    the run ended, skips validation. The error names the broken rule, never a value."""
    try:
        return RunInfo.model_validate(run.model_dump())
    except ValidationError as error:
        raise SummaryInputError(f"the run's facts are invalid: {error}") from error


def _join(run: RunInfo, cases: Sequence[SmokeCase], calls: Sequence[MeasuredCall]) -> list[_Call]:
    """Pair every call with its case, refusing anything that contradicts the case or the run."""
    by_id = {case.id: case for case in cases}
    if len(by_id) != len(cases):
        raise SummaryInputError("case ids must be unique")
    check_distinct_prompts(cases)
    if len(calls) > run.planned_calls:
        raise SummaryInputError("more calls were made than planned")
    seen: set[tuple[str, int]] = set()
    joined: list[_Call] = []
    for call in calls:
        case = by_id.get(call.case_id)
        if case is None:
            raise SummaryInputError(f"call for unknown case {call.case_id}")
        if (call.case_id, call.repeat) in seen:
            raise SummaryInputError(f"case {call.case_id} repeat {call.repeat} appears twice")
        seen.add((call.case_id, call.repeat))
        problem = _record_problem(run, case, call.record)
        if problem is not None:
            raise SummaryInputError(f"case {case.id}: {problem}")
        joined.append(_Call(case, call.repeat, call.record))
    return joined


def _record_problem(run: RunInfo, case: SmokeCase, record: CallRecord) -> str | None:
    if record.business_id != case.business_id:
        return "its record belongs to another business"
    if record.alias != case.alias:
        return "its record names another alias"
    if record.catalog_version != run.catalog_version:
        return "its record names another catalog version"
    if case.alias not in run.alias_models:
        # Its record is the gateway's unknown-alias rejection (no `model_requested`), and the run
        # has no model and no cap for it. The CLI's `check_dataset` refuses such a case earlier.
        return "its alias is not in the run's catalog"
    if (
        record.model_requested is not None
        and record.model_requested != run.alias_models[case.alias]
    ):
        return "its record's model_requested is not the catalog's model for the alias"
    if case.kind == "plain" and record.first_try_valid is not None:
        return "a plain case's record has a first_try_valid"
    return None


def _partial_reasons(run: RunInfo, calls_made: int) -> tuple[str, ...]:
    """Partial only when the run did not make every planned call (decided by shesha, 2026-10-15).

    Cost-incomplete calls play no part: `CostSummary` counts them and marks the lower bound.
    """
    if calls_made >= run.planned_calls:  # `_join` refuses more calls than planned
        return ()
    stop = () if run.stopped is None else (f"stopped early: {run.stopped}",)
    return (*stop, f"{calls_made}/{run.planned_calls} planned calls made")


def _not_for_note(run: RunInfo, partial_reasons: tuple[str, ...]) -> tuple[str, ...]:
    reasons = [f"partial run: {reason}" for reason in partial_reasons]
    if run.stopped is not None and not partial_reasons:
        # Every planned call was made, but the last one stopped the run (an account error, a bound
        # miss, an error, an interrupt): complete, yet not for the note (decided 2026-10-15).
        reasons.append(f"stopped by its last planned call: {run.stopped}")
    if run.provider == "fake":
        reasons.append("dry run: fake provider, not a measurement")
    if run.dataset_review != "done":
        reasons.append("the hi/te and code-mixed prompts are not reviewed yet")
    if run.uncommitted_changes:
        reasons.append("uncommitted changes: the commit does not name the code that ran")
    if not run.dataset_committed:
        reasons.append("dataset not as committed: the commit does not name the prompts that ran")
    return tuple(reasons)


def _grouped[K: (str, tuple[str, str])](
    calls: Iterable[_Call], key: Callable[[_Call], K]
) -> list[tuple[K, list[_Call]]]:
    """Calls grouped by `key`, groups sorted by key, calls in their original order."""
    groups: defaultdict[K, list[_Call]] = defaultdict(list)
    for call in calls:
        groups[key(call)].append(call)
    return sorted(groups.items(), key=lambda item: item[0])


def _models_answered(calls: Iterable[_Call]) -> tuple[str, ...]:
    return tuple(sorted({c.record.model_answered for c in calls if c.record.model_answered}))


def _outcomes(calls: Iterable[_Call]) -> dict[str, int]:
    return dict(sorted(Counter(c.record.outcome.value for c in calls).items()))


def _count(calls: Sequence[_Call], test: Callable[[CallRecord], bool]) -> Fraction:
    return Fraction(k=sum(test(c.record) for c in calls), n=len(calls))


def _timed_out(record: CallRecord) -> bool:
    """The timeout rule (decided 2026-10-14): the deadline fired, or any attempt timed out."""
    return record.outcome == CallOutcome.DEADLINE_EXCEEDED or record.attempt_timeouts > 0


def _latencies(calls: Iterable[_Call]) -> list[float]:
    return [c.record.latency_ms for c in calls]


def _range(calls: Sequence[_Call]) -> LatencyRange:
    full = latency_summary(_latencies(calls))  # H3; its p95 (if any) is deliberately not kept
    return LatencyRange(n=full.n, min_ms=full.min_ms, median_ms=full.median_ms, max_ms=full.max_ms)


def _cost(calls: Sequence[_Call]) -> CostSummary:
    costs = [c.record.cost_usd for c in calls]
    total = sum(costs, Decimal(0))
    incomplete = sum(not c.record.cost_complete for c in calls)
    mean = (total / len(costs)).quantize(MEAN_USD_QUANTUM, ROUND_HALF_EVEN) if costs else total
    return CostSummary(
        calls=len(costs),
        total_usd=total,
        mean_usd_per_call=mean,
        max_usd_per_call=max(costs, default=Decimal(0)),
        cost_incomplete=Fraction(k=incomplete, n=len(costs)),
        is_lower_bound=incomplete > 0,
    )


def _output_tokens(calls: Sequence[_Call]) -> OutputTokens | None:
    answered = [c.record for c in calls if c.record.model_answered is not None]
    single = [record.output_tokens for record in answered if record.repairs == 0]
    if not single:
        return None
    return OutputTokens(
        calls=len(single),
        median=float(statistics.median(single)),
        max=max(single),
        repaired_left_out=len(answered) - len(single),
    )


def _prompt_tokens(record: CallRecord) -> int:
    return (
        record.input_tokens
        + record.cache_read_tokens
        + record.cache_write_5m_tokens
        + record.cache_write_1h_tokens
    )


def _tokens_per_char(calls: Sequence[_Call]) -> TokensPerChar | None:
    used = [c for c in calls if c.case.kind == "plain" and _prompt_tokens(c.record) > 0]
    if not used:
        return None
    tokens = sum(_prompt_tokens(c.record) for c in used)
    chars = sum(len(c.case.prompt) for c in used)
    return TokensPerChar(
        calls=len(used), prompt_tokens=tokens, prompt_chars=chars, per_char=tokens / chars
    )


def _first_try(calls: Sequence[_Call]) -> FirstTryValidity | None:
    cases = [c for c in calls if c.case.kind == "structured" and not c.case.forced_repair]
    if not cases:
        return None
    validated = [c for c in cases if c.record.first_try_valid is not None]
    unvalidated = [c for c in cases if c.record.first_try_valid is None]
    return FirstTryValidity(
        valid=Fraction(
            k=sum(c.record.first_try_valid is True for c in validated), n=len(validated)
        ),
        not_validated=_outcomes(unvalidated),
    )


def _forced_result(record: CallRecord) -> ForcedRepairResult:
    if record.first_try_valid is None:
        return ForcedRepairResult.NO_FIRST_ANSWER
    if record.first_try_valid:
        return ForcedRepairResult.FIRST_ANSWER_VALID
    if record.repairs == 0:
        return ForcedRepairResult.NOT_REPAIRED
    if record.outcome == CallOutcome.OK:
        return ForcedRepairResult.REPAIR_WORKED
    if record.outcome == CallOutcome.VALIDATION_FAILED:
        return ForcedRepairResult.REPAIR_FAILED
    return ForcedRepairResult.REPAIR_UNFINISHED


def _forced_repairs(calls: Sequence[_Call]) -> tuple[ForcedRepairReport, ...]:
    return tuple(
        ForcedRepairReport(
            case_id=c.case.id,
            outcome=c.record.outcome,
            repairs=c.record.repairs,
            result=_forced_result(c.record),
        )
        for c in calls
        if c.case.forced_repair
    )


def _points(calls: Sequence[_Call]) -> tuple[LatencyPoint, ...]:
    points = (
        LatencyPoint(
            case_id=c.case.id,
            output_tokens=c.record.output_tokens,
            latency_ms=c.record.latency_ms,
            outcome=c.record.outcome,
        )
        for c in calls
    )
    return tuple(sorted(points, key=lambda p: (p.output_tokens, p.latency_ms, p.case_id)))


class _Counts(TypedDict):
    """The figures an alias's distinct calls and its repeats share, computed one way for both."""

    deadline_cut: int
    outcomes: dict[str, int]
    timeout_rate: Fraction
    deadline_exceeded: Fraction
    refusals: Fraction
    truncated: Fraction
    first_try: FirstTryValidity | None
    forced_repairs: tuple[ForcedRepairReport, ...]
    cost: CostSummary


def _counts(calls: Sequence[_Call]) -> _Counts:
    return _Counts(
        deadline_cut=sum(c.record.outcome == CallOutcome.DEADLINE_EXCEEDED for c in calls),
        outcomes=_outcomes(calls),
        timeout_rate=_count(calls, _timed_out),
        deadline_exceeded=_count(calls, lambda r: r.outcome == CallOutcome.DEADLINE_EXCEEDED),
        refusals=_count(calls, lambda r: r.outcome == CallOutcome.REFUSAL),
        truncated=_count(calls, lambda r: r.outcome == CallOutcome.TRUNCATED),
        first_try=_first_try(calls),
        forced_repairs=_forced_repairs(calls),
        cost=_cost(calls),
    )


def _alias_summary(run: RunInfo, alias: str, calls: Sequence[_Call]) -> AliasSummary:
    requested = {c.record.model_requested for c in calls}
    return AliasSummary(
        alias=alias,
        model_requested=next(iter(requested)) if len(requested) == 1 else None,
        models_answered=_models_answered(calls),
        distinct_prompts=len(calls),
        latency=latency_summary(_latencies(calls), min_n=P95_MIN_DISTINCT_PROMPTS),
        max_output_tokens=run.limits.max_output_tokens[alias],  # `_join`: alias in the catalog
        output_tokens=_output_tokens(calls),
        latency_vs_output_tokens=_points(calls),
        **_counts(calls),
    )


def _per_alias_groups(
    calls: Sequence[_Call], key: Callable[[_Call], str]
) -> tuple[GroupSummary, ...]:
    """One row per (key, alias): an alias is one model, so no row pools two models."""
    return tuple(
        GroupSummary(
            key=name,
            alias=alias,
            n=len(group),
            latency=_range(group),
            tokens_per_char=_tokens_per_char(group),
            cost=_cost(group),
        )
        for (name, alias), group in _grouped(calls, lambda c: (key(c), c.case.alias))
    )


def _repeat_summary(alias: str, calls: Sequence[_Call]) -> RepeatSummary:
    return RepeatSummary(
        alias=alias,
        calls=len(calls),
        cases=len({c.case.id for c in calls}),
        latency=_range(calls),
        **_counts(calls),
    )


# ── Rendering ────────────────────────────────────────────────────────────────────────────────


def render_json(summary: RunSummary) -> str:
    """The summary as indented JSON: money as fixed-point strings, exact."""
    return summary.model_dump_json(indent=2) + "\n"


def render_markdown(summary: RunSummary) -> str:
    """The summary as Markdown, for the committed report and the "what I measured" note."""
    sections = [
        _md_header(summary),
        _md_run(summary),
        "## Per alias\n",
        *(_md_alias(alias) for alias in summary.per_alias),
        _md_groups("Per language", "language", "language", summary.per_language),
        _md_groups("Per business", "business", "business", summary.per_business),
        _md_repeats(summary.repeats),
        _md_cache_hits(summary),
    ]
    return "\n".join(sections)


def _usd(value: Decimal) -> str:
    return "$" + ("0" if value.is_zero() else format(value.normalize(), "f"))


def _ms(value: float) -> str:
    return f"{value:.0f}"


def _cost_text(cost: CostSummary) -> str:
    text = (
        f"total {_usd(cost.total_usd)} · mean {_usd(cost.mean_usd_per_call)} · max"
        f" {_usd(cost.max_usd_per_call)} per call"
    )
    if cost.is_lower_bound:
        text += f" (lower bound: {cost.cost_incomplete} cost-incomplete)"
    else:
        text += f" ({cost.cost_incomplete} cost-incomplete)"
    return text


def _counts_text(counts: dict[str, int]) -> str:
    return " · ".join(f"{name} {count}" for name, count in counts.items()) or "none"


def _table(rows: Iterable[tuple[str, str]]) -> str:
    lines = ["| | |", "|---|---|", *(f"| {name} | {value} |" for name, value in rows)]
    return "\n".join(lines) + "\n"


def _md_header(summary: RunSummary) -> str:
    run = summary.run
    lines = [
        f"# LLM gateway measurement: {run.dataset} {run.dataset_version}",
        "",
        f"**{summary.labels[0]}**",
        "",
        *(f"- {label}" for label in summary.labels[1:]),
        "",
    ]
    return "\n".join(lines)


def _status(summary: RunSummary) -> str:
    if summary.feeds_note:
        return 'complete live run: may feed the "what I measured" note'
    prefix = "PARTIAL run" if summary.partial else "not for the note"
    return f'{prefix}; cannot feed the "what I measured" note: ' + "; ".join(summary.not_for_note)


def _seconds(value: float) -> str:
    return f"{value:g} s"


def _limits_text(limits: RunLimits) -> str:
    caps = ", ".join(f"{alias} {cap}" for alias, cap in sorted(limits.max_output_tokens.items()))
    return (
        f"retries {limits.max_retries} per request · repairs {limits.max_repairs} per call ·"
        f" attempt timeout {_seconds(limits.attempt_timeout_s)} · deadline"
        f" {_seconds(limits.deadline_s)} · backoff {_seconds(limits.backoff_base_s)} up to"
        f" {_seconds(limits.backoff_cap_s)} · max_output_tokens: {caps}"
    )


def _stop_text(run: RunInfo) -> str:
    if run.stopped is None:
        return "no"
    if run.stop_detail is None:
        return f"{run.stopped} (detail not recorded)"
    return f"{run.stopped}: " + run.stop_detail.replace("|", "\\|")  # one table cell, unbroken


def _md_run(summary: RunSummary) -> str:
    run = summary.run
    models = "; ".join(f"{alias} → {model}" for alias, model in sorted(run.alias_models.items()))
    rows = [
        ("Status", _status(summary)),
        (
            "Dataset",
            f"{run.dataset} {run.dataset_version} (prompt review: {run.dataset_review}; files as"
            f" committed: {'yes' if run.dataset_committed else 'no'})",
        ),
        (
            "Commit",
            f"{run.commit} (uncommitted changes: {'yes' if run.uncommitted_changes else 'no'})",
        ),
        ("Provider", run.provider),
        ("Started", run.started_at.isoformat()),
        ("Catalog version", run.catalog_version),
        ("Models (catalog)", models),
        ("Models that answered", ", ".join(summary.models_answered) or "none"),
        ("Limits", _limits_text(run.limits)),
        (
            "Calls",
            f"{summary.calls_made} made of {run.planned_calls} planned:"
            f" {summary.distinct_calls} distinct prompts, {summary.repeat_calls} repeats,"
            f" {summary.cache_hit_calls} cache hits",
        ),
        ("Stopped", _stop_text(run)),
        ("Cost of every call", _cost_text(summary.run_cost)),
        ("Cost cap", "none (dry run)" if run.max_usd is None else f"${run.max_usd:f}"),
    ]
    return "## Run\n\n" + _table(rows)


def _range_text(latency: LatencyRange | LatencySummary, deadline_cut: int) -> str:
    text = (
        f"median {_ms(latency.median_ms)} · min {_ms(latency.min_ms)} · max {_ms(latency.max_ms)}"
    )
    if isinstance(latency, LatencySummary):
        if latency.p95_ms is None:
            text += f" · p95 not reported (n = {latency.n} < {P95_MIN_DISTINCT_PROMPTS})"
        else:
            text += f" · p95 (inclusive) {_ms(latency.p95_ms)}"
    if deadline_cut:
        text += f" · includes {deadline_cut} cut off by the deadline (real latency longer)"
    return text


def _first_try_text(first_try: FirstTryValidity | None) -> str:
    if first_try is None:
        return "no non-forced structured case"
    return (
        f"{first_try.valid} valid on the first try (non-forced structured cases whose first answer"
        f" was validated); not validated, by outcome: {_counts_text(first_try.not_validated)}"
        f" · {FIRST_TRY_RULE}"
    )


def _forced_text(reports: tuple[ForcedRepairReport, ...]) -> str:
    if not reports:
        return "none on this alias"
    return "; ".join(
        f"{r.case_id}: {r.result} (outcome {r.outcome}, repairs {r.repairs})" for r in reports
    )


def _output_text(tokens: OutputTokens | None, cap: int) -> str:
    if tokens is None:
        return "no call without a repair got an answer"
    return (
        f"median {tokens.median:g} · max {tokens.max} of cap {cap} ({tokens.calls} calls with an"
        f" answer and no repair; {tokens.repaired_left_out} repaired left out, as each adds two"
        " answers)"
    )


def _count_rows(figures: AliasSummary | RepeatSummary) -> list[tuple[str, str]]:
    """The rows an alias and its repeats share, each rule beside its figure."""
    return [
        (
            "Timeout rate",
            f"{figures.timeout_rate} (deadline exceeded {figures.deadline_exceeded}) ·"
            f" {TIMEOUT_RULE}",
        ),
        ("Refusals", str(figures.refusals)),
        ("Truncated", f"{figures.truncated} · {TRUNCATION_RULE}"),
        ("First-try validity", _first_try_text(figures.first_try)),
        ("Forced repair", _forced_text(figures.forced_repairs)),
        ("Cost", _cost_text(figures.cost)),
        ("Outcomes", _counts_text(figures.outcomes)),
    ]


def _md_alias(alias: AliasSummary) -> str:
    rows = [
        ("Model (catalog)", alias.model_requested or "mixed"),
        ("Models that answered", ", ".join(alias.models_answered) or "none"),
        ("Distinct prompts (n)", str(alias.distinct_prompts)),
        ("Latency, ms", _range_text(alias.latency, alias.deadline_cut)),
        ("Output tokens per request", _output_text(alias.output_tokens, alias.max_output_tokens)),
        *_count_rows(alias),
    ]
    points = [
        f"| {p.case_id} | {p.output_tokens} | {_ms(p.latency_ms)} | {p.outcome} |"
        for p in alias.latency_vs_output_tokens
    ]
    return (
        f"### {alias.alias}\n\n"
        + _table(rows)
        + "\nLatency against output tokens:\n\n"
        + "| case | output tokens | latency, ms | outcome |\n|---|---|---|---|\n"
        + "\n".join(points)
        + "\n"
    )


def _tokens_text(tokens: TokensPerChar | None) -> str:
    if tokens is None:
        return "n/a"
    ratio = f"{tokens.prompt_tokens}/{tokens.prompt_chars}"
    return f"{tokens.per_char:.3f} ({ratio}, {tokens.calls} calls)"


def _md_groups(title: str, column: str, comparison: str, groups: tuple[GroupSummary, ...]) -> str:
    header = (
        f"## {title}, per alias (distinct prompts; no p95)\n\n"
        "One row per alias, so per model: no row pools two models. Tiny n and synthetic prompts:"
        f" descriptive only, not a {comparison} comparison. Each business has only the languages"
        " business-context §7 lists for it, so language and business are tied.\n\n"
        f"| {column} | alias | n | median, ms | min, ms | max, ms | prompt tokens per character |"
        " cost |\n|---|---|---|---|---|---|---|---|\n"
    )
    rows = [
        f"| {g.key} | {g.alias} | {g.n} | {_ms(g.latency.median_ms)} | {_ms(g.latency.min_ms)} |"
        f" {_ms(g.latency.max_ms)} | {_tokens_text(g.tokens_per_char)} | {_cost_text(g.cost)} |"
        for g in groups
    ]
    note = (
        "\nPrompt tokens per character: plain calls with an answer only (a structured call's"
        " input includes the gateway's schema instruction); characters are Unicode code points;"
        " the tokens may include a fixed per-request overhead.\n"
    )
    return header + "\n".join(rows) + "\n" + note


def _md_repeats(repeats: tuple[RepeatSummary, ...]) -> str:
    title = "## Repeats (the same prompts again: never distinct prompts; no p95)\n\n"
    if not repeats:
        return title + "None.\n"
    return title + "\n".join(_md_repeat(r) for r in repeats)


def _md_repeat(repeat: RepeatSummary) -> str:
    rows = [
        ("Repeat calls", f"{repeat.calls}, of {repeat.cases} cases"),
        ("Latency, ms", _range_text(repeat.latency, repeat.deadline_cut)),
        *_count_rows(repeat),
    ]
    return f"### {repeat.alias}, repeats\n\n" + _table(rows)


def _md_cache_hits(summary: RunSummary) -> str:
    title = "## Cache hits (reported separately; the cache is off in a live run)\n\n"
    text = f"{summary.cache_hit_rate} calls were cache hits"
    if summary.cache_hits:
        text += ": " + ", ".join(
            f"{h.case_id} (alias {h.alias}, repeat {h.repeat})" for h in summary.cache_hits
        )
    return title + text + ".\n"
