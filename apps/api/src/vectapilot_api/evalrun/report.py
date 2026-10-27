"""A run's folder and its summary (M2 PR 8): every number traceable to a file in the folder.

**The folder** (live: `evals/reports/<dataset>/<version>/<UTC start>-live-<split>/`, committed;
dry: the same under the git-ignored `evals/reports/tmp/`, `-fake-` instead of `-live-`):

- `run.json` (`RunInfo`): provenance and config: dataset name, version, stage and freeze.json's
  sha256, the split, the commit and whether the tree or the dataset differed from it, the embedding
  and answer models (configured and answered), the catalog and prompt versions, the chunk config,
  search k and metric k, `rag_min_score`, the eval namespaces, the interval settings, the cost,
  timestamps, `dry_run` and the status.
- `results.jsonl` (`scoring.QuestionResult`): one line per selected question, written as the run
  goes: ids, labels, the ranked hits (no text) with the groups each finds, H2's values, the
  outcome, sources and trace id. No question, chunk or answer text.
- `answers.jsonl` (`scoring.AnswerLine`): the system's reply per question that ran, the only file
  with model text (the answer), for labelling. No question text: the dataset holds it, pinned by
  freeze.json.
- `labels.jsonl`: one template line per question that ran, `label: null`, for shesha to fill in
  (`correct` | `incorrect`, `labelled_by`, `labelled_on`); `evalset.questions.CorrectnessLabel`.
- `embedding_call_log.jsonl`, `llm_call_log.jsonl`, `rag_answer_log.jsonl`: the product's own
  metadata-only records of every call (ADR 0007, 0008, 0011), joined to results by `trace_id`.
- `summary.md`, `summary.json`: written by `summarize`, which reads only the files above.

**`summarize` recomputes every number** from results.jsonl through H2's functions (passed in,
default `scoring.H2_METRICS`): it rebuilds each scored question's `RankedRelevance` from its hits'
groups, calls H2 per question and for the means, and refuses a file whose stored per-question
values differ (an edited file, or an H2 that changed). It also checks that H2's `mean_hit_at_k`
times n equals the count of H2's `hit_at_k`, that H2's `mean_reciprocal_rank` times n equals the
sum of H2's per-question `reciprocal_rank`, and that every first relevant rank lies in 1..k.
Intervals: Wilson for every `x of n`, a seeded percentile bootstrap over questions for MRR
(`intervals.py`; approximate at small n, where it covers less than 95 %, and never past the
observed values, so an interval of zero width is printed as degenerate, never as a 95 %
interval). Breakdowns: per business, per language and per category, each retrieval row with its
unscorable count, and a conservative hit@k that counts every unscorable question as a miss.
Safety (and any row of one category, `safety`) is counts only, never a rate: M2 has no safety
check (ADR 0011), and business-context §6's gate is M7's. A point percentage is shown only from
`RATE_MIN_N` questions, on every split; an MRR value and its bootstrap interval only from
`BOOTSTRAP_MIN_N` (below it the row shows the first-relevant-rank counts only). Correctness comes
only from filled labels.

**§6 targets** (hit@5 and correctness, ≥ 90 %, business-context §6): a target is *met* when the
point estimate reaches it, always shown with its Wilson interval, and "not shown at 95 %
confidence" when the interval's lower end is below it; no verdict below `RATE_MIN_N`, for a dry,
partial or failed run, or while answers are unlabelled. Per business too: a release needs every
business to meet it. These choices were decided by shesha on 2026-10-22 (ADR 0013).

**"Synthetic"** comes from the dataset manifest and the corpus manifests (run.json records both
flags), never from chunk text: the summary's data sentence and the CLI's plan say "NOT synthetic"
when either flag is false.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from vectapilot_api.evalrun.budget import usd
from vectapilot_api.evalrun.intervals import (
    DEFAULT_CONFIDENCE,
    bootstrap_interval,
    wilson_interval,
)
from vectapilot_api.evalrun.scoring import (
    AUDIENCE_LEAK,
    H2_METRICS,
    AnswerLine,
    MetricFunctions,
    QuestionResult,
    check_metric_k,
    first_relevant_rank,
)
from vectapilot_api.evalset.corpus import describe_validation_error
from vectapilot_api.evalset.questions import CATEGORIES, CorrectnessLabel, EvalQuestion
from vectapilot_api.evalset.ranking import RankedRelevance
from vectapilot_api.gateway.types import Usd

RUN_FILE: Final = "run.json"
RESULTS_FILE: Final = "results.jsonl"
ANSWERS_FILE: Final = "answers.jsonl"
LABELS_FILE: Final = "labels.jsonl"
SUMMARY_MD_FILE: Final = "summary.md"
SUMMARY_JSON_FILE: Final = "summary.json"
EMBEDDING_LOG_FILE: Final = "embedding_call_log.jsonl"
LLM_LOG_FILE: Final = "llm_call_log.jsonl"
ANSWER_LOG_FILE: Final = "rag_answer_log.jsonl"

RATE_MIN_N: Final = 10
"""A point percentage next to `x of n` only from this n, on every split (decided 2026-10-22)."""
BOOTSTRAP_MIN_N: Final = 10
"""An MRR value and its bootstrap interval only from this n: below it, the resamples are too few
distinct sets to say much, and a percentile bootstrap covers less than 95 % at small n anyway
(decided 2026-10-22: 10, with the summary's "approximate at small n" note). The
first-relevant-rank counts are always shown."""
BOOTSTRAP_NOTE: Final = "approximate at small n (percentile bootstrap undercovers)"
"""Printed with every MRR interval's explanation in summary.md (decided 2026-10-22)."""
TARGETS: Final = {"hit@5": Fraction(9, 10), "correctness": Fraction(9, 10)}
"""Business-context §6's starting targets that M2 measures (hit@5, correctness): ≥ 90 %."""
TARGET_HIT_K: Final = 5
"""The k of §6's retrieval target: a run with another metric k gets no hit@5 verdict."""
DEFAULT_BOOTSTRAP_SEED: Final = 20261022
"""Fixed before any run (2026-10-22) and recorded in run.json; never changed to move an interval."""
DEFAULT_RESAMPLES: Final = 2000

RETRIEVAL_ROWS: Final = frozenset({"scored", "unscorable"})
"""The retrieval statuses a breakdown row accounts for: scored (its n) and unscorable (shown)."""
COUNTS_ONLY_CATEGORIES: Final = frozenset({"safety"})
"""Rows reported as counts only, never a rate (ADR 0012, decision 7)."""
EVIDENCE_COMPLETE_CATEGORIES: Final = ("multi_hop", "ambiguous")

RunStatus = Literal["complete", "partial", "aborted_leak", "failed"]
SplitName = Literal["dev", "test", "unassigned"]


class SummaryError(ValueError):
    """A run folder cannot be summarised: a missing or invalid file, or numbers that disagree."""


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


# ── run.json ─────────────────────────────────────────────────────────────────────────────────


class ConfigInfo(_Closed):
    max_chars: int
    overlap_chars: int
    search_k: int
    """The k passed to the search (`rag_context_k`)."""
    metric_k: int
    """hit@k and MRR@k's k; at most `search_k` (asserted)."""
    rag_min_score: float
    weak_evidence_outcome: str
    llm_max_retries: int
    llm_max_repairs: int

    @model_validator(mode="after")
    def _metric_k_within_search_k(self) -> "ConfigInfo":
        check_metric_k(self.metric_k, self.search_k)  # also when read back from run.json
        return self


class CostInfo(_Closed):
    cap_usd: Usd | None
    embedding_usd: Usd
    llm_usd: Usd
    total_usd: Usd
    llm_cost_complete: bool
    """False: `llm_usd` (and the total) is a lower bound (a call's cost was incomplete)."""
    embedding_requests: int
    llm_calls: int


class IngestInfo(_Closed):
    business_id: str
    eval_business_id: str
    documents: int
    created: int
    chunks_written: int
    retired_versions: int


class RunInfo(_Closed):
    """`run.json`: what the run was, so every number can be traced and reproduced."""

    record_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    status: RunStatus
    stop: str | None
    problems: tuple[str, ...] = ()
    dry_run: bool
    environment: str
    dataset: str
    dataset_version: str
    dataset_stage: str
    dataset_synthetic: bool
    """The dataset manifest's `synthetic` flag (always true in v0.1)."""
    corpus_synthetic: bool
    """True when every corpus manifest of the dataset's businesses says `synthetic: true`."""
    freeze_sha256: str | None
    """sha256 of freeze.json's bytes; None for a dataset that is not frozen."""
    dataset_committed: bool
    """The dataset's and corpus's files are, byte for byte, those in `commit`."""
    corpus_review_pending: int
    split: SplitName
    questions_selected: int
    commit: str
    uncommitted_changes: bool
    started_at: AwareDatetime
    ended_at: AwareDatetime
    embeddings_provider: str
    embedding_model: str
    embedding_models_answered: tuple[str, ...]
    llm_provider: str
    answer_alias: str
    answer_model: str
    answer_models_answered: tuple[str, ...]
    catalog_version: str
    prompt_version: str
    config: ConfigInfo
    config_fingerprint: str
    namespaces: dict[str, str]
    confidence: str
    bootstrap_seed: int
    bootstrap_resamples: int
    rate_min_n: int
    bootstrap_min_n: int
    cost: CostInfo
    ingest: tuple[IngestInfo, ...]
    test_rerun_reason: str | None = None
    test_runs_before_config: int = Field(default=0, ge=0)
    """Test-run ledger entries before this run with the same config fingerprint."""
    test_runs_before_freeze: int = Field(default=0, ge=0)
    """Test-run ledger entries before this run on the same frozen version (any config)."""


def fingerprint_payload(
    *,
    dataset: str,
    version: str,
    freeze_sha256: str | None,
    embedding_model: str,
    answer_model: str,
    catalog_version: str,
    prompt_version: str,
    config: ConfigInfo,
) -> dict[str, object]:
    """Everything that defines what a run measures (not the commit, the seed or the cap), as the
    JSON `config_fingerprint` hashes; `compare` lists the fields where two runs differ."""
    return {
        "dataset": dataset,
        "version": version,
        "freeze_sha256": freeze_sha256,
        "embedding_model": embedding_model,
        "answer_model": answer_model,
        "catalog_version": catalog_version,
        "prompt_version": prompt_version,
        "config": config.model_dump(mode="json"),
    }


def fingerprint_digest(payload: Mapping[str, object]) -> str:
    """sha256 of a fingerprint payload's canonical JSON."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def config_fingerprint(
    *,
    dataset: str,
    version: str,
    freeze_sha256: str | None,
    embedding_model: str,
    answer_model: str,
    catalog_version: str,
    prompt_version: str,
    config: ConfigInfo,
) -> str:
    """sha256 of everything that defines what a run measures (`fingerprint_payload`): "test run
    once per reported config" is checked against it."""
    return fingerprint_digest(
        fingerprint_payload(
            dataset=dataset,
            version=version,
            freeze_sha256=freeze_sha256,
            embedding_model=embedding_model,
            answer_model=answer_model,
            catalog_version=catalog_version,
            prompt_version=prompt_version,
            config=config,
        )
    )


def run_fingerprint_payload(run: RunInfo) -> dict[str, object]:
    """The fingerprint payload of a run, from its run.json fields."""
    return fingerprint_payload(
        dataset=run.dataset,
        version=run.dataset_version,
        freeze_sha256=run.freeze_sha256,
        embedding_model=run.embedding_model,
        answer_model=run.answer_model,
        catalog_version=run.catalog_version,
        prompt_version=run.prompt_version,
        config=run.config,
    )


# ── Labels ───────────────────────────────────────────────────────────────────────────────────


def label_template(run_id: str, results: Iterable[QuestionResult]) -> str:
    """labels.jsonl as written by a run: one line per question that ran, nothing filled in."""
    lines = [
        json.dumps(
            {
                "question_id": result.question_id,
                "run_id": run_id,
                "label": None,
                "labelled_by": None,
                "labelled_on": None,
                "note": None,
            }
        )
        for result in results
        if result.outcome != "not_run"
    ]
    return "".join(f"{line}\n" for line in lines)


def parse_labels(
    text: str, ran: Collection[str], run_id: str
) -> tuple[dict[str, CorrectnessLabel], frozenset[str]]:
    """The filled labels by question id, and the questions that ran but have none yet (a
    `label: null` line or no line). Raises `SummaryError` naming the line: not JSON, a question
    that did not run in this run, a second line for one question, another run's id, or a filled
    line that is not a valid `CorrectnessLabel`. Never echoes a value."""
    labels: dict[str, CorrectnessLabel] = {}
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            raise SummaryError(f"{LABELS_FILE} line {number} is not a JSON object")
        question_id = payload.get("question_id")
        if not isinstance(question_id, str) or question_id not in ran:
            raise SummaryError(f"{LABELS_FILE} line {number}: not a question this run answered")
        if question_id in seen:
            raise SummaryError(f"{LABELS_FILE} line {number}: one label per question and run")
        seen.add(question_id)
        if payload.get("label") is None:
            continue
        label = _label(payload, number)
        if label.run_id != run_id:
            raise SummaryError(f"{LABELS_FILE} line {number}: run_id is not this run's")
        labels[question_id] = label
    return labels, frozenset(set(ran) - labels.keys())


def _label(payload: dict[str, object], number: int) -> CorrectnessLabel:
    try:
        return CorrectnessLabel.model_validate(payload)
    except ValidationError as error:
        detail = describe_validation_error(error, "(row)")
    raise SummaryError(f"{LABELS_FILE} line {number} is not a valid label: {detail}")


# ── The summary ──────────────────────────────────────────────────────────────────────────────


class CountStat(_Closed):
    """`x of n`, with its Wilson interval unless reported as counts only."""

    x: int
    n: int
    low: float | None = None
    high: float | None = None


class MeanStat(_Closed):
    """H2's MRR@k for a group: exact value, its bootstrap interval (when n allows), and how many
    questions had their first relevant hit at each rank (`none`: no relevant hit in the top k)."""

    value: str | None
    n: int
    low: str | None
    high: str | None
    ranks: dict[str, int]


class MetricRow(_Closed):
    group: str
    counts_only: bool
    hit: CountStat
    """`hit.n` is the group's scored questions."""
    mrr: MeanStat
    unscorable: int = 0
    """The group's questions unscorable at this config: outside `hit.n`, never dropped."""
    conservative: CountStat
    """hit@k with every unscorable question counted as a miss: `hit.x` of `hit.n + unscorable`
    (decided 2026-10-22), so a chunk config cannot look better by making questions unscorable."""


class CorrectnessRow(_Closed):
    group: str
    counts_only: bool
    correct: CountStat


class TargetRow(_Closed):
    """One §6 target for one group ("all" or a business): the measured `x of n` and its verdict."""

    metric: str
    group: str
    target: str
    """The target as an exact fraction ("9/10")."""
    stat: CountStat
    verdict: str
    """"met", "not met", or "no verdict: <why>" (module docstring)."""
    shown_at_95: bool | None
    """Whether the Wilson interval's lower end reaches the target; None without a verdict."""


class Summary(_Closed):
    run: RunInfo
    status_line: str
    not_for_note: tuple[str, ...]
    selected: int
    scored: int
    not_scored: tuple[str, ...]
    unscorable: tuple[str, ...]
    not_run: tuple[str, ...]
    answer_errors: tuple[str, ...]
    shared_split_group: int
    """Questions that share a `split_group` with another one in the run (not independent)."""
    retrieval: tuple[MetricRow, ...]
    all_groups: CountStat | None
    """Questions with 2+ groups whose every group is in the top k (H2's `all_groups_at_k`)."""
    evidence_complete: dict[str, CountStat]
    evidence_not_counted: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    """Per category: scored questions left out of `evidence_complete` because their answer step
    did not finish (no prompt evidence to check), by id."""
    unheld_keys: int = 0
    """Evidence keys of scored questions that no stored chunk holds at this config (each in a
    group another key still finds)."""
    unheld_key_questions: tuple[str, ...] = ()
    outcomes: dict[str, dict[str, int]]
    """expected outcome → {actual outcome: count}, over the questions that ran."""
    reasons: dict[str, int]
    safety_handoff: dict[str, CountStat]
    """Per business: safety questions that ran, and how many ended `handoff` (counts only)."""
    labelled: int
    unlabelled: int
    correctness: tuple[CorrectnessRow, ...]
    targets: tuple[TargetRow, ...] = ()


def read_results(run_dir: Path) -> list[QuestionResult]:
    path = run_dir / RESULTS_FILE
    if not path.is_file():
        raise SummaryError(f"{RESULTS_FILE} is missing")
    results: list[QuestionResult] = []
    for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            results.append(QuestionResult.model_validate_json(line))
        except ValidationError as error:
            detail = describe_validation_error(error, "(row)")
            raise SummaryError(f"{RESULTS_FILE} line {number} is invalid: {detail}") from None
    return results


def read_run(run_dir: Path) -> RunInfo:
    path = run_dir / RUN_FILE
    if not path.is_file():
        raise SummaryError(f"{RUN_FILE} is missing")
    try:
        return RunInfo.model_validate_json(path.read_text("utf-8"))
    except ValidationError as error:
        detail = describe_validation_error(error, "(run)")
    raise SummaryError(f"{RUN_FILE} is invalid: {detail}")


def read_answers(run_dir: Path) -> list[AnswerLine]:
    path = run_dir / ANSWERS_FILE
    if not path.is_file():
        return []
    return [
        AnswerLine.model_validate_json(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def _relabelled(sample: Sequence[RankedRelevance]) -> list[RankedRelevance]:
    """A bootstrap resample, each record renamed `<id>~<position>`: a question drawn twice is two
    entries of the resample, and H2's means refuse a repeated id (`ranking.check_results`)."""
    return [
        RankedRelevance(
            question_id=f"{record.question_id}~{position}",
            n_groups=record.n_groups,
            groups_by_rank=record.groups_by_rank,
        )
        for position, record in enumerate(sample)
    ]


def _check_stored(result: QuestionResult, k: int, search_k: int, f: MetricFunctions) -> None:
    """H2's values recomputed from the file equal the ones the run stored."""
    record = result.ranked()
    recomputed: list[bool | str | None] = [
        f.hit_at_k(record, k),
        f.all_groups_at_k(record, k),
        str(f.reciprocal_rank(record, k)),
    ]
    stored: list[bool | str | None] = [
        result.hit_at_k,
        result.all_groups_at_k,
        result.reciprocal_rank,
    ]
    evidence = result.ranked_evidence()
    if evidence is not None:
        recomputed.append(f.all_groups_at_k(evidence, search_k))
        stored.append(result.evidence_complete)
    if recomputed != stored:
        raise SummaryError(
            f"question {result.question_id}: the stored metric values differ from H2's on its"
            f" ranked groups ({RESULTS_FILE} edited, or H2 changed since the run)"
        )


def _metric_row(
    group: str,
    results: Sequence[QuestionResult],
    run: RunInfo,
    functions: MetricFunctions,
    *,
    unscorable: int = 0,
    k: int | None = None,
    counts_only: bool | None = None,
) -> MetricRow:
    """One retrieval row: H2's values for the group's scored `results`, cross-checked, with
    their intervals; `unscorable` is the group's count of questions unscorable at this config.
    `k` defaults to the run's metric k and `counts_only` to the category rule (safety); `compare`
    passes k 5 and, on the test split, counts only."""
    k = run.config.metric_k if k is None else k
    if counts_only is None:
        counts_only = group.removeprefix("category ") in COUNTS_ONLY_CATEGORIES
    records = [result.ranked() for result in results]
    n = len(records)
    if n == 0:  # only unscorable questions in this group: nothing to score, nothing hidden
        empty = MeanStat(value=None, n=0, low=None, high=None, ranks={})
        return MetricRow(
            group=group,
            counts_only=counts_only,
            hit=CountStat(x=0, n=0),
            mrr=empty,
            unscorable=unscorable,
            conservative=_stat(0, unscorable, counts_only=counts_only),
        )
    x = sum(functions.hit_at_k(record, k) for record in records)
    if functions.mean_hit_at_k(records, k) * n != x:
        raise SummaryError(f"{group}: H2's mean_hit_at_k disagrees with its hit_at_k count")
    hit = _stat(x, n, counts_only=counts_only)
    rrs = [functions.reciprocal_rank(record, k) for record in records]
    ranks: Counter[str] = Counter()
    for rr in rrs:
        rank = first_relevant_rank(rr)
        ranks["none" if rank is None else str(rank)] += 1
    ordered = {str(r): ranks[str(r)] for r in range(1, k + 1)} | {"none": ranks["none"]}
    if sum(ordered.values()) != n:
        raise SummaryError(f"{group}: a first relevant rank lies outside 1..{k} (H2's RR@{k})")
    value = low = high = None
    if not counts_only:
        mrr = functions.mean_reciprocal_rank(records, k)
        if mrr * n != sum(rrs, Fraction(0)):
            raise SummaryError(
                f"{group}: H2's mean_reciprocal_rank disagrees with its reciprocal_rank values"
            )
        value = str(mrr)
        if n >= run.bootstrap_min_n:
            interval = bootstrap_interval(
                records,
                lambda sample: functions.mean_reciprocal_rank(_relabelled(sample), k),
                seed=run.bootstrap_seed,
                resamples=run.bootstrap_resamples,
                confidence=DEFAULT_CONFIDENCE,
            )
            low, high = str(interval.low), str(interval.high)
    return MetricRow(
        group=group,
        counts_only=counts_only,
        hit=hit,
        mrr=MeanStat(value=value, n=n, low=low, high=high, ranks=ordered),
        unscorable=unscorable,
        conservative=_stat(x, n + unscorable, counts_only=counts_only),
    )


def _groups(results: Sequence[QuestionResult]) -> list[tuple[str, list[QuestionResult]]]:
    """("all", every result), then per business, per language and per category (that occur)."""
    by: dict[str, dict[str, list[QuestionResult]]] = {
        "business": defaultdict(list),
        "language": defaultdict(list),
        "category": defaultdict(list),
    }
    for result in results:
        by["business"][result.business_id].append(result)
        by["language"][result.language_group].append(result)
        by["category"][result.category].append(result)
    order: list[str] = list(CATEGORIES)
    groups = [("all", list(results))]
    groups += [(f"business {b}", by["business"][b]) for b in sorted(by["business"])]
    groups += [(f"language {lang}", by["language"][lang]) for lang in sorted(by["language"])]
    groups += [
        (f"category {c}", by["category"][c]) for c in sorted(by["category"], key=order.index)
    ]
    return groups


def _stat(x: int, n: int, *, counts_only: bool = False) -> CountStat:
    """`x of n`, with its 95 % Wilson interval unless counts only or empty."""
    if counts_only or n == 0:
        return CountStat(x=x, n=n)
    wilson = wilson_interval(x, n, DEFAULT_CONFIDENCE)
    return CountStat(x=x, n=n, low=wilson.low, high=wilson.high)


def _count(flags: Sequence[bool], *, counts_only: bool = False) -> CountStat:
    return _stat(sum(flags), len(flags), counts_only=counts_only)


def _no_verdict(run: RunInfo, unlabelled: int, metric: str) -> str | None:
    """Why a §6 target gets no verdict in this run at all, or None."""
    if run.dry_run:
        return "no verdict: dry run"
    if run.status != "complete":
        return f"no verdict: status {run.status}"
    if metric == "hit@5" and run.config.metric_k != TARGET_HIT_K:
        return f"no verdict: this run's metric k is {run.config.metric_k}, the target is hit@5"
    if metric == "correctness" and unlabelled:
        return f"no verdict: {unlabelled} answer(s) not labelled yet"
    return None


def _target_row(
    metric: str, group: str, stat: CountStat, run: RunInfo, unlabelled: int
) -> TargetRow:
    """One §6 target's verdict for one group (module docstring)."""
    target = TARGETS[metric]
    why = _no_verdict(run, unlabelled, metric)
    if why is None and stat.n == 0:
        why = "no verdict: none scored" if metric == "hit@5" else "no verdict: none labelled"
    if why is None and stat.n < run.rate_min_n:
        why = f"no verdict: n < {run.rate_min_n} (counts only)"
    if why is not None:
        return TargetRow(
            metric=metric, group=group, target=str(target), stat=stat, verdict=why, shown_at_95=None
        )
    met = Fraction(stat.x, stat.n) >= target  # exact: the point estimate decides
    # The one place an interval end is compared: with 0 < x it is irrational, so a float's last
    # bit cannot decide it; at x = 0 it is exactly 0.
    low = wilson_interval(stat.x, stat.n, DEFAULT_CONFIDENCE).low
    return TargetRow(
        metric=metric,
        group=group,
        target=str(target),
        stat=stat,
        verdict="met" if met else "not met",
        shown_at_95=low >= float(target),
    )


def _targets(
    retrieval: Sequence[MetricRow],
    correctness: Sequence[CorrectnessRow],
    run: RunInfo,
    unlabelled: int,
) -> tuple[TargetRow, ...]:
    """hit@5, then correctness, against §6: for "all" and each business of the run's dataset."""
    groups = ["all", *(f"business {business}" for business in sorted(run.namespaces))]
    hits = {row.group: row.hit for row in retrieval}
    correct = {row.group: row.correct for row in correctness}
    empty = CountStat(x=0, n=0)
    rows = [_target_row("hit@5", g, hits.get(g, empty), run, unlabelled) for g in groups]
    rows += [_target_row("correctness", g, correct.get(g, empty), run, unlabelled) for g in groups]
    return tuple(rows)


def status_of(run: RunInfo, unlabelled: int) -> tuple[str, tuple[str, ...]]:
    """The status line, and every reason this run cannot feed the "what I measured" note.

    The reasons come first: a complete live test run says it may feed the note only when there
    are none (M1's rule: the status line decides, so it never contradicts "Not for the note"). A
    complete run with a stop (a stop rule its last question tripped) is never for the note. A
    test rerun says so, with its reason. Every line of a dry run starts with DRY RUN."""
    reasons: list[str] = []
    if run.dry_run:
        reasons.append("dry run: fake providers, never a measurement")
    if run.status != "complete":
        reasons.append(f"status {run.status}")
    elif run.stop is not None:  # every question ran, but the last one tripped a stop rule
        reasons.append(f"stopped by its last question: {run.stop}")
    if run.split != "test":
        reasons.append(f"split {run.split}: only the held-out test split is reported")
    if run.dataset_stage != "frozen":
        reasons.append("the dataset is not frozen")
    if run.corpus_review_pending:
        reasons.append(f"{run.corpus_review_pending} corpus document(s) still review: pending")
    if run.uncommitted_changes:
        reasons.append("uncommitted changes: the commit does not name the code that ran")
    if not run.dataset_committed:
        reasons.append("dataset or corpus not as committed")
    if unlabelled:
        reasons.append(f"correctness: {unlabelled} answer(s) not labelled yet")
    if run.status == "failed":
        line = f"FAILED: {run.stop}. No number in this report is a result."
    elif run.status == "aborted_leak":
        if run.stop is not None and run.stop.startswith(AUDIENCE_LEAK):
            what = "a document no customer may see was retrieved (business-context §3.4)"
        else:
            what = "cross-business leak, a hard gate (business-context §6)"
        line = f"ABORTED: {what}: {run.stop}. No number in this report is a result."
    elif run.status == "partial":
        line = f"PARTIAL {'run' if run.dry_run else 'live run'}, not a result: {run.stop}."
    elif run.dry_run:
        line = (
            "DRY RUN: fake embedder and canned fake answers; no number in this report is a result."
        )
    elif run.split == "test":
        line = _test_line(run, reasons)
    else:
        line = "Complete live run on the dev split: tuning numbers, not the held-out result."
    if run.dry_run and not line.startswith("DRY RUN"):
        line = f"DRY RUN · {line}"
    return line, tuple(reasons)


def _test_line(run: RunInfo, reasons: Sequence[str]) -> str:
    """A complete live test run's status line (`status_of`)."""
    if run.test_rerun_reason is not None:
        what = f"held-out test RERUN (reason: {run.test_rerun_reason})"
    else:
        what = "held-out test run"
    if reasons:
        return f"{what[0].upper()}{what[1:]}, not for the note yet: {'; '.join(reasons)}."
    line = f"Complete live {what}: may feed the note (config {run.config_fingerprint[:12]})"
    if run.test_runs_before_freeze:
        line += "; the note cites every test-run ledger entry of this frozen version"
    return line + "."


def summarize(run_dir: Path, functions: MetricFunctions = H2_METRICS) -> Summary:
    """The summary of a run folder, recomputed from its files (module docstring)."""
    run = read_run(run_dir)
    results = read_results(run_dir)
    if len(results) != run.questions_selected:
        raise SummaryError(
            f"{RESULTS_FILE} has {len(results)} line(s), run.json says"
            f" {run.questions_selected} question(s) were selected"
        )
    ran = [r for r in results if r.outcome != "not_run"]
    labels_path = run_dir / LABELS_FILE
    labels_text = labels_path.read_text("utf-8") if labels_path.is_file() else ""
    labels, unlabelled = parse_labels(labels_text, {r.question_id for r in ran}, run.run_id)
    scored = [r for r in results if r.retrieval == "scored"]
    for result in scored:
        _check_stored(result, run.config.metric_k, run.config.search_k, functions)
    # Groups over scored and unscorable questions, so each row shows what it left out.
    retrieval = tuple(
        _metric_row(
            group,
            [r for r in members if r.retrieval == "scored"],
            run,
            functions,
            unscorable=sum(r.retrieval == "unscorable" for r in members),
        )
        for group, members in _groups([r for r in results if r.retrieval in RETRIEVAL_ROWS])
        if members
    )
    multi = [r for r in scored if r.n_groups >= 2]
    all_groups = (
        _count([functions.all_groups_at_k(r.ranked(), run.config.metric_k) for r in multi])
        if multi
        else None
    )
    evidence_complete: dict[str, CountStat] = {}
    evidence_not_counted: dict[str, tuple[str, ...]] = {}
    for category in EVIDENCE_COMPLETE_CATEGORIES:
        members = [r for r in scored if r.category == category]
        flags = [
            functions.all_groups_at_k(evidence, run.config.search_k)
            for r in members
            if (evidence := r.ranked_evidence()) is not None
        ]
        if members:
            evidence_complete[category] = _count(flags, counts_only=True)
            evidence_not_counted[category] = tuple(
                r.question_id for r in members if r.evidence_chunk_ids is None
            )
    outcomes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for result in ran:
        outcomes[result.expected_outcome][result.outcome] += 1
    safety: dict[str, list[bool]] = defaultdict(list)
    for result in ran:
        if result.category == "safety":
            safety[result.business_id].append(result.outcome == "handoff")
    correctness = tuple(
        CorrectnessRow(
            group=group,
            counts_only=group.removeprefix("category ") in COUNTS_ONLY_CATEGORIES,
            correct=_count(
                [labels[r.question_id].label == "correct" for r in members],
                counts_only=group.removeprefix("category ") in COUNTS_ONLY_CATEGORIES,
            ),
        )
        for group, members in _groups([r for r in ran if r.question_id in labels])
        if members
    )
    line, reasons = status_of(run, len(unlabelled))
    shared = Counter(r.split_group for r in results if r.split_group is not None)
    return Summary(
        run=run,
        status_line=line,
        not_for_note=reasons,
        selected=len(results),
        scored=len(scored),
        not_scored=tuple(r.question_id for r in results if r.retrieval == "not_scored"),
        unscorable=tuple(r.question_id for r in results if r.retrieval == "unscorable"),
        not_run=tuple(r.question_id for r in results if r.outcome == "not_run"),
        answer_errors=tuple(r.question_id for r in results if r.outcome == "error"),
        shared_split_group=sum(n for n in shared.values() if n > 1),
        retrieval=retrieval,
        all_groups=all_groups,
        evidence_complete=evidence_complete,
        evidence_not_counted=evidence_not_counted,
        unheld_keys=sum(len(r.unheld_keys) for r in scored),
        unheld_key_questions=tuple(r.question_id for r in scored if r.unheld_keys),
        outcomes={e: dict(got) for e, got in sorted(outcomes.items())},
        reasons=dict(sorted(Counter(r.reason for r in ran if r.reason).items())),
        safety_handoff={b: _count(f, counts_only=True) for b, f in sorted(safety.items())},
        labelled=len(labels),
        unlabelled=len(unlabelled),
        correctness=correctness,
        targets=_targets(retrieval, correctness, run, len(unlabelled)),
    )


# ── Rendering ────────────────────────────────────────────────────────────────────────────────


def _pct(value: float) -> str:
    return f"{value * 100:.0f} %"


def _count_text(stat: CountStat, min_n: int) -> str:
    if stat.n == 0:
        return "none scored"
    text = f"{stat.x} of {stat.n}"
    if stat.low is None or stat.high is None:
        return text + " (counts only)"
    point = f", {_pct(stat.x / stat.n)}" if stat.n >= min_n else ""
    return f"{text}{point} (95 % Wilson {_pct(stat.low)} to {_pct(stat.high)})"


def _mrr_text(stat: MeanStat, min_n: int, k: int) -> str:
    """MRR@k as shown: no value below `min_n` (the rank counts carry the row), and a bootstrap
    interval of zero width called degenerate, never a 95 % interval (module docstring)."""
    if stat.n == 0:
        return "none scored"
    if stat.value is None:
        return "counts only"
    if stat.n < min_n:
        return f"n < {min_n}: no MRR value; see first relevant rank"
    text = f"{float(Fraction(stat.value)):.3f}"
    if stat.low is None or stat.high is None:  # (summarize always sets both from min_n)
        return f"{text} (no interval)"
    low, high = Fraction(stat.low), Fraction(stat.high)
    if low == high:
        return (
            f"{text} (bootstrap degenerate: its 2.5th and 97.5th percentiles are both"
            f" {float(low):.3f}, no interval; see the hit@{k} Wilson interval)"
        )
    return f"{text} (95 % bootstrap {float(low):.3f} to {float(high):.3f})"


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows]
    return lines


def _ids(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "none"


def _ledger_row(run: RunInfo) -> list[tuple[str, str]]:
    """The Run table's test-run ledger row (test runs only)."""
    if run.split != "test":
        return []
    return [
        (
            "Test-run ledger",
            f"{run.test_runs_before_config} earlier test run(s) of this config,"
            f" {run.test_runs_before_freeze} of this frozen version (any config); rerun reason:"
            f" {run.test_rerun_reason or 'none'}",
        )
    ]


def render_markdown(summary: Summary) -> str:
    run = summary.run
    config = run.config
    k = config.metric_k
    is_synthetic = run.dataset_synthetic and run.corpus_synthetic
    synthetic = "yes" if is_synthetic else "NO"
    flags = (
        f"(dataset manifest `synthetic: {str(run.dataset_synthetic).lower()}`, corpus manifests"
        f" `synthetic: {str(run.corpus_synthetic).lower()}`)"
    )
    data_sentence = (
        f"Synthetic data: human-written questions about fictional businesses {flags}. These"
        " numbers describe the system on this data, not real customers."
        if is_synthetic
        else f"NOT synthetic {flags}: this run's data is not marked synthetic in its manifests."
    )
    answered_models = (
        "none (canned dry-run reply)"
        if run.dry_run
        else ", ".join(run.answer_models_answered) or "none"
    )
    cost = run.cost
    cost_text = (
        f"embeddings {usd(cost.embedding_usd)} ({cost.embedding_requests} requests) · answer"
        f" model {usd(cost.llm_usd)} ({cost.llm_calls} calls"
        + ("" if cost.llm_cost_complete else ", a lower bound: a call's cost was incomplete")
        + f") · total {usd(cost.total_usd)} · cap "
        + ("none" if cost.cap_usd is None else usd(cost.cap_usd))
    )
    if run.dry_run:
        cost_text += " (dry run: fake usage, not a cost)"
    lines = [
        f"# Eval run {run.run_id}",
        "",
        f"**{summary.status_line}**",
        "",
        data_sentence,
        "",
        "## Run",
        "",
        *_table(
            ["", ""],
            [
                ("Status", f"{run.status}" + (f": {run.stop}" if run.stop else "")),
                (
                    "Dataset",
                    f"{run.dataset} {run.dataset_version} (stage {run.dataset_stage}; freeze.json"
                    f" sha256 {run.freeze_sha256 or 'none'}; files as committed:"
                    f" {'yes' if run.dataset_committed else 'no'}; synthetic: {synthetic})",
                ),
                ("Split", f"{run.split} ({run.questions_selected} questions selected)"),
                (
                    "Commit",
                    f"{run.commit} (uncommitted changes:"
                    f" {'yes' if run.uncommitted_changes else 'no'})",
                ),
                (
                    "Embeddings",
                    f"{run.embeddings_provider} · {run.embedding_model} (answered:"
                    f" {', '.join(run.embedding_models_answered) or 'none'})",
                ),
                (
                    "Answer model",
                    f"{run.llm_provider} · {run.answer_alias} → {run.answer_model} (answered:"
                    f" {answered_models}) · catalog {run.catalog_version} · prompt"
                    f" {run.prompt_version}",
                ),
                (
                    "Config",
                    f"chunks {config.max_chars}/{config.overlap_chars} · search k"
                    f" {config.search_k} · metric k {config.metric_k} · rag_min_score"
                    f" {config.rag_min_score} · weak evidence → {config.weak_evidence_outcome} ·"
                    f" retries {config.llm_max_retries}, repairs {config.llm_max_repairs}",
                ),
                ("Config fingerprint", run.config_fingerprint),
                (
                    "Eval businesses",
                    "; ".join(f"{b} → {e}" for b, e in sorted(run.namespaces.items())),
                ),
                (
                    "Intervals",
                    f"95 % Wilson for counts; percentile bootstrap over questions for MRR,"
                    f" {run.bootstrap_resamples} resamples, seed {run.bootstrap_seed}, with the"
                    f" MRR value from n = {run.bootstrap_min_n}: {BOOTSTRAP_NOTE}, and never"
                    f" past the observed values; a percentage from n = {run.rate_min_n}, on"
                    " every split",
                ),
                *_ledger_row(run),
                ("Started / ended", f"{run.started_at.isoformat()} / {run.ended_at.isoformat()}"),
                ("Cost (list prices)", cost_text),
                ("Not for the note", "; ".join(summary.not_for_note) or "nothing: it may feed it"),
            ],
        ),
        "",
        f"## Retrieval: hit@{k} and MRR@{k} (search k = {config.search_k})",
        "",
        f"On the search's ranked list, before the score threshold. {summary.selected} selected ·"
        f" {summary.scored} scored · {len(summary.not_scored)} not scored (no evidence) ·"
        f" {len(summary.unscorable)} unscorable at this config · {len(summary.not_run)} not run."
        " n counts each group's scored questions; its unscorable ones are outside n, shown next"
        " to it.",
        "",
    ]
    if summary.retrieval:
        lines += _table(
            ["Group", "n", "Unscorable", f"hit@{k}", f"MRR@{k}", "First relevant rank"],
            [
                (
                    row.group,
                    str(row.hit.n),
                    str(row.unscorable),
                    _count_text(row.hit, run.rate_min_n),
                    _mrr_text(row.mrr, run.bootstrap_min_n, k),
                    " · ".join(f"{rank}: {count}" for rank, count in row.mrr.ranks.items()),
                )
                for row in summary.retrieval
            ],
        )
    else:
        lines.append("No question was scored.")
    lines += [
        "",
        "Intervals treat questions as independent; "
        f"{summary.shared_split_group} question(s) share a split_group with another (translations"
        " or twins), so the intervals are too narrow by an unknown amount.",
    ]
    if any(row.mrr.low is not None for row in summary.retrieval):
        lines.append(f"MRR bootstrap intervals: {BOOTSTRAP_NOTE}.")
    lines.append("")
    conservative = [
        f"{row.group} {_count_text(row.conservative, run.rate_min_n)}"
        for row in summary.retrieval
        if row.group == "all" or row.group.startswith("business ")
    ]
    if conservative:
        lines.append(
            f"- Conservative hit@{k}, every unscorable question counted as a miss: "
            + "; ".join(conservative)
        )
    if summary.all_groups is not None:
        lines.append(
            f"- Every group in the top {k} (questions with 2+ groups):"
            f" {_count_text(summary.all_groups, run.rate_min_n)}"
        )
    for category, stat in summary.evidence_complete.items():
        missing = summary.evidence_not_counted.get(category, ())
        gap = (
            f" ({len(missing)} scored {category} question(s) not counted, answer-step error:"
            f" {_ids(missing)})"
            if missing
            else ""
        )
        lines.append(
            f"- Evidence complete, {category} (every group among the hits the prompt held, at"
            f" rag_min_score {config.rag_min_score}; not hit@k): {stat.x} of {stat.n}{gap}"
        )
    lines += [
        f"- Not scored: {_ids(summary.not_scored)}",
        f"- Unscorable at this config: {_ids(summary.unscorable)}",
        f"- Evidence keys not held at this config (their groups are still findable):"
        f" {summary.unheld_keys}, in {_ids(summary.unheld_key_questions)}",
        f"- Not run: {_ids(summary.not_run)}",
        "",
        "## Answers",
        "",
    ]
    got = ("answered", "dont_know", "handoff", "error")
    if summary.outcomes:
        lines += _table(
            ["Expected \\ got", *got],
            [
                (expected, *(str(row.get(name, 0)) for name in got))
                for expected, row in summary.outcomes.items()
            ],
        )
    else:
        lines.append("No question ran.")
    reasons = " · ".join(f"{reason} {count}" for reason, count in summary.reasons.items())
    lines += [
        "",
        f"- Why answers were not shown: {reasons or 'none'}",
        f"- Answer-step errors: {_ids(summary.answer_errors)}",
        "- Safety (counts only, never a rate; M2 has no safety check, ADR 0011): "
        + (
            "; ".join(f"{b} handoff {s.x} of {s.n}" for b, s in summary.safety_handoff.items())
            or "no safety question ran"
        ),
        "",
        "## Correctness (human labels)",
        "",
    ]
    if not summary.correctness:
        lines.append(
            f"Not labelled yet: 0 of {summary.labelled + summary.unlabelled} answers labelled"
            f" ({LABELS_FILE}). Correctness is reported only from human labels."
        )
    else:
        lines.append(
            f"{summary.labelled} labelled, {summary.unlabelled} not yet"
            + (": incomplete, not a result." if summary.unlabelled else ".")
        )
        lines.append("")
        lines += _table(
            ["Group", "Correct"],
            [(row.group, _count_text(row.correct, run.rate_min_n)) for row in summary.correctness],
        )
    lines += ["", *_targets_markdown(summary.targets, run.rate_min_n)]
    return "\n".join(lines) + "\n"


def _verdict_text(row: TargetRow) -> str:
    if row.verdict != "met":
        return row.verdict
    confidence = (
        "the 95 % Wilson lower end reaches it too"
        if row.shown_at_95
        else "not shown at 95 % confidence"
    )
    return f"met (point estimate; {confidence})"


def _targets_markdown(targets: Sequence[TargetRow], rate_min_n: int) -> list[str]:
    lines = [
        "## Targets (business-context §6)",
        "",
        "A target is met when the point estimate reaches it, always read with its 95 % Wilson"
        ' interval; "not shown at 95 % confidence" when the interval\'s lower end is below it.'
        f" No verdict below n = {rate_min_n}, on any split. A release needs every business to"
        " meet it (decided by shesha, 2026-10-22).",
        "",
    ]
    lines += _table(
        ["Metric", "Group", "Target", "Measured", "Verdict"],
        [
            (
                row.metric,
                row.group,
                f"≥ {_pct(float(Fraction(row.target)))}",
                _count_text(row.stat, rate_min_n),
                _verdict_text(row),
            )
            for row in targets
        ],
    )
    return lines


def render_json(summary: Summary) -> str:
    return summary.model_dump_json(indent=2) + "\n"


def render_label_sheet(answers: Sequence[AnswerLine], questions: Mapping[str, EvalQuestion]) -> str:
    """For shesha's labelling, printed to the terminal only (never written or logged): each
    question as asked, what a correct reply does, and what the system replied."""
    blocks: list[str] = []
    for answer in answers:
        question = questions.get(answer.question_id)
        asked = question.question if question is not None else "(not in this dataset)"
        reference = (question.reference_answer if question is not None else None) or "none"
        sources = ", ".join(
            f"{s.source_id} {s.version} chunk {s.chunk_index}" for s in answer.sources
        )
        blocks.append(
            "\n".join(
                [
                    f"── {answer.question_id} ──",
                    f"Question: {asked}",
                    f"Expected outcome: {answer.expected_outcome} · reference answer: {reference}",
                    f"Got: {answer.outcome}"
                    + (f" ({answer.reason})" if answer.reason else "")
                    + (f" [{answer.error}]" if answer.error else ""),
                    f"Answer: {answer.answer if answer.answer is not None else '(no text)'}",
                    f"Sources: {sources or 'none'}",
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_summary(run_dir: Path, summary: Summary) -> str:
    """Write summary.md and summary.json; return the Markdown."""
    markdown = render_markdown(summary)
    (run_dir / SUMMARY_MD_FILE).write_text(markdown, encoding="utf-8")
    (run_dir / SUMMARY_JSON_FILE).write_text(render_json(summary), encoding="utf-8")
    return markdown


ResultsWriter = Callable[[QuestionResult, AnswerLine | None], None]
