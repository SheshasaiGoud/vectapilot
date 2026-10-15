"""PR 8: the run summary (`measure/summary.py`), on a fixed fixture of hand-built records.

The fixture (`fixtures/summary_run/`) is one run: 25 cases, 27 calls, every record a real
`llm_call` log line. Latencies and costs are chosen to be easy to work by hand, not to be
realistic. Prompts are repeated characters ending in the case id (so each is distinct), and their
lengths are obvious: en 40 (`e`), hi 20 (`क`), hi code-mixed 40 (`h`), te 20 (`క`), te code-mixed
40 (`t`), in Unicode code points.

| case | business | lang | alias | kind | latency ms | outcome | notes |
|---|---|---|---|---|---|---|---|
| s01-s05 | sunrise | en | small | plain | 100-500 | ok | in 10, out 10*i |
| s06-s09 | paws | en | small | plain | 600-900 | ok | in 10, out 10*i |
| s10-s11 | northwind | en | small | plain | 1000-1100 | ok | in 10, out 10*i |
| s12 | northwind | en | small | IntentLabel | 1200 | ok | first try valid, out 120 |
| s13 | northwind | en | small | IntentLabel | 1300 | ok | repaired, out 130, $0.0002 |
| s14 | northwind | en | small | BillCheck, **forced** | 1400 | ok | repaired, out 140, $0.0002 |
| s15 | sunrise | hi | small | plain | 1500 | refusal | in 30, out 150 |
| s16 | sunrise | hi | small | IntentLabel | 1600 | refusal | first_try_valid null, out 160 |
| s17 | sunrise | hi-cm | small | plain | 1700 | truncated | in 20, out 1024 |
| s18 | sunrise | hi-cm | small | plain | 1800 | ok | attempt timeout, **incomplete**, out 180 |
| s19 | paws | te | small | plain | 1900 | ok | in 40, out 190 |
| s20 | paws | te-cm | small | plain | 2000 | ok | in 24, out 200 |
| l01 | northwind | en | large | OrderQuery | 3000 | ok | first try valid, out 300, $0.003 |
| l02 | northwind | en | large | OrderQuery | 30000 | deadline | no answer, **incomplete**, $0 |
| l03 | paws | te | large | BookingRequest | 2500 | validation_failed | repaired, out 400, $0.006 |
| l04 | paws | te | large | plain | 4500 | ok | in 40, out 500, $0.005 |
| l05 | sunrise | en | large | plain | 1500 | ok | in 10, out 250, $0.0025 |
| s01 repeat 1 | | | small | plain | 150 | ok | a repeat |
| s02 repeat 1 | | | small | plain | 2 | ok | a **cache hit**, $0 |

s18 has 1 attempt timeout; l02 (`deadline_exceeded`) has none; s18 and l02 are cost-incomplete.
In and out are input and output tokens. Every cost not listed is $0.0001. `s` cases are on
`small` (20 distinct), `l` cases on `large` (5). The run's limits (`LIMITS`) are Settings'
defaults, with caps small 1024 and large 2048.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from vectapilot_api.gateway.types import CallOutcome, CallRecord
from vectapilot_api.measure.dataset import SmokeCase, load_cases
from vectapilot_api.measure.records import (
    CallMade,
    MeasuredCall,
    join_records,
    parse_llm_call_lines,
)
from vectapilot_api.measure.summary import (
    FIRST_TRY_RULE,
    LATENCY_LABEL,
    NO_TARGET_LABEL,
    STOP_DETAIL_MAX_CHARS,
    SYNTHETIC_LABEL,
    TRUNCATION_RULE,
    AliasSummary,
    CacheHit,
    Fraction,
    GroupSummary,
    LatencyRange,
    RunInfo,
    RunSummary,
    SummaryInputError,
    check_distinct_prompts,
    render_json,
    render_markdown,
    summarise_run,
)

FIXTURE: Final = Path(__file__).parent / "fixtures" / "summary_run"
CASES: Final[tuple[SmokeCase, ...]] = load_cases((FIXTURE / "cases.jsonl").read_text("utf-8"))
CALLS_MADE: Final = tuple(
    CallMade.model_validate_json(line)
    for line in (FIXTURE / "calls_made.jsonl").read_text().splitlines()
)
RECORDS: Final = parse_llm_call_lines((FIXTURE / "llm_call_log.jsonl").read_text().splitlines())
CALLS: Final = join_records(CALLS_MADE, RECORDS)
TOL: Final = 1e-9
LIMITS: Final[dict[str, Any]] = {
    "max_retries": 2,
    "max_repairs": 1,
    "attempt_timeout_s": 15.0,
    "deadline_s": 30.0,
    "backoff_base_s": 0.5,
    "backoff_cap_s": 8.0,
    "max_output_tokens": {"small": 1024, "large": 2048},
}


def run_info(**overrides: Any) -> RunInfo:
    fields: dict[str, Any] = {
        "dataset": "summary_fixture",
        "dataset_version": "v0.1",
        "dataset_review": "done",
        "dataset_committed": True,
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "uncommitted_changes": False,
        "provider": "anthropic",
        "started_at": datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
        "catalog_version": "fixture-1",
        "alias_models": {"small": "model-small-test", "large": "model-large-test"},
        "limits": LIMITS,
        "planned_calls": 27,
        "max_usd": Decimal("1.00"),
    }
    return RunInfo.model_validate(fields | overrides)


@pytest.fixture(scope="module")
def summary() -> RunSummary:
    return summarise_run(run_info(), CASES, CALLS)


def alias(summary: RunSummary, name: str) -> AliasSummary:
    [found] = [a for a in summary.per_alias if a.alias == name]
    return found


def group(groups: Sequence[GroupSummary], key: str, on: str) -> GroupSummary:
    [found] = [g for g in groups if (g.key, g.alias) == (key, on)]
    return found


def case(case_id: str) -> SmokeCase:
    [found] = [c for c in CASES if c.id == case_id]
    return found


def measured(case_id: str, repeat: int = 0) -> MeasuredCall:
    [found] = [c for c in CALLS if (c.case_id, c.repeat) == (case_id, repeat)]
    return found


def with_record(call: MeasuredCall, *, repeat: int | None = None, **update: Any) -> MeasuredCall:
    """The same call (or a repeat of it) with some record fields changed (validated:
    `CallRecord.model_copy`)."""
    return MeasuredCall(
        case_id=call.case_id,
        repeat=call.repeat if repeat is None else repeat,
        record=call.record.model_copy(update=update),
    )


def k_n(k: int, n: int) -> Fraction:
    return Fraction(k=k, n=n)


# ── The fixture and the buckets ─────────────────────────────────────────────────────────────


def test_the_fixture_is_what_the_table_says() -> None:
    assert (len(CASES), len(CALLS)) == (25, 27)
    assert [c.case_id for c in CALLS[:3]] == ["s01", "s02", "s03"]  # in the order calls were made
    assert len({c.prompt for c in CASES}) == 25


def test_every_call_is_in_exactly_one_bucket(summary: RunSummary) -> None:
    # The 25 first runs are distinct; s01 repeat 1 is a repeat; s02 repeat 1 is a cache hit.
    assert (summary.calls_made, summary.distinct_calls) == (27, 25)
    assert (summary.repeat_calls, summary.cache_hit_calls) == (1, 1)
    assert sum(a.distinct_prompts for a in summary.per_alias) == 25
    assert sum(g.n for g in summary.per_language) == 25
    assert sum(g.n for g in summary.per_business) == 25


# ── Per alias: latency and p95 ──────────────────────────────────────────────────────────────


def test_twenty_distinct_prompts_get_an_inclusive_p95(summary: RunSummary) -> None:
    small = alias(summary, "small")
    latency = small.latency

    # Samples 100, 200, ..., 2000 (s01-s20; the repeat 150 and the cache hit 2 are not in them).
    # median = (x[9] + x[10]) / 2 = (1000 + 1100) / 2 = 1050.
    assert small.distinct_prompts == 20
    assert (latency.n, latency.min_ms, latency.median_ms, latency.max_ms) == (20, 100, 1050, 2000)
    # Inclusive p95: position 0.95 x (20 - 1) = 18.05 → x[18] + 0.05 x (x[19] - x[18])
    # = 1900 + 0.05 x 100 = 1905.
    assert latency.p95_ms == pytest.approx(1905, abs=TOL)


def test_fewer_than_twenty_distinct_prompts_get_no_p95(summary: RunSummary) -> None:
    large = alias(summary, "large")
    latency = large.latency

    # Samples 3000, 30000, 2500, 4500, 1500 → sorted 1500, 2500, 3000, 4500, 30000: median 3000.
    assert large.distinct_prompts == 5
    assert (latency.n, latency.min_ms, latency.median_ms, latency.max_ms) == (5, 1500, 3000, 30000)
    assert latency.p95_ms is None  # n = 5 < 20


def test_a_timed_out_call_is_never_dropped(summary: RunSummary) -> None:
    large = alias(summary, "large")

    # l02 hit the deadline at 30000 ms: it is the max, and it is flagged as cut off.
    assert large.latency.max_ms == 30000
    assert large.deadline_cut == 1
    assert ("l02", 0, 30000.0) in [
        (p.case_id, p.output_tokens, p.latency_ms) for p in large.latency_vs_output_tokens
    ]


# ── Per alias: k/n counts ───────────────────────────────────────────────────────────────────


def test_the_timeout_rate_counts_attempt_timeouts_and_the_deadline(summary: RunSummary) -> None:
    small, large = alias(summary, "small"), alias(summary, "large")

    # small: s18 had an attempt timeout (outcome ok) → 1/20; nothing hit the deadline → 0/20.
    assert (small.timeout_rate, small.deadline_exceeded) == (k_n(1, 20), k_n(0, 20))
    # large: l02 hit the deadline with 0 attempt timeouts → 1/5, and 1/5 on its own.
    assert (large.timeout_rate, large.deadline_exceeded) == (k_n(1, 5), k_n(1, 5))


def test_a_call_with_both_kinds_of_timeout_counts_once() -> None:
    # l02 again, now also with an attempt timeout: still one timed-out call, 1/5.
    calls = [with_record(c, attempt_timeouts=1) if c.case_id == "l02" else c for c in CALLS]

    large = alias(summarise_run(run_info(), CASES, calls), "large")

    assert (large.timeout_rate, large.deadline_exceeded) == (k_n(1, 5), k_n(1, 5))


def test_refusals_and_truncations_are_k_of_n(summary: RunSummary) -> None:
    small, large = alias(summary, "small"), alias(summary, "large")

    # small: s15 and s16 refused, s17 was truncated. large: neither.
    assert (small.refusals, small.truncated) == (k_n(2, 20), k_n(1, 20))
    assert (large.refusals, large.truncated) == (k_n(0, 5), k_n(0, 5))
    assert small.outcomes == {"ok": 17, "refusal": 2, "truncated": 1}
    assert large.outcomes == {"deadline_exceeded": 1, "ok": 3, "validation_failed": 1}


def test_output_tokens_are_per_request_beside_the_cap(summary: RunSummary) -> None:
    small, large = alias(summary, "small"), alias(summary, "large")

    # small: all 20 answered, but s13 and s14 were repaired (each adds two answers): left out.
    # The other 18: 10, 20, ..., 120 (s01-s12), 150, 160, 1024 (s17), 180, 190, 200. Sorted, the
    # 9th and 10th are 90 and 100 → median 95; max 1024, the truncated call, of cap 1024.
    assert small.output_tokens is not None
    assert (small.output_tokens.calls, small.output_tokens.repaired_left_out) == (18, 2)
    assert (small.output_tokens.median, small.output_tokens.max) == (95, 1024)
    assert small.max_output_tokens == 1024
    # large: l02 got no answer and l03 was repaired, so 3 calls: 250, 300, 500 → median 300.
    assert large.output_tokens is not None
    assert (large.output_tokens.calls, large.output_tokens.repaired_left_out) == (3, 1)
    assert (large.output_tokens.median, large.output_tokens.max) == (300, 500)
    assert large.max_output_tokens == 2048


def test_latency_is_reported_against_output_tokens(summary: RunSummary) -> None:
    large = alias(summary, "large")

    # Every distinct call, sorted by output tokens: l02 (0, no answer), l05, l01, l03, l04.
    assert [(p.case_id, p.output_tokens, p.latency_ms) for p in large.latency_vs_output_tokens] == [
        ("l02", 0, 30000),
        ("l05", 250, 1500),
        ("l01", 300, 3000),
        ("l03", 400, 2500),
        ("l04", 500, 4500),
    ]
    assert len(alias(summary, "small").latency_vs_output_tokens) == 20


# ── First-try validity (Maya's variant) and the forced repair ───────────────────────────────


def test_first_try_validity_follows_mayas_rule(summary: RunSummary) -> None:
    small, large = alias(summary, "small"), alias(summary, "large")

    # small's non-forced structured cases: s12 (true), s13 (false), s16 (null: refused).
    # n = 2 (validated), k = 1; s16 is reported beside it, by outcome. s14 is forced: not in it.
    assert small.first_try is not None
    assert (small.first_try.valid, small.first_try.not_validated) == (k_n(1, 2), {"refusal": 1})
    # large: l01 (true), l03 (false, then validation_failed), l02 (null: the deadline).
    assert large.first_try is not None
    assert (large.first_try.valid, large.first_try.not_validated) == (
        k_n(1, 2),
        {"deadline_exceeded": 1},
    )


def test_an_alias_without_structured_cases_has_no_first_try_figure() -> None:
    plain = [c for c in CALLS if c.case_id in {"s01", "s02", "s03"} and c.repeat == 0]

    summary = summarise_run(run_info(planned_calls=3), CASES, plain)

    assert alias(summary, "small").first_try is None


def test_the_forced_repair_is_reported_on_its_own(summary: RunSummary) -> None:
    [report] = alias(summary, "small").forced_repairs

    assert (report.case_id, report.outcome, report.repairs) == ("s14", CallOutcome.OK, 1)
    assert report.result == "repair worked"
    assert alias(summary, "large").forced_repairs == ()


@pytest.mark.parametrize(
    ("update", "result"),
    [
        # The first answer happened to be valid: no repair was needed.
        (
            {"attempts": 1, "repairs": 0, "first_try_valid": True, "request_ids": ["r-1"]},
            "not exercised: the first answer",
        ),
        # Refused before any answer was validated.
        (
            {
                "outcome": "refusal",
                "stop_reason": "refusal",
                "attempts": 1,
                "repairs": 0,
                "first_try_valid": None,
                "request_ids": ["r-1"],
            },
            "not exercised: no first answer",
        ),
        # The repaired answer was invalid too.
        ({"outcome": "validation_failed"}, "repair failed"),
        # Invalid, repaired, then the deadline fired during the repair.
        ({"outcome": "deadline_exceeded", "cost_complete": False}, "repair did not finish"),
        # Invalid, and repairs are off (max_repairs 0).
        (
            {"outcome": "validation_failed", "attempts": 1, "repairs": 0, "request_ids": ["r-1"]},
            "first answer invalid, no repair made",
        ),
    ],
)
def test_every_forced_repair_result_is_named(update: dict[str, Any], result: str) -> None:
    forced = with_record(measured("s14"), **update)

    summary = summarise_run(run_info(planned_calls=1), CASES, [forced])

    [report] = alias(summary, "small").forced_repairs
    assert report.result.startswith(result)


# ── Cost ────────────────────────────────────────────────────────────────────────────────────


def test_cost_is_an_exact_decimal_sum(summary: RunSummary) -> None:
    small, large = alias(summary, "small").cost, alias(summary, "large").cost

    # small: 18 x 0.0001 + 2 x 0.0002 (s13, s14) = 0.0022; mean 0.0022 / 20 = 0.00011.
    assert (small.total_usd, small.mean_usd_per_call, small.max_usd_per_call) == (
        Decimal("0.0022"),
        Decimal("0.00011"),
        Decimal("0.0002"),
    )
    # large: 0.003 + 0 + 0.006 + 0.005 + 0.0025 = 0.0165; mean 0.0165 / 5 = 0.0033.
    assert (large.total_usd, large.mean_usd_per_call, large.max_usd_per_call) == (
        Decimal("0.0165"),
        Decimal("0.0033"),
        Decimal("0.006"),
    )
    assert isinstance(small.total_usd, Decimal)
    # Every call: 0.0022 + 0.0165 + the repeat's 0.0001 + the cache hit's 0 = 0.0188.
    assert summary.run_cost.total_usd == Decimal("0.0188")


def test_an_incomplete_cost_makes_the_total_a_lower_bound(summary: RunSummary) -> None:
    small, large = alias(summary, "small").cost, alias(summary, "large").cost

    # s18 (small) and l02 (large) are cost-incomplete.
    assert (small.cost_incomplete, small.is_lower_bound) == (k_n(1, 20), True)
    assert (large.cost_incomplete, large.is_lower_bound) == (k_n(1, 5), True)
    assert (summary.run_cost.cost_incomplete, summary.run_cost.is_lower_bound) == (k_n(2, 27), True)
    # paws-and-co has none of them, on either alias: not a lower bound.
    for on, n in (("small", 6), ("large", 2)):
        paws = group(summary.per_business, "paws-and-co", on).cost
        assert (paws.cost_incomplete, paws.is_lower_bound) == (k_n(0, n), False)


# ── Per language and per business, per alias ────────────────────────────────────────────────


def test_language_and_business_rows_are_per_alias(summary: RunSummary) -> None:
    # One row per (key, alias) that has calls, sorted; never one row pooling two aliases.
    assert [(g.key, g.alias) for g in summary.per_language] == [
        ("en", "large"),
        ("en", "small"),
        ("hi", "small"),
        ("hi-code-mixed", "small"),
        ("te", "large"),
        ("te", "small"),
        ("te-code-mixed", "small"),
    ]
    assert [(g.key, g.alias) for g in summary.per_business] == [
        ("northwind-shop", "large"),
        ("northwind-shop", "small"),
        ("paws-and-co", "large"),
        ("paws-and-co", "small"),
        ("sunrise-clinic", "large"),
        ("sunrise-clinic", "small"),
    ]


def test_per_language_figures(summary: RunSummary) -> None:
    def latency(key: str, on: str) -> tuple[int, float, float, float]:
        found = group(summary.per_language, key, on).latency
        return (found.n, found.median_ms, found.min_ms, found.max_ms)

    # en small: s01-s14, 100-1400 → 14 samples; (700 + 800) / 2 = 750.
    assert latency("en", "small") == (14, 750, 100, 1400)
    # en large: l05 1500, l01 3000, l02 30000 → median 3000.
    assert latency("en", "large") == (3, 3000, 1500, 30000)
    assert latency("hi", "small") == (2, 1550, 1500, 1600)  # s15, s16: (1500 + 1600) / 2
    assert latency("hi-code-mixed", "small") == (2, 1750, 1700, 1800)  # s17, s18
    assert latency("te", "small") == (1, 1900, 1900, 1900)  # s19
    assert latency("te", "large") == (2, 3500, 2500, 4500)  # l03 2500, l04 4500
    assert latency("te-code-mixed", "small") == (1, 2000, 2000, 2000)  # s20
    # en small: 12 x 0.0001 (s01-s12) + 2 x 0.0002 (s13, s14) = 0.0016, all complete.
    en_small = group(summary.per_language, "en", "small").cost
    assert (en_small.total_usd, en_small.cost_incomplete) == (Decimal("0.0016"), k_n(0, 14))
    # en large: 0.003 + 0 + 0.0025 = 0.0055; l02 is cost-incomplete.
    en_large = group(summary.per_language, "en", "large").cost
    assert (en_large.total_usd, en_large.cost_incomplete) == (Decimal("0.0055"), k_n(1, 3))
    # te large: 0.006 + 0.005 = 0.011.
    assert group(summary.per_language, "te", "large").cost.total_usd == Decimal("0.011")


def test_per_business_figures(summary: RunSummary) -> None:
    def latency(key: str, on: str) -> tuple[int, float, float, float]:
        found = group(summary.per_business, key, on).latency
        return (found.n, found.median_ms, found.min_ms, found.max_ms)

    # sunrise small: 100-500, 1500, 1600, 1700, 1800 → 9; the 5th is 500.
    assert latency("sunrise-clinic", "small") == (9, 500, 100, 1800)
    assert latency("sunrise-clinic", "large") == (1, 1500, 1500, 1500)  # l05
    # paws small: 600-900, 1900, 2000 → 6; (800 + 900) / 2 = 850.
    assert latency("paws-and-co", "small") == (6, 850, 600, 2000)
    assert latency("paws-and-co", "large") == (2, 3500, 2500, 4500)  # l03, l04
    # northwind small: 1000-1400 → 5; the 3rd is 1200. large: 3000, 30000 → 16500.
    assert latency("northwind-shop", "small") == (5, 1200, 1000, 1400)
    assert latency("northwind-shop", "large") == (2, 16500, 3000, 30000)
    # sunrise 0.0009 + 0.0025; paws 0.0006 + 0.011; northwind 0.0007 (3 x 0.0001 + 2 x 0.0002)
    # + 0.003. The sums per business match the whole run's distinct calls: 0.0187.
    costs = {(g.key, g.alias): g.cost.total_usd for g in summary.per_business}
    assert costs == {
        ("northwind-shop", "large"): Decimal("0.003"),
        ("northwind-shop", "small"): Decimal("0.0007"),
        ("paws-and-co", "large"): Decimal("0.011"),
        ("paws-and-co", "small"): Decimal("0.0006"),
        ("sunrise-clinic", "large"): Decimal("0.0025"),
        ("sunrise-clinic", "small"): Decimal("0.0009"),
    }
    assert sum(costs.values()) == Decimal("0.0187")


def test_no_p95_per_language_or_business(summary: RunSummary) -> None:
    assert set(LatencyRange.model_fields) == {"n", "min_ms", "median_ms", "max_ms"}
    # The type has no p95 field, so no group can carry one, however many calls it has.
    for found in (*summary.per_language, *summary.per_business):
        assert "p95" not in found.model_dump_json()


def test_prompt_tokens_per_character_are_per_alias(summary: RunSummary) -> None:
    def ratio(groups: Sequence[GroupSummary], key: str, on: str) -> tuple[int, int, int, float]:
        tokens = group(groups, key, on).tokens_per_char
        assert tokens is not None
        return (tokens.calls, tokens.prompt_tokens, tokens.prompt_chars, tokens.per_char)

    # Plain calls with an answer only. en small: s01-s11, 11 x 10 tokens over 11 x 40 chars.
    assert ratio(summary.per_language, "en", "small") == (11, 110, 440, 0.25)
    assert ratio(summary.per_language, "en", "large") == (1, 10, 40, 0.25)  # l05; l01, l02 struct.
    assert ratio(summary.per_language, "hi", "small") == (1, 30, 20, 1.5)  # s15; s16 is structured
    assert ratio(summary.per_language, "hi-code-mixed", "small") == (2, 40, 80, 0.5)
    # te on each alias, never pooled: s19 on small, l04 on large (l03 is structured).
    assert ratio(summary.per_language, "te", "small") == (1, 40, 20, 2.0)
    assert ratio(summary.per_language, "te", "large") == (1, 40, 20, 2.0)
    assert ratio(summary.per_language, "te-code-mixed", "small") == (1, 24, 40, 0.6)
    # sunrise small: s01-s05 (50/200), s15 (30/20), s17 + s18 (40/80) → 120/300.
    assert ratio(summary.per_business, "sunrise-clinic", "small") == (8, 120, 300, 0.4)
    # paws small: s06-s09 (40/160), s19 (40/20), s20 (24/40) → 104/220.
    paws = ratio(summary.per_business, "paws-and-co", "small")
    assert paws[:3] == (6, 104, 220)
    assert paws[3] == pytest.approx(104 / 220)  # 0.4727..., not exact in binary
    assert ratio(summary.per_business, "northwind-shop", "small") == (2, 20, 80, 0.25)  # s10, s11


def test_a_group_with_only_structured_calls_has_no_tokens_per_character(
    summary: RunSummary,
) -> None:
    # northwind on large: l01 and l02, both structured.
    assert group(summary.per_business, "northwind-shop", "large").tokens_per_char is None


# ── Repeats and cache hits ──────────────────────────────────────────────────────────────────


def test_repeats_are_reported_separately_and_never_as_distinct_prompts(
    summary: RunSummary,
) -> None:
    [repeats] = summary.repeats

    assert (repeats.alias, repeats.calls, repeats.cases) == ("small", 1, 1)
    assert repeats.latency == LatencyRange(n=1, min_ms=150, median_ms=150, max_ms=150)
    assert repeats.cost.total_usd == Decimal("0.0001")
    assert (repeats.timeout_rate, repeats.refusals, repeats.truncated) == (
        k_n(0, 1),
        k_n(0, 1),
        k_n(0, 1),
    )
    assert (repeats.first_try, repeats.forced_repairs) == (None, ())  # s01 is plain
    assert alias(summary, "small").distinct_prompts == 20  # s01's repeat is not a 21st


def test_a_failure_in_a_repeat_is_never_hidden() -> None:
    # Step 8 review. s12 again: its first answer is now invalid (repaired), after one attempt
    # timed out (1 + 1 repair + 1 retry = 3 attempts), and it still ends ok. The forced s14 again:
    # its repair now fails too.
    s12_again = with_record(
        measured("s12"),
        repeat=1,
        first_try_valid=False,
        repairs=1,
        retries=1,
        attempts=3,
        attempt_timeouts=1,
        cost_complete=False,
    )
    s14_again = with_record(measured("s14"), repeat=1, outcome="validation_failed")

    summary = summarise_run(run_info(planned_calls=29), CASES, [*CALLS, s12_again, s14_again])

    [repeats] = summary.repeats
    # Repeats of small: s01 (ok), s12 (timed out once, ok), s14 (validation_failed).
    assert (repeats.calls, repeats.cases) == (3, 3)
    assert (repeats.timeout_rate, repeats.deadline_exceeded) == (k_n(1, 3), k_n(0, 3))
    assert repeats.first_try is not None
    assert (repeats.first_try.valid, repeats.first_try.not_validated) == (k_n(0, 1), {})
    [forced] = repeats.forced_repairs
    assert (forced.case_id, forced.result) == ("s14", "repair failed")
    assert repeats.outcomes == {"ok": 2, "validation_failed": 1}
    assert (repeats.cost.cost_incomplete, repeats.cost.is_lower_bound) == (k_n(1, 3), True)
    # The distinct figures do not change: the repeats are kept apart.
    small = alias(summary, "small")
    assert (small.distinct_prompts, small.timeout_rate) == (20, k_n(1, 20))
    assert small.first_try is not None and small.first_try.valid == k_n(1, 2)
    # And the Markdown shows them, each rule beside its figure.
    markdown = render_markdown(summary)
    for text in (
        "### small, repeats",
        "| Repeat calls | 3, of 3 cases |",
        "| Timeout rate | 1/3 (deadline exceeded 0/3)",
        f"0/1 valid on the first try (non-forced structured cases whose first answer was"
        f" validated); not validated, by outcome: none · {FIRST_TRY_RULE} |",
        "| Forced repair | s14: repair failed (outcome validation_failed, repairs 1) |",
    ):
        assert text in markdown, text


def test_cache_hits_are_reported_separately(summary: RunSummary) -> None:
    assert summary.cache_hits == (CacheHit(case_id="s02", repeat=1, alias="small"),)
    assert summary.cache_hit_rate == k_n(1, 27)
    assert alias(summary, "small").latency.min_ms == 100  # the hit's 2 ms is in no latency figure


def test_a_cache_hit_on_a_first_run_is_not_a_distinct_prompt() -> None:
    # s03's first run becomes a cache hit (exactly the shape CallRecord accepts for one).
    hit = with_record(
        measured("s03"),
        attempts=0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=Decimal(0),
        inference_geo=None,
        request_ids=(),
        cache_hit=True,
        latency_ms=1.0,
    )
    calls = [hit if c.case_id == "s03" and c.repeat == 0 else c for c in CALLS]

    summary = summarise_run(run_info(), CASES, calls)

    small = alias(summary, "small")
    # 19 distinct prompts are left, so the hit also takes away the p95 (19 < 20).
    assert (small.distinct_prompts, small.latency.p95_ms) == (19, None)
    assert [(h.case_id, h.repeat) for h in summary.cache_hits] == [("s03", 0), ("s02", 1)]
    assert summary.cache_hit_rate == k_n(2, 27)


# ── Distinct prompts ────────────────────────────────────────────────────────────────────────


def test_two_cases_with_one_prompt_on_one_alias_are_refused() -> None:
    # They would count twice toward n (and toward the p95's n >= 20). The message names the ids.
    twin = case("s05").model_copy(update={"id": "s99"})

    with pytest.raises(SummaryInputError) as caught:
        summarise_run(run_info(), [*CASES, twin], CALLS)

    message = str(caught.value)
    assert message.startswith("cases s05 and s99 send the same prompt on alias small")
    assert twin.prompt not in message


def test_one_prompt_on_two_aliases_is_allowed() -> None:
    # Each alias has its own n, so the same prompt on small and on large is two distinct prompts.
    other_alias = case("s05").model_copy(update={"id": "s99", "alias": "large"})

    check_distinct_prompts([*CASES, other_alias])  # does not raise


# ── Partial runs and the note ───────────────────────────────────────────────────────────────


def test_cost_incomplete_calls_do_not_make_a_run_partial(summary: RunSummary) -> None:
    # Decided by shesha, 2026-10-15. All 27 planned calls were made; s18 (an attempt timeout) and
    # l02 (the deadline) are cost-incomplete: counted as k/n, the cost a lower bound, nothing more.
    markdown = render_markdown(summary)

    assert (summary.partial, summary.partial_reasons) == (False, ())
    assert (summary.feeds_note, summary.not_for_note) == (True, ())
    assert (summary.run_cost.cost_incomplete, summary.run_cost.is_lower_bound) == (k_n(2, 27), True)
    assert "| Cost of every call | total $0.0188 · " in markdown
    assert "per call (lower bound: 2/27 cost-incomplete) |" in markdown
    # The old collision is gone: a run with an attempt timeout can feed the note, so its timeout
    # rate needs no warning label.
    assert alias(summary, "small").timeout_rate == k_n(1, 20)
    assert "Partial-run rule" not in markdown


STOP_DETAIL: Final = (
    "stopped before call 11 (s11): its worst case $0.0123 on top of the $0.001 counted so far"
    " would cross the cap $1.00"
)


def test_a_run_stopped_by_the_cost_cap_is_partial() -> None:
    first_ten = [c for c in CALLS if c.case_id in {f"s{i:02}" for i in range(1, 11)}][:10]

    summary = summarise_run(run_info(stopped="cost_cap", stop_detail=STOP_DETAIL), CASES, first_ten)

    assert summary.partial is True
    assert summary.partial_reasons == ("stopped early: cost_cap", "10/27 planned calls made")
    assert summary.feeds_note is False
    assert "partial run: stopped early: cost_cap" in summary.not_for_note
    markdown = render_markdown(summary)
    assert '| Status | PARTIAL run; cannot feed the "what I measured" note:' in markdown
    assert f"| Stopped | cost_cap: {STOP_DETAIL} |" in markdown


def test_a_run_short_of_its_plan_is_partial_even_without_a_stop() -> None:
    summary = summarise_run(run_info(), CASES, CALLS[:-1])

    assert summary.partial is True
    assert summary.partial_reasons == ("26/27 planned calls made",)
    assert summary.feeds_note is False


def test_a_stop_by_the_last_planned_call_is_complete_but_never_feeds_the_note() -> None:
    # Every planned call was made, then the last one stopped the run (here: billed beyond its
    # bound). Not partial under decision 1, but not for the note either (proposal, pending shesha).
    five = [measured(f"s{i:02}") for i in range(1, 6)]
    detail = "s05: billed beyond its worst-case bound, so the bound's assumptions do not hold"

    summary = summarise_run(
        run_info(planned_calls=5, stopped="error", stop_detail=detail), CASES, five
    )

    assert (summary.partial, summary.partial_reasons) == (False, ())
    assert summary.feeds_note is False
    assert summary.not_for_note == ("stopped by its last planned call: error",)
    markdown = render_markdown(summary)
    assert (
        '| Status | not for the note; cannot feed the "what I measured" note: stopped by its last'
        " planned call: error |"
    ) in markdown
    assert f"| Stopped | error: {detail} |" in markdown


def test_only_a_complete_reviewed_committed_live_run_feeds_the_note() -> None:
    five = [measured(f"s{i:02}") for i in range(1, 6)]  # all cost-complete

    complete = summarise_run(run_info(planned_calls=5), CASES, five)

    assert (complete.partial, complete.feeds_note, complete.not_for_note) == (False, True, ())
    disqualified: list[tuple[dict[str, Any], str]] = [
        ({"provider": "fake", "max_usd": None}, "dry run: fake provider, not a measurement"),
        ({"dataset_review": "pending"}, "the hi/te and code-mixed prompts are not reviewed yet"),
        ({"uncommitted_changes": True}, "uncommitted changes: the commit does not name the code"),
        (
            {"dataset_committed": False},
            "dataset not as committed: the commit does not name the prompts that ran",
        ),
    ]
    for overrides, reason in disqualified:
        other = summarise_run(run_info(planned_calls=5, **overrides), CASES, five)
        assert (other.partial, other.feeds_note) == (False, False)
        assert len(other.not_for_note) == 1
        assert other.not_for_note[0].startswith(reason)


@pytest.mark.parametrize(("committed", "shown"), [(True, "yes"), (False, "no")])
def test_the_run_table_says_whether_the_dataset_files_are_as_committed(
    committed: bool, shown: str
) -> None:
    run = run_info(planned_calls=1, dataset_committed=committed)

    markdown = render_markdown(summarise_run(run, CASES, [measured("s01")]))

    assert (
        "| Dataset | summary_fixture v0.1 (prompt review: done; files as committed:"
        f" {shown}) |" in markdown
    )


# ── How the run stopped: `stop_detail` ──────────────────────────────────────────────────────


def test_the_stop_detail_is_in_the_json_and_round_trips() -> None:
    summary = summarise_run(
        run_info(stopped="cost_cap", stop_detail=STOP_DETAIL), CASES, CALLS[:10]
    )

    text = render_json(summary)
    payload = json.loads(text)
    assert (payload["run"]["stopped"], payload["run"]["stop_detail"]) == ("cost_cap", STOP_DETAIL)
    assert RunSummary.model_validate_json(text) == summary


def test_a_run_that_was_not_stopped_has_no_stop_detail(summary: RunSummary) -> None:
    payload = json.loads(render_json(summary))

    assert (payload["run"]["stopped"], payload["run"]["stop_detail"]) == (None, None)
    assert "| Stopped | no |" in render_markdown(summary)


def test_a_stop_without_a_detail_says_the_detail_was_not_recorded() -> None:
    summary = summarise_run(run_info(stopped="max_calls"), CASES, CALLS[:1])

    assert "| Stopped | max_calls (detail not recorded) |" in render_markdown(summary)


def test_a_pipe_in_the_stop_detail_cannot_break_the_run_table() -> None:
    summary = summarise_run(
        run_info(stopped="error", stop_detail="s01: unexpected A|B"), CASES, CALLS[:1]
    )

    assert "| Stopped | error: s01: unexpected A\\|B |" in render_markdown(summary)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"stop_detail": "s01: outcome auth_failed"}, "stop_detail needs stopped"),
        ({"stopped": "error", "stop_detail": ""}, "at least 1 character"),
        ({"stopped": "error", "stop_detail": "s01: outcome auth_failed\nmore"}, "pattern"),
        (
            {"stopped": "error", "stop_detail": "s01 " * (STOP_DETAIL_MAX_CHARS // 4 + 1)},
            f"at most {STOP_DETAIL_MAX_CHARS} characters",
        ),
    ],
)
def test_a_stop_detail_is_one_short_line_and_needs_a_stop(
    fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as caught:
        run_info(**fields)

    assert "auth_failed" not in str(caught.value)  # the error names the rule, never the value


def test_a_stop_detail_added_by_model_copy_is_checked_too() -> None:
    # The CLI adds how the run ended with `model_copy(update=...)`, which skips validation:
    # `summarise_run` validates the run's facts again.
    unchecked = run_info().model_copy(update={"stop_detail": "s01: outcome auth_failed"})

    with pytest.raises(SummaryInputError, match="stop_detail needs stopped") as caught:
        summarise_run(unchecked, CASES, CALLS)

    assert "auth_failed" not in str(caught.value)


# ── Labels, provenance and config ───────────────────────────────────────────────────────────


def test_a_live_run_carries_the_labels_and_its_provenance(summary: RunSummary) -> None:
    markdown = render_markdown(summary)

    assert summary.labels == (SYNTHETIC_LABEL, LATENCY_LABEL, NO_TARGET_LABEL)
    assert SYNTHETIC_LABEL == "synthetic prompts, single home network"
    for text in (
        SYNTHETIC_LABEL,
        LATENCY_LABEL,
        NO_TARGET_LABEL,
        "0123456789abcdef0123456789abcdef01234567",  # commit
        "summary_fixture v0.1",  # dataset and version
        "fixture-1",  # catalog version
        "large → model-large-test; small → model-small-test",  # model ids
    ):
        assert text in markdown


def test_the_markdown_names_the_limits_the_run_used(summary: RunSummary) -> None:
    # Team rule 3: numbers come with their config. Retries, repairs, timeouts, backoff and caps.
    assert (
        "| Limits | retries 2 per request · repairs 1 per call · attempt timeout 15 s · deadline"
        " 30 s · backoff 0.5 s up to 8 s · max_output_tokens: large 2048, small 1024 |"
    ) in render_markdown(summary)


def test_the_json_names_the_limits_the_run_used(summary: RunSummary) -> None:
    payload = json.loads(render_json(summary))

    assert payload["run"]["limits"] == LIMITS
    assert {a["alias"]: a["max_output_tokens"] for a in payload["per_alias"]} == {
        "large": 2048,
        "small": 1024,
    }


def test_the_limits_are_required() -> None:
    # Team rule 3: no figure without the limits behind it, so no summary says "not recorded".
    without_limits = run_info().model_dump(exclude={"limits"})

    with pytest.raises(ValidationError, match=r"limits\n  Field required"):
        RunInfo.model_validate(without_limits)
    with pytest.raises(ValidationError, match=r"limits\n  Input should be a valid dictionary"):
        run_info(limits=None)


def test_limits_dropped_by_model_copy_are_refused_too() -> None:
    # The CLI adds how the run ended with `model_copy(update=...)`, which skips validation:
    # `summarise_run` validates the run's facts again.
    unchecked = run_info().model_copy(update={"limits": None})

    with pytest.raises(SummaryInputError, match=r"invalid: .*\nlimits\n"):
        summarise_run(unchecked, CASES, CALLS)


def test_the_limits_must_name_every_alias_of_the_catalog() -> None:
    caps_for_one_alias = LIMITS | {"max_output_tokens": {"small": 1024}}

    with pytest.raises(ValidationError, match="must name exactly the aliases"):
        run_info(limits=caps_for_one_alias)


def test_a_dry_run_is_labelled_as_one() -> None:
    summary = summarise_run(run_info(provider="fake", max_usd=None), CASES, CALLS)

    assert summary.labels[0].startswith("dry run: fake provider")
    assert SYNTHETIC_LABEL not in summary.labels  # no network was involved
    assert "Cost cap | none (dry run)" in render_markdown(summary)


def test_no_latency_target_is_quoted_or_claimed(summary: RunSummary) -> None:
    text = render_markdown(summary) + render_json(summary)

    # business-context §6's message-to-reply target (p95 < 6 s) never appears next to the figures.
    for forbidden in ("6 s", "6s", "< 6", "6000", "target met", "meets", "passed"):
        assert forbidden not in text


# ── Rendering ───────────────────────────────────────────────────────────────────────────────


def test_markdown_shows_counts_bounds_and_the_rules(summary: RunSummary) -> None:
    markdown = render_markdown(summary)

    for text in (
        "median 1050 · min 100 · max 2000 · p95 (inclusive) 1905",
        "median 3000 · min 1500 · max 30000 · p95 not reported (n = 5 < 20)",
        "includes 1 cut off by the deadline",
        "| Timeout rate | 1/20 (deadline exceeded 0/20) · counts deadline_exceeded or any attempt"
        " timeout (decided 2026-10-14) |",
        "| Timeout rate | 1/5 (deadline exceeded 1/5)",
        "| Refusals | 2/20 |",
        f"| Truncated | 1/20 · {TRUNCATION_RULE} |",
        "| Output tokens per request | median 95 · max 1024 of cap 1024 (18 calls with an answer"
        " and no repair; 2 repaired left out, as each adds two answers) |",
        "max 500 of cap 2048",
        "1/2 valid on the first try",
        f"not validated, by outcome: refusal 1 · {FIRST_TRY_RULE} |",
        f"not validated, by outcome: deadline_exceeded 1 · {FIRST_TRY_RULE} |",
        "s14: repair worked (outcome ok, repairs 1)",
        "total $0.0022 · mean $0.00011 · max $0.0002 per call (lower bound: 1/20 cost-incomplete)",
        "(0/6 cost-incomplete)",
        "| hi | small | 2 | 1550 | 1500 | 1600 | 1.500 (30/20, 1 calls) |",
        "| te | large | 2 | 3500 | 2500 | 4500 | 2.000 (40/20, 1 calls) |",
        "not a language comparison",
        "not a business comparison",
        "1/27 calls were cache hits: s02 (alias small, repeat 1).",
        '| Status | complete live run: may feed the "what I measured" note |',
        "| Stopped | no |",
    ):
        assert text in markdown, text


def test_markdown_of_a_complete_run_without_repeats_hits_or_structured_cases() -> None:
    five = [measured(f"s{i:02}") for i in range(1, 6)]

    markdown = render_markdown(summarise_run(run_info(planned_calls=5), CASES, five))

    for text in (
        '| Status | complete live run: may feed the "what I measured" note |',
        "| First-try validity | no non-forced structured case |",
        "| Forced repair | none on this alias |",
        "the same prompts again: never distinct prompts; no p95)\n\nNone.",
        "0/5 calls were cache hits.",
    ):
        assert text in markdown, text


def test_markdown_when_no_call_got_an_answer() -> None:
    # Only l02 (structured, the deadline fired before any answer).
    markdown = render_markdown(summarise_run(run_info(planned_calls=1), CASES, [measured("l02")]))

    assert "| Output tokens per request | no call without a repair got an answer |" in markdown
    assert "| northwind-shop | large | 1 | 30000 | 30000 | 30000 | n/a |" in markdown


def test_markdown_holds_no_prompt(summary: RunSummary) -> None:
    markdown = render_markdown(summary)

    for prompt in {c.prompt for c in CASES}:
        assert prompt not in markdown


def test_json_round_trips_with_exact_money(summary: RunSummary) -> None:
    text = render_json(summary)

    assert RunSummary.model_validate_json(text) == summary
    payload = json.loads(text)
    assert payload["run_cost"]["total_usd"] == "0.0188"  # a fixed-point string, never a float
    assert payload["per_alias"][1]["latency"]["p95_ms"] == pytest.approx(1905)  # small
    assert payload["per_alias"][0]["latency"]["p95_ms"] is None  # large
    assert payload["per_alias"][1]["output_tokens"]["repaired_left_out"] == 2


# ── Inputs that contradict each other ───────────────────────────────────────────────────────


def _foreign(call: MeasuredCall, **update: Any) -> list[MeasuredCall]:
    return [with_record(call, **update), *(c for c in CALLS if c is not call)]


@pytest.mark.parametrize(
    ("calls", "run", "message"),
    [
        (_foreign(measured("s01"), business_id="paws-and-co"), {}, "another business"),
        (_foreign(measured("s01"), alias="large", model_requested="model-large-test"), {}, "alias"),
        (_foreign(measured("s01"), catalog_version="fixture-2"), {}, "catalog version"),
        (_foreign(measured("s01"), model_requested="model-other"), {}, "model_requested"),
        (_foreign(measured("s01"), first_try_valid=True), {}, "plain case"),
        (
            [*CALLS, MeasuredCall(case_id="nope", repeat=0, record=CALLS[0].record)],
            {"planned_calls": 28},
            "unknown case",
        ),
        ([*CALLS, CALLS[0]], {"planned_calls": 28}, "appears twice"),
        (list(CALLS), {"planned_calls": 26}, "more calls"),
    ],
)
def test_contradicting_inputs_are_refused(
    calls: list[MeasuredCall], run: dict[str, Any], message: str
) -> None:
    with pytest.raises(SummaryInputError, match=message):
        summarise_run(run_info(**run), CASES, calls)


def test_a_call_on_an_alias_outside_the_runs_catalog_is_refused() -> None:
    # The gateway's unknown-alias rejection has no `model_requested`, so only the case's alias shows
    # that the run has no model and no cap for it (the CLI's `check_dataset` refuses it earlier).
    rejected = with_record(
        measured("l02"),
        alias="medium",
        model_requested=None,
        outcome=CallOutcome.INVALID_REQUEST,
        attempts=0,
        retries=0,
        request_ids=(),
        latency_ms=1.0,
    )
    medium = SmokeCase.model_validate(case("l02").model_dump() | {"alias": "medium"})
    cases = [medium if c.id == "l02" else c for c in CASES]

    with pytest.raises(SummaryInputError, match="case l02: its alias is not in the run's catalog"):
        summarise_run(run_info(planned_calls=1), cases, [rejected])


def test_duplicate_case_ids_are_refused() -> None:
    with pytest.raises(SummaryInputError, match="unique"):
        summarise_run(run_info(), [*CASES, CASES[0]], CALLS)


def test_a_fraction_cannot_have_k_above_n() -> None:
    with pytest.raises(ValueError, match="k must be <= n"):
        Fraction(k=3, n=2)
    assert str(k_n(1, 20)) == "1/20"


def test_the_fixture_records_are_valid_call_records() -> None:
    # The fixture is read through the same parser the CLI uses; each record is a CallRecord.
    assert all(isinstance(r, CallRecord) for r in RECORDS)
    assert case("s14").forced_repair is True
