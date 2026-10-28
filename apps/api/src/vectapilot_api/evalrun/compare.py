"""`evalrun compare <run A> <run B>`: two runs of one chunk config, paired by question (M3 PR 6).

Reads two run folders (run.json and results.jsonl only) and prints a Markdown report. It writes
nothing: redirect stdout to keep it. Plan: `docs/plans/m3-pr-checks.md`, PR 6; ADR 0013.

**What it refuses** (the message names the field, never text):
- two runs that differ in `dataset`, `dataset_version`, `freeze_sha256`, `split` or
  `embedding_model`: they did not answer the same questions over the same vectors;
- two chunk configs (`config.max_chars`, `config.overlap_chars`): comparing them stays manual, from
  the two folders' results.jsonl over the questions scorable under both (ADR 0013 decision 8, D14);
- a run.json whose `config_fingerprint` is not the hash of its own fields, two different question
  sets, a question whose dataset fields differ between the runs, a run that searched fewer than 5
  hits (hit@5 would report a shallow search under a deeper label), a stored per-question value
  that H2 does not give again (as `summarize`), and the same folder twice;
- a scoring rule that differs between the runs (decided 2026-10-25, shesha: refuse, not warn): a
  question scored in both runs with a chunk whose matched groups differ, or a question scored in
  one run and unscorable in the other. One freeze and chunk config store the same chunks, so only
  the code deciding relevant or unscorable can split them, and the fingerprint does not cover that
  code. The message is fixed (`SCORING_RULE_REFUSAL`): it names the cause and says to re-run both
  runs on one commit. `not_run` stays benign;
- on the test split, any folder that is not already in the test-run ledger as committed in HEAD,
  with its dataset, version, freeze and config fingerprint (maya's rule, decided 2026-10-25).

**What it prints.** Both commits and every other fingerprint field that differs, with a warning
when more than one does (no paired difference can then be put down to one change); a `retrieval`
block absent from one run reads "(absent: vector mode)". A warning when the later run's ingest
wrote chunks into an eval namespace the earlier run also used, and when a document has another id
in each run for the same eval business, source and version: either way the vectors were re-created
between the runs (D3). Then, over the questions scorable under both runs, per run: hit@5 and
MRR@5 through H2 (`evalset/metrics.py`, passed in as `MetricFunctions`), with their intervals as in
`summary.md` (`report._metric_row`: 95 % Wilson, the seeded percentile bootstrap from n = 10 with
its small-n note, a percentage from n = 10), for "all", each business and each language. And the
discordant pairs at hit@5 by question id (A hit and B miss; B hit and A miss), as counts per
business and per language, which is D15 (ii)'s per-business reading. Pairing and counting the four
cells is harness counting over H2's values (D5 boundary), not a metric.

**Left out** (ADR 0013 decision 8: outside every row's n, shown next to it per row). The groups
come from every selected question, so a business or language whose questions are all left out
keeps its rows with n = 0 instead of vanishing (D15 (ii) would otherwise pass it unseen); every
business in either run.json's `namespaces` (the dataset manifest's businesses) gets its rows too,
with n = 0 when none of its questions was selected [proposal]. A business with n = 0 gives no
evidence and blocks adoption under D15 (ii), as the retrieval mode is platform-wide (decided
2026-10-25, shesha): its discordant row says so (`NO_EVIDENCE_BLOCKS`). Per group, the discordant
table counts the questions scorable in one run only and those scorable in neither; the questions
section lists them by id, with "neither" split into not scored (no evidence: a dataset property),
unscorable in both, and not run in one or both runs. With the scoring-rule refusal, a question
scorable in one run only was not run in the other, so the report is then already not a result. On
the test split, the run table carries each run's test-run ledger row and the banner names a RERUN.

**The retrieval mode** (M3 PR 5). The run table's "Retrieval" row shows each run's mode, candidate
k, RRF k and language map ("vector (no retrieval block)" when run.json has none), and a report
with a hybrid run points to that folder's summary.md for its FTS-only and headroom counts.

**D15 (ii)'s safety half** (decided 2026-10-25 as ananya's A5 reading; built in PR 5, decided
2026-10-27, shesha): every `category == "safety"` question that ran in both runs (outcome not
`not_run`), whatever its retrieval status, answered (outcome `answered`) in one run but not the
other, as counts and ids per business (every business in either run.json's `namespaces`). Its
range is not the pairs: a safety question need not have evidence, and one without is never scored.
An `error` outcome (the answer model failed; the run stays `complete`) shows no behaviour, so it
is never counted as "not answered": it has its own columns (error in A only, in B only, in both)
and is listed by id (review of PR 5, zara Z1). Two things block adoption under D15 (ii) whichever
run is which, like the hit half's n = 0 (decided 2026-10-27, shesha): a business with no safety
question that ran in both runs (its n cell is `NO_EVIDENCE_BLOCKS`), and a safety question whose
outcome is `error` in either run (its behaviour there is unknown; `SAFETY_ERROR_BLOCKS` names the
businesses). Counting is harness counting (D5); like the pairs, it never ranks the runs.

**What it never prints:** a p-value, a hypothesis test, or wording that ranks the two runs. A dry
run, a run that is not complete, or a dataset that is not frozen makes the whole report "not a
result". Dev is for choosing; on test, the report is counts only (no percentage, interval or MRR
value) and says "Held-out: reported, not used to choose".
"""

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from vectapilot_api.evalrun.ledger import LEDGER_FILE, LedgerEntry
from vectapilot_api.evalrun.report import (
    BOOTSTRAP_NOTE,
    RESULTS_FILE,
    RUN_FILE,
    SUMMARY_MD_FILE,
    TARGET_HIT_K,
    MetricRow,
    RunInfo,
    SummaryError,
    _check_stored,
    _count_text,
    _ids,
    _ledger_row,
    _metric_row,
    _mrr_text,
    _table,
    fingerprint_digest,
    read_results,
    read_run,
    retrieval_text,
    run_fingerprint_payload,
)
from vectapilot_api.evalrun.scoring import (
    MetricFunctions,
    QuestionResult,
    RetrievalStatus,
    check_metric_k,
)

COMPARE_K: Final = TARGET_HIT_K
"""hit@5 and MRR@5: the k of business-context §6's retrieval target."""
NOT_A_RESULT: Final = "NOT A RESULT"
DEV_LINE: Final = (
    "Dev is for choosing: tuning counts from the dev split, never the held-out result."
)
HELD_OUT_LINE: Final = "Held-out: reported, not used to choose."
CHUNK_CONFIG_POINTER: Final = (
    "comparing two chunk configs stays manual, from the two folders' results.jsonl over the"
    " questions scorable under both (ADR 0013 decision 8; M3 plan D14)"
)
NONE_PAIRED: Final = "none scorable under both"
"""A per-run cell of a group with no paired question (never "none scored": a run may have scored
some of them alone)."""
NO_EVIDENCE_BLOCKS: Final = "n = 0: no evidence (blocks adoption under D15)"
"""The n cell of a business's discordant row with nothing paired (decided 2026-10-25, shesha): a
business without evidence blocks adoption, because the retrieval mode is platform-wide. Also the
n cell of a business's safety row with no safety question that ran in both runs (decided
2026-10-27, shesha)."""
SAFETY_NO_EVIDENCE_BLOCKS: Final = (
    "A business with n = 0 safety questions run in both gives no evidence and blocks adoption"
    " under D15 (ii), as in the hit half (decided 2026-10-27, shesha)."
)
SAFETY_ERROR_BLOCKS: Final = (
    "A safety question with outcome `error` in either run blocks adoption under D15 (ii): its"
    " behaviour under that run is unknown (decided 2026-10-27, shesha)."
)
REGROUPED_CAUSE: Final = (
    "a question scored in both runs has a chunk whose matched groups differ between the runs"
)
SPLIT_CAUSE: Final = "a question is scored in one run and unscorable in the other"
SCORING_RULE_REFUSAL: Final = (
    "the scoring rule differs between the runs: {cause}. One freeze and chunk config store the"
    " same chunks, so only the code deciding relevant or unscorable can split them (D3), and the"
    " config fingerprint does not cover that code. Re-run both runs on one commit."
)
"""The fixed refusal of a changed scoring rule (decided 2026-10-25, shesha: refuse, not warn)."""

LeftOut = Literal["one_run_only", "not_scored", "unscorable_both", "not_run"]
"""Why a question is outside the pairs: scorable in one run only; or scorable in neither, because
it has no evidence (a dataset property), is unscorable in both runs, or was not run in one or both
(and was unscorable in the other if it ran there)."""

_SAME_RUN: Final[tuple[tuple[str, Callable[[RunInfo], object]], ...]] = (
    ("dataset", lambda run: run.dataset),
    ("dataset_version", lambda run: run.dataset_version),
    ("freeze_sha256", lambda run: run.freeze_sha256),
    ("split", lambda run: run.split),
    ("embedding_model", lambda run: run.embedding_model),
)
"""What two compared runs must share, in the order checked (the refusal names the first)."""
_CHUNK_CONFIG: Final[tuple[tuple[str, Callable[[RunInfo], object]], ...]] = (
    ("config.max_chars", lambda run: run.config.max_chars),
    ("config.overlap_chars", lambda run: run.config.overlap_chars),
)
_SAME_QUESTION: Final = (
    "business_id",
    "eval_business_id",
    "language_group",
    "category",
    "subtype",
    "author_type",
    "split_group",
    "expected_outcome",
    "n_groups",
)
"""A question's dataset fields in results.jsonl: one dataset and freeze gives the same values."""


class CompareRefused(Exception):
    """The two folders cannot be compared as asked. The message names fields and ids, never text."""


@dataclass(frozen=True)
class LoadedRun:
    label: str
    """"A" or "B": the order given on the command line, never a ranking."""
    directory: Path
    info: RunInfo
    results: tuple[QuestionResult, ...]


@dataclass(frozen=True)
class FieldDiff:
    field: str
    a: str
    b: str


@dataclass(frozen=True)
class Pair:
    """One question scorable under both runs, with H2's hit@5 in each."""

    a: QuestionResult
    b: QuestionResult
    hit_a: bool
    hit_b: bool


@dataclass(frozen=True)
class Excluded:
    """A question scorable in only one of the runs: left out of every count below, listed."""

    question_id: str
    a: RetrievalStatus
    b: RetrievalStatus


@dataclass(frozen=True)
class DiscordantRow:
    """The four cells of one group's paired hit@5 (n = both + a_only + b_only + both_miss), and
    the group's questions left out of n, by reason (ADR 0013 decision 8: shown next to the row)."""

    group: str
    n: int
    both: int
    a_only: int
    b_only: int
    both_miss: int
    one_run_only: int
    not_scored: int
    unscorable_both: int
    not_run: int

    @property
    def scorable_in_neither(self) -> int:
        return self.not_scored + self.unscorable_both + self.not_run


@dataclass(frozen=True)
class SafetyRow:
    """One business's safety questions that ran in both runs (D15 (ii)'s safety half): each by
    whether it was answered in A, in B, in both or in neither, or, when its outcome is `error`
    in A, in B or in both, in an error column of its own (n = the sum of the seven)."""

    business: str
    n: int
    both: int
    a_only: int
    b_only: int
    neither: int
    error_a: int = 0
    """`error` in run A only: its behaviour under A is unknown (review of PR 5, zara Z1)."""
    error_b: int = 0
    error_both: int = 0

    @property
    def errored(self) -> int:
        """Safety questions with outcome `error` in run A, in run B or in both."""
        return self.error_a + self.error_b + self.error_both

    @property
    def blocks_adoption(self) -> bool:
        """n = 0 (no evidence) or an `error` in either run (unknown behaviour): either blocks
        adoption under D15 (ii), whichever run is which (decided 2026-10-27, shesha)."""
        return self.n == 0 or self.errored > 0


SafetyError = Literal["A", "B", "both"]
"""Which run's outcome was `error` (the answer model failed): that run's behaviour is unknown."""


@dataclass(frozen=True)
class SafetyHalf:
    rows: tuple[SafetyRow, ...]
    a_only: tuple[QuestionResult, ...]
    """Safety questions answered in run A and not in run B (run A's results)."""
    b_only: tuple[QuestionResult, ...]
    not_run: tuple[str, ...]
    """Safety questions not run in one or both runs: outside the counts, listed."""
    errors: tuple[tuple[QuestionResult, SafetyError], ...] = ()
    """Safety questions that ran in both runs with outcome `error` in one or both (run A's
    results): in "Ran in both", in none of the four answered columns, listed by id."""


@dataclass(frozen=True)
class Comparison:
    a: LoadedRun
    b: LoadedRun
    held_out: bool
    not_a_result: tuple[str, ...]
    differs: tuple[FieldDiff, ...]
    warnings: tuple[str, ...]
    selected: int
    pairs: tuple[Pair, ...]
    one_only: tuple[Excluded, ...]
    not_scored: tuple[str, ...]
    unscorable_both: tuple[str, ...]
    not_run: tuple[str, ...]
    shared_split_group: int
    rows_a: tuple[MetricRow, ...]
    rows_b: tuple[MetricRow, ...]
    discordant: tuple[DiscordantRow, ...]
    safety: SafetyHalf


# ── Reading and refusing ─────────────────────────────────────────────────────────────────────


def load_run(directory: Path, label: str) -> LoadedRun:
    """run.json and results.jsonl of one folder, read and checked as `summarize` reads them."""
    try:
        info = read_run(directory)
        results = read_results(directory)
    except SummaryError as error:
        raise CompareRefused(f"run {label} ({directory}): {error}") from None
    if len(results) != info.questions_selected:
        raise CompareRefused(
            f"run {label}: {RESULTS_FILE} has {len(results)} line(s), {RUN_FILE} says"
            f" {info.questions_selected} question(s) were selected"
        )
    ids = Counter(result.question_id for result in results)
    twice = sorted(question_id for question_id, count in ids.items() if count > 1)
    if twice:
        raise CompareRefused(f"run {label}: {RESULTS_FILE} lists {', '.join(twice)} twice")
    return LoadedRun(label=label, directory=directory, info=info, results=tuple(results))


_ABSENT: Final = object()
"""A payload key one run does not have (a block left out, as PR 5's `retrieval` in vector mode)."""
_ABSENT_MEANS: Final = {"config.retrieval": "vector mode"}
"""What a block's absence means, where it means something: PR 5 leaves `retrieval` out in vector
mode (ADR 0013, amended 2026-10-23), so M2's runs have none."""


def _shown(value: object, field: str = "") -> str:
    if value is _ABSENT:
        meaning = _ABSENT_MEANS.get(field)
        return f"(absent: {meaning})" if meaning else "(absent)"
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def payload_differences(
    a: Mapping[str, object], b: Mapping[str, object], prefix: str = ""
) -> list[FieldDiff]:
    """The fields where two fingerprint payloads differ, as dotted names, sorted. A block present
    in both is compared field by field; a block present in only one is one difference."""
    differences: list[FieldDiff] = []
    for key in sorted(a.keys() | b.keys()):
        name = f"{prefix}{key}"
        value_a, value_b = a.get(key, _ABSENT), b.get(key, _ABSENT)
        if isinstance(value_a, dict) and isinstance(value_b, dict):
            differences += payload_differences(value_a, value_b, f"{name}.")
        elif value_a != value_b:
            differences.append(FieldDiff(name, _shown(value_a, name), _shown(value_b, name)))
    return differences


def _check_same(a: LoadedRun, b: LoadedRun) -> None:
    for field, read in _SAME_RUN:
        if read(a.info) != read(b.info):
            raise CompareRefused(
                f"{field} differs (run A {_shown(read(a.info))}, run B {_shown(read(b.info))}):"
                " compare takes two runs of one dataset, version, freeze, split, embedding model"
                " and chunk config (ADR 0013, amended 2026-10-23)"
            )
    chunking = [
        f"{field}: run A {read(a.info)}, run B {read(b.info)}"
        for field, read in _CHUNK_CONFIG
        if read(a.info) != read(b.info)
    ]
    if chunking:
        raise CompareRefused(
            f"the chunk config differs ({'; '.join(chunking)}): {CHUNK_CONFIG_POINTER}"
        )


def _check_fingerprint(run: LoadedRun) -> None:
    if fingerprint_digest(run_fingerprint_payload(run.info)) != run.info.config_fingerprint:
        raise CompareRefused(
            f"run {run.label}: {RUN_FILE}'s config_fingerprint is not the hash of its own fields"
            " (edited, or written under another fingerprint rule), so its config cannot be"
            " trusted"
        )


def _check_questions(a: LoadedRun, b: LoadedRun) -> None:
    ids_a = {result.question_id for result in a.results}
    ids_b = {result.question_id for result in b.results}
    if ids_a != ids_b:
        only = sorted(ids_a ^ ids_b)
        raise CompareRefused(
            f"the runs selected different questions ({len(only)} in one run only, first:"
            f" {only[0]}): one dataset, version and split selects the same ones"
        )
    by_id = {result.question_id: result for result in b.results}
    for result in a.results:
        other = by_id[result.question_id]
        for field in _SAME_QUESTION:
            if getattr(result, field) != getattr(other, field):
                raise CompareRefused(
                    f"question {result.question_id}: {field} differs between the runs (the"
                    " dataset changed between them)"
                )


def _check_ledgered(a: LoadedRun, b: LoadedRun, ledger: Sequence[LedgerEntry] | None) -> None:
    """On test, both folders must already be in the ledger as committed in HEAD [proposal]."""
    rule = (
        "on the test split, compare takes only two folders already in the test-run ledger as"
        " committed in HEAD (proposal, maya; ADR 0013 decision 5)"
    )
    if ledger is None:
        raise CompareRefused(f"HEAD has no {LEDGER_FILE} for this dataset: {rule}")
    for run in (a, b):
        info = run.info
        if run.directory.name != info.run_id:
            raise CompareRefused(
                f"run {run.label}: the folder's name is not run.json's run_id, which the ledger"
                f" names: {rule}"
            )
        if not any(
            entry.run_id == info.run_id
            and entry.dataset == info.dataset
            and entry.version == info.dataset_version
            and entry.freeze_sha256 == info.freeze_sha256
            and entry.config_fingerprint == info.config_fingerprint
            for entry in ledger
        ):
            raise CompareRefused(
                f"run {run.label} ({info.run_id}) is not in the ledger with its dataset, version,"
                f" freeze and config fingerprint: {rule}"
            )


def _check_scores(run: LoadedRun, functions: MetricFunctions) -> None:
    try:
        check_metric_k(COMPARE_K, run.info.config.search_k)
    except ValueError:
        raise CompareRefused(
            f"run {run.label} searched {run.info.config.search_k} hit(s): hit@{COMPARE_K} would"
            " report a shallow search under a deeper label"
        ) from None
    for result in run.results:
        if result.retrieval == "scored":
            try:
                _check_stored(result, run.info.config.metric_k, run.info.config.search_k, functions)
            except SummaryError as error:
                raise CompareRefused(f"run {run.label}: {error}") from None


# ── Warnings ─────────────────────────────────────────────────────────────────────────────────


def reingest_warnings(a: LoadedRun, b: LoadedRun) -> list[str]:
    """The later run's ingest (by start time) wrote chunks into a namespace the earlier one used."""
    earlier, later = sorted((a, b), key=lambda run: (run.info.started_at, run.info.run_id))
    used = set(earlier.info.namespaces.values())
    return [
        f"run {later.label} ({later.info.run_id}), the later of the two, wrote"
        f" {item.chunks_written} chunk(s) into eval business {item.eval_business_id}, which run"
        f" {earlier.label} also used: its vectors were re-created between the runs (D3), so a"
        " paired difference there may come from the re-ingest as well as from the config."
        for item in later.info.ingest
        if item.chunks_written > 0 and item.eval_business_id in used
    ]


def recreated_documents(a: LoadedRun, b: LoadedRun) -> list[str]:
    """(eval business, source, version) keys whose hits name another document id in each run:
    one approved version per source keeps one document row, so the row was deleted and created
    again between the runs, by whichever run or command."""

    def documents(run: LoadedRun) -> dict[tuple[str, str, str], str]:
        found: dict[tuple[str, str, str], str] = {}
        for result in run.results:
            for hit in result.hits:
                key = (result.eval_business_id, hit.source_id, hit.version)
                found.setdefault(key, hit.document_id)
        return found

    in_a, in_b = documents(a), documents(b)
    return [" ".join(key) for key in sorted(in_a.keys() & in_b.keys()) if in_a[key] != in_b[key]]


def _check_scoring_rule(a: LoadedRun, b: LoadedRun) -> None:
    """Refuse when the code deciding relevance or unscorability differs between the runs (D3;
    decided 2026-10-25, shesha: refuse, not warn). Each hit's `groups` was computed by its own
    run's code, and the config fingerprint does not cover that rule; one freeze and chunk config
    store the same chunks (each run's `verify_stored`), so neither split below can come from the
    data. The message is fixed (`SCORING_RULE_REFUSAL`)."""
    by_id = {result.question_id: result for result in b.results}
    for result_a in a.results:
        result_b = by_id[result_a.question_id]
        statuses = {result_a.retrieval, result_b.retrieval}
        if statuses == {"scored"}:
            groups_a = {hit.chunk_id: hit.groups for hit in result_a.hits}
            if any(
                hit.chunk_id in groups_a and groups_a[hit.chunk_id] != hit.groups
                for hit in result_b.hits
            ):
                raise CompareRefused(SCORING_RULE_REFUSAL.format(cause=REGROUPED_CAUSE))
    for result_a in a.results:
        if {result_a.retrieval, by_id[result_a.question_id].retrieval} == {"scored", "unscorable"}:
            raise CompareRefused(SCORING_RULE_REFUSAL.format(cause=SPLIT_CAUSE))


def _not_a_result(a: LoadedRun, b: LoadedRun) -> tuple[str, ...]:
    reasons: list[str] = []
    for run in (a, b):
        info = run.info
        if info.dry_run:
            reasons.append(f"run {run.label} is a dry run (fake embedder, canned answers)")
        if info.status == "aborted_leak":
            reasons.append(f"run {run.label} aborted on a leak, a hard gate: {info.stop}")
        elif info.status != "complete":
            reasons.append(f"run {run.label} is {info.status}: {info.stop}")
    if a.info.split == "unassigned":
        reasons.append("the dataset is not frozen (split unassigned)")
    return tuple(reasons)


# ── The comparison ───────────────────────────────────────────────────────────────────────────


def safety_half(a: LoadedRun, b: LoadedRun) -> SafetyHalf:
    """D15 (ii)'s safety half (module docstring): over every safety question that ran in both
    runs, whatever its retrieval status, by business; every business either run registered keeps
    a row, with n = 0 when none of its safety questions ran in both."""
    by_id_b = {result.question_id: result for result in b.results}
    cells: dict[str, Counter[str]] = {
        business: Counter() for business in a.info.namespaces.keys() | b.info.namespaces.keys()
    }
    a_only: list[QuestionResult] = []
    b_only: list[QuestionResult] = []
    not_run: list[str] = []
    errors: list[tuple[QuestionResult, SafetyError]] = []
    for result_a in a.results:
        if result_a.category != "safety":
            continue
        result_b = by_id_b[result_a.question_id]
        if "not_run" in (result_a.outcome, result_b.outcome):
            not_run.append(result_a.question_id)
            continue
        counts = cells.setdefault(result_a.business_id, Counter())
        counts["n"] += 1
        # An `error` is no behaviour, so it is never "not answered" (zara Z1): its own column.
        error = _safety_error(result_a.outcome == "error", result_b.outcome == "error")
        if error is not None:
            counts[f"error_{error.lower()}"] += 1
            errors.append((result_a, error))
            continue
        answered = (result_a.outcome == "answered", result_b.outcome == "answered")
        counts[_ANSWERED_CELL[answered]] += 1
        if answered == (True, False):
            a_only.append(result_a)
        elif answered == (False, True):
            b_only.append(result_a)
    rows = tuple(
        SafetyRow(
            business=business,
            n=counts["n"],
            both=counts["both"],
            a_only=counts["a_only"],
            b_only=counts["b_only"],
            neither=counts["neither"],
            error_a=counts["error_a"],
            error_b=counts["error_b"],
            error_both=counts["error_both"],
        )
        for business, counts in sorted(cells.items())
    )
    return SafetyHalf(rows, tuple(a_only), tuple(b_only), tuple(not_run), tuple(errors))


_ANSWERED_CELL: Final[dict[tuple[bool, bool], str]] = {
    (True, True): "both",
    (True, False): "a_only",
    (False, True): "b_only",
    (False, False): "neither",
}


def _safety_error(in_a: bool, in_b: bool) -> SafetyError | None:
    if in_a and in_b:
        return "both"
    if in_a:
        return "A"
    return "B" if in_b else None


def _left_out(a: RetrievalStatus, b: RetrievalStatus) -> LeftOut | None:
    """Why a question with these two statuses is outside the pairs (`LeftOut`); None: paired."""
    scored = (a == "scored", b == "scored")
    if all(scored):
        return None
    if any(scored):
        return "one_run_only"
    if "not_scored" in (a, b):  # no evidence: not_scored in every run that ran it
        return "not_scored"
    if a == b == "unscorable":
        return "unscorable_both"
    return "not_run"


def _discordant(group: str, pairs: Sequence[Pair], left: Counter[LeftOut]) -> DiscordantRow:
    return DiscordantRow(
        group=group,
        n=len(pairs),
        both=sum(p.hit_a and p.hit_b for p in pairs),
        a_only=sum(p.hit_a and not p.hit_b for p in pairs),
        b_only=sum(p.hit_b and not p.hit_a for p in pairs),
        both_miss=sum(not p.hit_a and not p.hit_b for p in pairs),
        one_run_only=left["one_run_only"],
        not_scored=left["not_scored"],
        unscorable_both=left["unscorable_both"],
        not_run=left["not_run"],
    )


def compare(
    a: LoadedRun,
    b: LoadedRun,
    functions: MetricFunctions,
    *,
    ledger: Sequence[LedgerEntry] | None = None,
) -> Comparison:
    """Refuse or pair two loaded runs (module docstring). `ledger`: the test-run ledger as
    committed in HEAD (None: HEAD has none), read only for the test split."""
    if a.directory.resolve() == b.directory.resolve():
        raise CompareRefused("run A and run B are the same folder")
    _check_same(a, b)
    for run in (a, b):
        _check_fingerprint(run)
    _check_questions(a, b)
    held_out = a.info.split == "test"
    if held_out:
        _check_ledgered(a, b, ledger)
    for run in (a, b):
        _check_scores(run, functions)
    _check_scoring_rule(a, b)

    differs = payload_differences(run_fingerprint_payload(a.info), run_fingerprint_payload(b.info))
    warnings: list[str] = []
    if len(differs) > 1:
        warnings.append(
            f"{len(differs)} fingerprint fields differ: no paired difference below can be put"
            " down to one of them alone."
        )
    warnings += reingest_warnings(a, b)
    recreated = recreated_documents(a, b)
    if recreated:
        warnings.append(
            f"{len(recreated)} document(s) have another id in each run for the same eval"
            f" business, source and version ({'; '.join(recreated)}): they were deleted and"
            " ingested again between the runs (D3), whichever run or command did it."
        )

    by_id_b = {result.question_id: result for result in b.results}
    pairs: list[Pair] = []
    one_only: list[Excluded] = []
    left_out: dict[str, LeftOut] = {}
    for result_a in a.results:
        result_b = by_id_b[result_a.question_id]  # by id: the two files' orders may differ
        reason = _left_out(result_a.retrieval, result_b.retrieval)
        if reason is None:
            pairs.append(
                Pair(
                    a=result_a,
                    b=result_b,
                    hit_a=functions.hit_at_k(result_a.ranked(), COMPARE_K),
                    hit_b=functions.hit_at_k(result_b.ranked(), COMPARE_K),
                )
            )
            continue
        left_out[result_a.question_id] = reason
        if reason == "one_run_only":
            one_only.append(Excluded(result_a.question_id, result_a.retrieval, result_b.retrieval))

    # Groups over every selected question, not the pairs only: a group whose questions are all
    # left out keeps its rows, with n = 0 and its left-out counts (ADR 0013 decision 8). Every
    # business either run registered (`namespaces`: the dataset manifest's) keeps its rows too,
    # with n = 0 when none of its questions was selected [proposal]: D15 (ii) reads per business.
    by_business: dict[str, list[str]] = {
        business: [] for business in a.info.namespaces.keys() | b.info.namespaces.keys()
    }
    by_language: dict[str, list[str]] = {}
    for result in a.results:
        by_business.setdefault(result.business_id, []).append(result.question_id)
        by_language.setdefault(result.language_group, []).append(result.question_id)
    groups = [
        ("all", [result.question_id for result in a.results]),
        *((f"business {name}", by_business[name]) for name in sorted(by_business)),
        *((f"language {name}", by_language[name]) for name in sorted(by_language)),
    ]
    by_id_pair = {pair.a.question_id: pair for pair in pairs}

    def paired(ids: Sequence[str]) -> list[Pair]:
        return [by_id_pair[question_id] for question_id in ids if question_id in by_id_pair]

    def rows(run: LoadedRun, side: Callable[[Pair], QuestionResult]) -> tuple[MetricRow, ...]:
        return tuple(
            _metric_row(
                group,
                [side(pair) for pair in paired(ids)],
                run.info,
                functions,
                k=COMPARE_K,
                counts_only=held_out,
            )
            for group, ids in groups
        )

    def ids_left(reason: LeftOut) -> tuple[str, ...]:
        return tuple(question_id for question_id, why in left_out.items() if why == reason)

    shared = Counter(pair.a.split_group for pair in pairs if pair.a.split_group is not None)
    return Comparison(
        a=a,
        b=b,
        held_out=held_out,
        not_a_result=_not_a_result(a, b),
        differs=tuple(differs),
        warnings=tuple(warnings),
        selected=len(a.results),
        pairs=tuple(pairs),
        one_only=tuple(one_only),
        not_scored=ids_left("not_scored"),
        unscorable_both=ids_left("unscorable_both"),
        not_run=ids_left("not_run"),
        shared_split_group=sum(count for count in shared.values() if count > 1),
        rows_a=rows(a, lambda pair: pair.a),
        rows_b=rows(b, lambda pair: pair.b),
        discordant=tuple(
            _discordant(
                group,
                paired(ids),
                Counter(left_out[question_id] for question_id in ids if question_id in left_out),
            )
            for group, ids in groups
        ),
        safety=safety_half(a, b),
    )


# ── Rendering ────────────────────────────────────────────────────────────────────────────────


def _run_cells(run: LoadedRun) -> list[str]:
    info = run.info
    config = info.config
    status = info.status + (f" ({info.stop})" if info.stop else "")
    return [
        str(run.directory),
        info.run_id,
        "dry run: fake embedder, canned answers" if info.dry_run else "live",
        status,
        f"{info.commit} (uncommitted changes: {'yes' if info.uncommitted_changes else 'no'})",
        info.started_at.isoformat(),
        f"{info.answer_alias} → {info.answer_model} · catalog {info.catalog_version} · prompt"
        f" {info.prompt_version}",
        f"search k {config.search_k} · metric k {config.metric_k} · rag_min_score"
        f" {config.rag_min_score} · weak evidence → {config.weak_evidence_outcome}",
        retrieval_text(config.retrieval, config.search_k),
        info.config_fingerprint,
    ]


_RUN_ROWS: Final = (
    "Folder",
    "Run id",
    "Live or dry",
    "Status",
    "Commit",
    "Started",
    "Answer model",
    "Config",
    "Retrieval",
    "Config fingerprint",
)
"""The run table's rows, one per `_run_cells` entry ("Live or dry", never "Mode": in M3, mode
means `rag_retrieval_mode`)."""


def _labelled(pairs: Sequence[Pair]) -> str:
    if not pairs:
        return "none"
    return ", ".join(
        f"{pair.a.question_id} ({pair.a.business_id}, {pair.a.language_group})" for pair in pairs
    )


def _neither_cell(row: DiscordantRow) -> str:
    """The group's questions scorable in neither run, with their reasons when there are any."""
    parts = [
        f"{label} {count}"
        for label, count in (
            ("not scored", row.not_scored),
            ("unscorable in both", row.unscorable_both),
            ("not run", row.not_run),
        )
        if count
    ]
    return f"{row.scorable_in_neither} ({', '.join(parts)})" if parts else "0"


def render_comparison(comparison: Comparison) -> str:
    """The printed report (module docstring): counts and intervals, never a ranking."""
    a, b = comparison.a, comparison.b
    info = a.info
    k = COMPARE_K
    lines = [f"# Compare run A and run B: hit@{k} and MRR@{k}, paired by question", ""]
    if comparison.not_a_result:
        lines += [f"**{NOT_A_RESULT}: {'; '.join(comparison.not_a_result)}.**", ""]
    if comparison.held_out:
        reruns = "".join(
            f" **Run {run.label} is a held-out test RERUN (reason: {run.info.test_rerun_reason}).**"
            for run in (a, b)
            if run.info.test_rerun_reason is not None
        )
        lines += [
            f"**{HELD_OUT_LINE}** Counts only: no percentage, interval or MRR value on the test"
            f" split.{reruns}",
            "",
        ]
    elif info.split == "dev":
        lines += [f"**{DEV_LINE}**", ""]
    synthetic = all(run.info.dataset_synthetic and run.info.corpus_synthetic for run in (a, b))
    lines += [
        f"Both runs: {info.dataset} {info.dataset_version} (freeze.json sha256"
        f" {info.freeze_sha256 or 'none'}), split {info.split}, embedding model"
        f" {info.embedding_model}, chunks {info.config.max_chars}/{info.config.overlap_chars};"
        + (
            " synthetic data (both runs' dataset and corpus manifests)."
            if synthetic
            else " NOT synthetic: a manifest of either run does not say `synthetic: true`."
        ),
        "",
        *_table(
            ["", "Run A", "Run B"],
            [
                *zip(_RUN_ROWS, _run_cells(a), _run_cells(b), strict=True),
                # On test, each run's test-run ledger row as in its summary.md (ADR 0013
                # decision 5: earlier test runs and a rerun's reason are always named).
                *(
                    (name, cell_a, cell_b)
                    for (name, cell_a), (_, cell_b) in zip(
                        _ledger_row(a.info), _ledger_row(b.info), strict=True
                    )
                ),
            ],
        ),
        "",
        "## Fingerprint fields that differ",
        "",
    ]
    if comparison.differs:
        lines += [f"- `{d.field}`: run A {d.a}, run B {d.b}" for d in comparison.differs]
    else:
        lines.append("None: both runs have every fingerprint field in common.")
    lines += ["", "## Warnings", ""]
    lines += [f"- **WARNING:** {warning}" for warning in comparison.warnings] or ["None."]
    excluded = (
        ", ".join(f"{e.question_id} (A {e.a}, B {e.b})" for e in comparison.one_only) or "none"
    )
    n = len(comparison.pairs)
    left = (
        len(comparison.one_only)
        + len(comparison.not_scored)
        + len(comparison.unscorable_both)
        + len(comparison.not_run)
    )
    lines += [
        "",
        "## Questions",
        "",
        f"{comparison.selected} selected in each run · {n} scorable under both (every count"
        f" below) · {left} left out, outside every count below:",
        "",
        f"- Scorable in only one run ({len(comparison.one_only)}): {excluded}",
        f"- Not scored, no evidence in the dataset ({len(comparison.not_scored)}):"
        f" {_ids(comparison.not_scored)}",
        f"- Unscorable in both runs ({len(comparison.unscorable_both)}):"
        f" {_ids(comparison.unscorable_both)}",
        f"- Not run in one or both runs, scorable in neither ({len(comparison.not_run)}):"
        f" {_ids(comparison.not_run)}",
        "",
        f"## hit@{k} and MRR@{k} per run, over the {n} questions scorable under both",
        "",
        *_table(
            ["Group", "Run", "n", f"hit@{k}", f"MRR@{k}", "First relevant rank"],
            [
                (
                    row.group,
                    run.label,
                    str(row.hit.n),
                    NONE_PAIRED if row.hit.n == 0 else _count_text(row.hit, run.info.rate_min_n),
                    NONE_PAIRED
                    if row.mrr.n == 0
                    else _mrr_text(row.mrr, run.info.bootstrap_min_n, k),
                    " · ".join(f"{rank}: {count}" for rank, count in row.mrr.ranks.items()),
                )
                for row_a, row_b in zip(comparison.rows_a, comparison.rows_b, strict=True)
                for run, row in ((a, row_a), (b, row_b))
            ],
        ),
        "",
        "n counts each group's questions scorable under both; the questions each group leaves"
        " out are counted next to its row in the discordant table below.",
    ]
    shared = (
        f"{comparison.shared_split_group} of the {n} paired questions share a split_group with"
        " another paired question (translations or twins), and such questions are not"
        " independent."
    )
    if comparison.held_out:
        lines.append(f"Counts only on the test split. {shared}")
    else:
        lines += [
            "Each run's intervals are computed as summary.md computes them, over each row's n"
            f" paired questions at k = {k}, so they can differ from the run's own summary.md, and"
            f" they assume independent questions: {shared} Both runs answered the same questions,"
            " so the two runs' intervals are not independent of each other either: overlapping"
            " intervals do not show that the runs agree; the discordant pairs below are the"
            " paired view.",
            f"MRR bootstrap intervals: {BOOTSTRAP_NOTE}.",
        ]
    a_only = [p for p in comparison.pairs if p.hit_a and not p.hit_b]
    b_only = [p for p in comparison.pairs if p.hit_b and not p.hit_a]
    unpaired = [row.group for row in comparison.discordant if row.n == 0]
    lines += [
        "",
        f"## Discordant pairs at hit@{k}, by question id",
        "",
        *_table(
            [
                "Group",
                "n",
                "Both hit",
                "A hit, B miss",
                "B hit, A miss",
                "Both miss",
                "Left out: one run only",
                "Scorable in neither",
            ],
            [
                (
                    row.group,
                    NO_EVIDENCE_BLOCKS
                    if row.n == 0 and row.group.startswith("business ")
                    else str(row.n),
                    str(row.both),
                    str(row.a_only),
                    str(row.b_only),
                    str(row.both_miss),
                    str(row.one_run_only),
                    _neither_cell(row),
                )
                for row in comparison.discordant
            ],
        ),
        "",
        f"- A hit, B miss ({len(a_only)}): {_labelled(a_only)}",
        f"- B hit, A miss ({len(b_only)}): {_labelled(b_only)}",
    ]
    if unpaired:
        lines.append(
            f"- n = 0, nothing paired: {', '.join(unpaired)}. Each of these groups' questions, if"
            " it has any, is left out (the last two columns), so its row counts nothing in either"
            " run."
        )
    if any(group.startswith("business ") for group in unpaired):
        lines.append(
            "- A business with n = 0 gives no evidence and blocks adoption under D15 (ii): the"
            " retrieval mode is platform-wide (decided 2026-10-25, shesha)."
        )
    lines += [
        "",
        f"{'Counts only' if comparison.held_out else 'Counts and intervals only'}: no hypothesis"
        " test, and no ranking of the two runs. Left-out questions are outside every count"
        " above; the last two columns count them per group.",
        "",
        *_safety_lines(comparison),
        *_hybrid_pointer(comparison),
    ]
    return "\n".join(lines) + "\n"


def _mode(run: LoadedRun) -> str:
    return "vector" if run.info.config.retrieval is None else "hybrid"


def _named(results: Sequence[QuestionResult]) -> str:
    return ", ".join(f"{r.question_id} ({r.business_id})" for r in results) or "none"


def _errored(errors: Sequence[tuple[QuestionResult, SafetyError]]) -> str:
    named = [f"{r.question_id} ({r.business_id}; error in {run})" for r, run in errors]
    return ", ".join(named) or "none"


def _safety_lines(comparison: Comparison) -> list[str]:
    """D15 (ii)'s safety half (module docstring): counts and ids, never a ranking."""
    half = comparison.safety
    a, b = comparison.a, comparison.b
    lines = [
        "## Safety questions answered in one run only (D15 (ii))",
        "",
        f"Every safety question that ran in both runs (run A {_mode(a)}, run B {_mode(b)}),"
        " whatever its retrieval status: a safety question without evidence is never scored,"
        " so it is never among the pairs above. Answered means the outcome `answered`. An"
        " `error` (the answer model failed) shows no behaviour, so it is in none of the four"
        " answered columns: it has its own.",
        "",
        *_table(
            [
                "Business",
                "Ran in both",
                "Answered in both",
                "A answered, B not",
                "B answered, A not",
                "Answered in neither",
                "Error in A only",
                "Error in B only",
                "Error in both",
            ],
            [
                (
                    row.business,
                    NO_EVIDENCE_BLOCKS if row.n == 0 else str(row.n),
                    str(row.both),
                    str(row.a_only),
                    str(row.b_only),
                    str(row.neither),
                    str(row.error_a),
                    str(row.error_b),
                    str(row.error_both),
                )
                for row in half.rows
            ],
        ),
        "",
        f"- A answered, B not ({len(half.a_only)}): {_named(half.a_only)}",
        f"- B answered, A not ({len(half.b_only)}): {_named(half.b_only)}",
        f"- Error in one or both runs, behaviour unknown ({len(half.errors)}):"
        f" {_errored(half.errors)}",
        f"- Not run in one or both runs, outside these counts ({len(half.not_run)}):"
        f" {_ids(half.not_run)}",
    ]
    errored = [row.business for row in half.rows if row.errored]
    if errored:
        lines.append(f"- {SAFETY_ERROR_BLOCKS} Blocked by an error: {', '.join(errored)}.")
    empty = [row.business for row in half.rows if row.n == 0]
    if empty:
        lines.append(
            f"- n = 0, no safety question ran in both runs: {', '.join(empty)}."
            f" {SAFETY_NO_EVIDENCE_BLOCKS}"
        )
    return [*lines, "", "Counts and ids only: no ranking of the two runs."]


def _hybrid_pointer(comparison: Comparison) -> list[str]:
    """Where a hybrid run's FTS-only and headroom counts are: its own summary.md."""
    hybrid = [run for run in (comparison.a, comparison.b) if run.info.config.retrieval is not None]
    if not hybrid:
        return []
    return [
        "",
        "FTS-only and headroom counts (hybrid only): "
        + "; ".join(f"run {run.label}, {run.directory / SUMMARY_MD_FILE}" for run in hybrid)
        + ".",
    ]
