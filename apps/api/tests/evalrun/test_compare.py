"""`python -m vectapilot_api.evalrun compare` (`evalrun/compare.py`, M3 PR 6).

Every run folder here is TEST-ONLY, built in pytest's tmp_path (never in evals/reports or
evals/datasets), with ids, ranks and groups only: no question, chunk or answer text. Per-question
values and means come from H2 (`evalset/metrics.py`); every expected count, mean and rank count is
worked out by hand in the comments, from the first-relevant ranks in `A_FIRST` and `B_FIRST`.
"""

import io
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Final, NoReturn

import pytest

from tests.evalrun.helpers import CONFIG, HYBRID_RETRIEVAL, PAWS, SUNRISE, run_info
from vectapilot_api.evalrun import cli
from vectapilot_api.evalrun.cli import Io
from vectapilot_api.evalrun.compare import (
    COMPARE_K,
    NO_EVIDENCE_BLOCKS,
    REGROUPED_CAUSE,
    SAFETY_ERROR_BLOCKS,
    SAFETY_NO_EVIDENCE_BLOCKS,
    SCORING_RULE_REFUSAL,
    SPLIT_CAUSE,
    CompareRefused,
    compare,
    load_run,
    payload_differences,
    render_comparison,
)
from vectapilot_api.evalrun.intervals import bootstrap_interval, wilson_interval
from vectapilot_api.evalrun.ledger import LEDGER_FILE, LedgerEntry
from vectapilot_api.evalrun.report import (
    RESULTS_FILE,
    RUN_FILE,
    IngestInfo,
    _relabelled,
    fingerprint_digest,
    read_run,
    run_fingerprint_payload,
)
from vectapilot_api.evalrun.scoring import H2_METRICS, HitRef, MetricFunctions, QuestionResult
from vectapilot_api.evalset import metrics
from vectapilot_api.evalset.ranking import RankedRelevance
from vectapilot_api.llm_cli import GitInfo

NAMESPACES: Final = run_info().namespaces
TRACE: Final = "0123456789abcdef0123456789abcdef"
LIVE: Final[dict[str, Any]] = {
    "dry_run": False,
    "embeddings_provider": "voyage",
    "embedding_model": "voyage-4",
    "embedding_models_answered": ("voyage-4",),
    "llm_provider": "anthropic",
}
"""run.json fields of a live run. TEST-ONLY folders: nothing here was sent anywhere."""
LEDGER_PATH: Final = PurePosixPath("evals/reports/support_qa", LEDGER_FILE)
DRY_CELL: Final = "dry run: fake embedder, canned answers"
"""The run table's "Live or dry" cell of a dry run (a literal here, so the text is pinned)."""

# The rank of each question's first relevant hit (None: one irrelevant hit only). Search k 6, so
# a rank of 6 is in the list but past k = 5 (a miss at hit@5).
A_FIRST: Final[dict[str, int | None]] = {
    "s-01": 1, "s-02": 2, "s-03": None, "s-04": 1, "s-05": None, "s-06": 6,
    "p-01": 1, "p-02": 5, "p-03": 1, "p-04": None, "p-05": 3, "p-06": 1,
}  # fmt: skip
B_FIRST: Final[dict[str, int | None]] = {
    "s-01": 1, "s-02": None, "s-03": 3, "s-04": 2, "s-05": None, "s-06": 1,
    "p-01": 1, "p-02": None, "p-03": 4, "p-04": 1, "p-05": 3, "p-06": 1,
}  # fmt: skip
HINDI: Final = frozenset({"s-04", "s-05", "p-03"})
PAIRED: Final = frozenset(A_FIRST)


def scored(
    question_id: str,
    business: str = SUNRISE,
    language: str = "en",
    *,
    first: int | None,
    metric_k: int = 5,
    document: str = "doc-1",
) -> QuestionResult:
    """A scored question with one evidence group, its stored values as H2 gives them at
    `metric_k` (hit when 1 <= first <= metric_k, RR 1/first). Its relevant chunk is
    `<id>-relevant` in every run and the others `<id>-c<rank>`: one chunk id keeps its matched
    groups across runs, as one stored chunk does (only a changed rule could change them)."""
    groups: tuple[tuple[int, ...], ...] = ((),) if first is None else ((),) * (first - 1) + ((0,),)
    hit = first is not None and first <= metric_k
    return QuestionResult(
        question_id=question_id,
        business_id=business,
        eval_business_id=NAMESPACES[business],
        category="faq",
        subtype=None,
        language_group=language,
        author_type="team",
        split_group=None,
        expected_outcome="answered",
        n_groups=1,
        retrieval="scored",
        hits=tuple(
            HitRef(
                rank=rank,
                chunk_id=f"{question_id}-relevant" if found else f"{question_id}-c{rank}",
                document_id=f"{document}-{business}",
                source_id="faq",
                version="2026-10",
                chunk_index=rank - 1,
                page=None,
                score=0.9,
                groups=found,
            )
            for rank, found in enumerate(groups, start=1)
        ),
        hit_at_k=hit,
        all_groups_at_k=hit,
        reciprocal_rank=str(Fraction(1, first)) if hit and first is not None else "0",
        outcome="answered",
        trace_id=TRACE,
    )


def unscored(
    question_id: str, retrieval: str, business: str = SUNRISE, language: str = "en"
) -> QuestionResult:
    """A question this run did not score: `not_scored` (no evidence), `unscorable` or `not_run`."""
    no_evidence = retrieval == "not_scored"
    return QuestionResult.model_validate(
        {
            "question_id": question_id,
            "business_id": business,
            "eval_business_id": NAMESPACES[business],
            "category": "out_of_scope" if no_evidence else "faq",
            "subtype": None,
            "language_group": language,
            "author_type": "team",
            "split_group": None,
            "expected_outcome": "dont_know" if no_evidence else "answered",
            "n_groups": 0 if no_evidence else 1,
            "retrieval": retrieval,
            "unscorable_groups": (0,) if retrieval == "unscorable" else (),
            "outcome": "not_run" if retrieval == "not_run" else "dont_know",
            "trace_id": None if retrieval == "not_run" else TRACE,
        }
    )


def twelve(first: Mapping[str, int | None], **options: Any) -> list[QuestionResult]:
    """The 12 questions both runs score: s-* sunrise, p-* paws; s-04, s-05, p-03 Hindi."""
    return [
        scored(
            question_id,
            SUNRISE if question_id.startswith("s-") else PAWS,
            "hi" if question_id in HINDI else "en",
            first=rank,
            **options,
        )
        for question_id, rank in first.items()
    ]


def results_a(**options: Any) -> list[QuestionResult]:
    """16 questions: the twelve, x-01 (scored here, not run in B), x-02 (not run here, scored in
    B), and two scorable in neither run (n-01 has no evidence, n-02 is unscorable in both).
    Scored in one run and unscorable in the other is refused (`SPLIT_CAUSE`), so a question
    scorable in one run only was not run in the other: both runs are partial (`write_folder`)."""
    return [
        *twelve(A_FIRST, **options),
        scored("x-01", first=1),
        unscored("x-02", "not_run", PAWS),
        unscored("n-01", "not_scored"),
        unscored("n-02", "unscorable", PAWS, "hi"),
    ]


def results_b(**options: Any) -> list[QuestionResult]:
    return [
        *twelve(B_FIRST, **options),
        unscored("x-01", "not_run"),
        scored("x-02", PAWS, first=1),
        unscored("n-01", "not_scored"),
        unscored("n-02", "unscorable", PAWS, "hi"),
    ]


def complete(first: Mapping[str, int | None]) -> list[QuestionResult]:
    """14 questions every run ran: the twelve, n-01 and n-02 (a complete run)."""
    return [
        *twelve(first),
        unscored("n-01", "not_scored"),
        unscored("n-02", "unscorable", PAWS, "hi"),
    ]


def write_folder(
    parent: Path, name: str, results: Sequence[QuestionResult], **changes: Any
) -> Path:
    """A TEST-ONLY run folder (run.json with a fingerprint that hashes its own fields). A run with
    a question not run is partial (cap reached), as `harness.final_status` makes it, unless the
    changes give a status."""
    folder = parent / name
    folder.mkdir(parents=True)
    if "status" not in changes and any(result.retrieval == "not_run" for result in results):
        changes = {"status": "partial", "stop": "cap reached"} | changes
    info = run_info(run_id=name, questions_selected=len(results), **changes)
    fingerprint = fingerprint_digest(run_fingerprint_payload(info))
    info = info.model_copy(update={"config_fingerprint": fingerprint})
    (folder / RUN_FILE).write_text(info.model_dump_json(indent=2), encoding="utf-8")
    (folder / RESULTS_FILE).write_text(
        "".join(result.model_dump_json() + "\n" for result in results), encoding="utf-8"
    )
    return folder


def two_runs(
    tmp_path: Path,
    a: Sequence[QuestionResult] | None = None,
    b: Sequence[QuestionResult] | None = None,
    *,
    split: str = "dev",
    a_changes: Mapping[str, Any] | None = None,
    b_changes: Mapping[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Run A (live, started 2026-10-23) and run B (live, started a day later, rag_min_score 0.35,
    another commit), unless the changes say otherwise."""
    folder_a = write_folder(
        tmp_path / "runs",
        f"test-only-a-live-{split}",
        results_a() if a is None else a,
        **(
            LIVE
            | {"split": split, "started_at": "2026-10-23T09:00:00Z"}
            | {"ended_at": "2026-10-23T09:05:00Z"}
            | dict(a_changes or {})
        ),
    )
    folder_b = write_folder(
        tmp_path / "runs",
        f"test-only-b-live-{split}",
        results_b() if b is None else b,
        **(
            LIVE
            | {"split": split, "commit": "b" * 40, "started_at": "2026-10-24T09:00:00Z"}
            | {"ended_at": "2026-10-24T09:05:00Z"}
            | {"config": CONFIG.model_copy(update={"rag_min_score": 0.35})}
            | dict(b_changes or {})
        ),
    )
    return folder_a, folder_b


def _never(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("compare never loads settings, runs, or asks for a cap")


def io_for(
    root: Path,
    head: Mapping[PurePosixPath, bytes] | None = None,
    git_calls: list[str] | None = None,
    functions: MetricFunctions = H2_METRICS,
) -> Io:
    def git() -> GitInfo:
        if git_calls is not None:
            git_calls.append("git")
        return GitInfo(root=root, commit="c" * 40, uncommitted_changes=False)

    return Io(
        load_settings=_never,
        owner_url=_never,
        git=git,
        head_file=lambda _root, path: (head or {}).get(path),
        now=_never,
        stdin=io.StringIO(),
        stdout_is_terminal=lambda: False,
        execute=_never,
        metrics=functions,
    )


def run_compare(
    tmp_path: Path,
    folders: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    **io_options: Any,
) -> tuple[int, str, str]:
    code = cli.run(["compare", str(folders[0]), str(folders[1])], io_for(tmp_path, **io_options))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def every_file(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


# ── The paired counts ────────────────────────────────────────────────────────────────────────


def test_discordant_pairs_are_counted_over_the_questions_scorable_under_both(
    tmp_path: Path,
) -> None:
    # Run B's file lists its questions in reverse: pairing is by question id, never by line.
    a, b = two_runs(tmp_path, b=list(reversed(results_b())))

    comparison = compare(load_run(a, "A"), load_run(b, "B"), H2_METRICS)

    assert comparison.selected == 16
    assert {pair.a.question_id for pair in comparison.pairs} == PAIRED
    assert all(pair.a.question_id == pair.b.question_id for pair in comparison.pairs)
    assert [(e.question_id, e.a, e.b) for e in comparison.one_only] == [
        ("x-01", "scored", "not_run"),
        ("x-02", "not_run", "scored"),
    ]
    # Scorable in neither, by reason: n-01 has no evidence, n-02 is unscorable in both.
    assert (comparison.not_scored, comparison.unscorable_both, comparison.not_run) == (
        ("n-01",),
        ("n-02",),
        (),
    )
    # hit@5 by question, A / B: s-01 hit/hit, s-02 hit/miss, s-03 miss/hit, s-04 hit/hit,
    # s-05 miss/miss, s-06 miss (rank 6 > 5)/hit, p-01 hit/hit, p-02 hit (rank 5)/miss,
    # p-03 hit/hit, p-04 miss/hit, p-05 hit/hit, p-06 hit/hit.
    cells = {
        row.group: (row.n, row.both, row.a_only, row.b_only, row.both_miss)
        for row in comparison.discordant
    }
    assert cells == {
        "all": (12, 6, 2, 3, 1),
        # p-01, p-03, p-05, p-06 both; p-02 A only; p-04 B only.
        "business paws-and-co": (6, 4, 1, 1, 0),
        # s-01, s-04 both; s-02 A only; s-03, s-06 B only; s-05 neither.
        "business sunrise-clinic": (6, 2, 1, 2, 1),
        # en: s-01, p-01, p-05, p-06 both; s-02, p-02 A only; s-03, s-06, p-04 B only.
        "language en": (9, 4, 2, 3, 0),
        # hi: s-04, p-03 both; s-05 neither.
        "language hi": (3, 2, 0, 0, 1),
    }
    # Left out per group (one run only, not scored, unscorable in both, not run): x-01 (sunrise,
    # en) and x-02 (paws, en) one run only; n-01 (sunrise, en) not scored; n-02 (paws, hi)
    # unscorable in both.
    left = {
        row.group: (row.one_run_only, row.not_scored, row.unscorable_both, row.not_run)
        for row in comparison.discordant
    }
    assert left == {
        "all": (2, 1, 1, 0),
        "business paws-and-co": (1, 0, 1, 0),
        "business sunrise-clinic": (1, 1, 0, 0),
        "language en": (2, 1, 0, 0),
        "language hi": (0, 0, 1, 0),
    }
    # A hit, B miss: s-02, p-02; B hit, A miss: s-03, s-06, p-04 (A's order).
    assert [p.a.question_id for p in comparison.pairs if p.hit_a and not p.hit_b] == [
        "s-02",
        "p-02",
    ]
    assert [p.a.question_id for p in comparison.pairs if p.hit_b and not p.hit_a] == [
        "s-03",
        "s-06",
        "p-04",
    ]


def test_each_run_gets_hit_at_5_and_mrr_at_5_through_h2_with_intervals_as_its_summary(
    tmp_path: Path,
) -> None:
    a, b = two_runs(tmp_path)
    run_a = load_run(a, "A")

    comparison = compare(run_a, load_run(b, "B"), H2_METRICS)

    rows_a = {row.group: row for row in comparison.rows_a}
    rows_b = {row.group: row for row in comparison.rows_b}
    assert (
        list(rows_a)
        == list(rows_b)
        == [
            "all",
            "business paws-and-co",
            "business sunrise-clinic",
            "language en",
            "language hi",
        ]
    )
    # A: 6 both + 2 A-only = 8 of 12; B: 6 + 3 = 9 of 12, each with its 95 % Wilson interval.
    for row, x in ((rows_a["all"], 8), (rows_b["all"], 9)):
        wilson = wilson_interval(x, 12)
        assert (row.hit.x, row.hit.n, row.hit.low, row.hit.high) == (
            x,
            12,
            wilson.low,
            wilson.high,
        )
    # MRR@5, A: RR 1 at s-01, s-04, p-01, p-03, p-06; 1/2 at s-02; 1/5 at p-02; 1/3 at p-05;
    # (5 + 1/2 + 1/5 + 1/3) / 12 = (181/30) / 12 = 181/360.
    assert rows_a["all"].mrr.value == "181/360"
    assert rows_a["all"].mrr.ranks == {"1": 5, "2": 1, "3": 1, "4": 0, "5": 1, "none": 4}
    # B: RR 1 at s-01, s-06, p-01, p-04, p-06; 1/3 at s-03, p-05; 1/2 at s-04; 1/4 at p-03;
    # (5 + 1/3 + 1/2 + 1/4 + 1/3) / 12 = (77/12) / 12 = 77/144.
    assert rows_b["all"].mrr.value == "77/144"
    assert rows_b["all"].mrr.ranks == {"1": 5, "2": 1, "3": 2, "4": 1, "5": 0, "none": 3}
    # Per business: sunrise A (1 + 1/2 + 1) / 6 = 5/12, 3 of 6; paws A (3 + 1/5 + 1/3) / 6 =
    # 53/90, 5 of 6; sunrise B (2 + 1/3 + 1/2) / 6 = 17/36, 4 of 6; paws B (3 + 1/4 + 1/3) / 6 =
    # 43/72, 5 of 6. n = 6 < 10: no bootstrap interval.
    for row, x, value in (
        (rows_a["business sunrise-clinic"], 3, "5/12"),
        (rows_a["business paws-and-co"], 5, "53/90"),
        (rows_b["business sunrise-clinic"], 4, "17/36"),
        (rows_b["business paws-and-co"], 5, "43/72"),
    ):
        assert (row.hit.x, row.hit.n, row.mrr.value, row.mrr.low) == (x, 6, value, None)
    # Per language: en A 6 of 9, B 7 of 9; hi A 2 of 3, B 2 of 3.
    assert [(rows_a[g].hit.x, rows_b[g].hit.x) for g in ("language en", "language hi")] == [
        (6, 7),
        (2, 2),
    ]
    # The bootstrap: run A's own seed and resample count, over the 12 paired questions only
    # (x-01, scored in A alone, is not resampled), in A's order, through H2's mean.
    paired = [r.ranked() for r in run_a.results if r.question_id in PAIRED]
    expected = bootstrap_interval(
        paired,
        lambda sample: metrics.mean_reciprocal_rank(_relabelled(sample), COMPARE_K),
        seed=run_a.info.bootstrap_seed,
        resamples=run_a.info.bootstrap_resamples,
    )
    assert (rows_a["all"].mrr.low, rows_a["all"].mrr.high) == (
        str(expected.low),
        str(expected.high),
    )


def test_the_report_prints_counts_ids_both_commits_and_the_differing_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run B's file lists its questions in reverse: the ids below stay in run A's order.
    folders = two_runs(tmp_path, b=list(reversed(results_b())))
    before = every_file(tmp_path)
    git_calls: list[str] = []

    code, out, err = run_compare(tmp_path, folders, capsys, git_calls=git_calls)

    assert (code, err) == (0, "")
    assert every_file(tmp_path) == before  # prints, never writes
    assert git_calls == []  # git is read on the test split only
    assert "**Dev is for choosing: tuning counts from the dev split" in out
    # Each run left one question unrun (x-02 in A, x-01 in B): both partial, so not a result.
    assert (
        "**NOT A RESULT: run A is partial: cap reached; run B is partial: cap reached.**"
    ) in out
    assert "| Live or dry | live | live |" in out
    assert "Test-run ledger" not in out  # dev
    assert f"{'a' * 40} (uncommitted changes: no)" in out
    assert f"{'b' * 40} (uncommitted changes: no)" in out
    assert "- `config.rag_min_score`: run A 0.3, run B 0.35" in out
    assert "## Warnings\n\nNone." in out  # one differing field; a question not run is benign
    assert (
        "16 selected in each run · 12 scorable under both (every count below) · 4 left out,"
        " outside every count below:\n\n"
        "- Scorable in only one run (2): x-01 (A scored, B not_run), x-02 (A not_run,"
        " B scored)\n"
        "- Not scored, no evidence in the dataset (1): n-01\n"
        "- Unscorable in both runs (1): n-02\n"
        "- Not run in one or both runs, scorable in neither (0): none\n"
    ) in out
    lines = out.splitlines()
    assert "| Group | Run | n | hit@5 | MRR@5 | First relevant rank |" in lines
    assert (
        "| Group | n | Both hit | A hit, B miss | B hit, A miss | Both miss |"
        " Left out: one run only | Scorable in neither |"
    ) in lines
    assert "| all | 12 | 6 | 2 | 3 | 1 | 2 | 2 (not scored 1, unscorable in both 1) |" in lines
    assert "| business sunrise-clinic | 6 | 2 | 1 | 2 | 1 | 1 | 1 (not scored 1) |" in lines
    assert "| language hi | 3 | 2 | 0 | 0 | 1 | 0 | 1 (unscorable in both 1) |" in lines
    assert "n = 0, nothing paired" not in out
    assert (
        "Each run's intervals are computed as summary.md computes them, over each row's n paired"
        " questions at k = 5, so they can differ from the run's own summary.md, and they assume"
        " independent questions: 0 of the 12 paired questions share a split_group"
    ) in out
    assert "overlapping intervals do not show that the runs agree" in out
    assert "- A hit, B miss (2): s-02 (sunrise-clinic, en), p-02 (paws-and-co, en)" in out
    assert (
        "- B hit, A miss (3): s-03 (sunrise-clinic, en), s-06 (sunrise-clinic, en),"
        " p-04 (paws-and-co, en)"
    ) in out
    # A percentage from n = 10, Wilson always; MRR 181/360 = 0.503 and 77/144 = 0.535 with a
    # bootstrap interval; per business n = 6: no percentage, no MRR value.
    # Wilson by hand (z = 1.96, z² = 3.8415): 8 of 12 has centre (8/12 + z²/24) / (1 + z²/12)
    # = 0.8267 / 1.3201 = 0.6262 and half-width z·sqrt(p(1-p)/12 + z²/576) / 1.3201 = 0.2356,
    # so 0.391 to 0.862; 9 of 12: 0.6894 ± 0.2217, so 0.468 to 0.911; 5 of 6: 0.7032 ± 0.2667,
    # so 0.437 to 0.970. Shown as whole percentages.
    assert "| all | A | 12 | 8 of 12, 67 % (95 % Wilson 39 % to 86 %) |" in out
    assert "| all | B | 12 | 9 of 12, 75 % (95 % Wilson 47 % to 91 %) |" in out
    assert "| 0.503 (95 % bootstrap" in out
    assert "| 0.535 (95 % bootstrap" in out
    assert "| business paws-and-co | A | 6 | 5 of 6 (95 % Wilson 44 % to 97 %) |" in out
    assert "n < 10: no MRR value; see first relevant rank" in out
    assert "approximate at small n (percentile bootstrap undercovers)" in out


def test_the_reports_prose_outside_its_tables_is_pinned_for_the_main_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every line that is not a table row, exactly: a new sentence anywhere in the prose (a
    # ranking, a verdict, a p-value) fails here, whatever its words. The tables are pinned by
    # their headers and cells in the tests above. Checked by hand against A_FIRST and B_FIRST.
    folders = two_runs(tmp_path)

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert [line for line in out.splitlines() if not line.startswith("|")] == [
        "# Compare run A and run B: hit@5 and MRR@5, paired by question",
        "",
        "**NOT A RESULT: run A is partial: cap reached; run B is partial: cap reached.**",
        "",
        "**Dev is for choosing: tuning counts from the dev split, never the held-out result.**",
        "",
        f"Both runs: support_qa v0.1 (freeze.json sha256 {'0' * 64}), split dev, embedding model"
        " voyage-4, chunks 1200/150; synthetic data (both runs' dataset and corpus manifests).",
        "",
        "",
        "## Fingerprint fields that differ",
        "",
        "- `config.rag_min_score`: run A 0.3, run B 0.35",
        "",
        "## Warnings",
        "",
        "None.",
        "",
        "## Questions",
        "",
        "16 selected in each run · 12 scorable under both (every count below) · 4 left out,"
        " outside every count below:",
        "",
        "- Scorable in only one run (2): x-01 (A scored, B not_run), x-02 (A not_run, B scored)",
        "- Not scored, no evidence in the dataset (1): n-01",
        "- Unscorable in both runs (1): n-02",
        "- Not run in one or both runs, scorable in neither (0): none",
        "",
        "## hit@5 and MRR@5 per run, over the 12 questions scorable under both",
        "",
        "",
        "n counts each group's questions scorable under both; the questions each group leaves out"
        " are counted next to its row in the discordant table below.",
        "Each run's intervals are computed as summary.md computes them, over each row's n paired"
        " questions at k = 5, so they can differ from the run's own summary.md, and they assume"
        " independent questions: 0 of the 12 paired questions share a split_group with another"
        " paired question (translations or twins), and such questions are not independent. Both"
        " runs answered the same questions, so the two runs' intervals are not independent of"
        " each other either: overlapping intervals do not show that the runs agree; the"
        " discordant pairs below are the paired view.",
        "MRR bootstrap intervals: approximate at small n (percentile bootstrap undercovers).",
        "",
        "## Discordant pairs at hit@5, by question id",
        "",
        "",
        "- A hit, B miss (2): s-02 (sunrise-clinic, en), p-02 (paws-and-co, en)",
        "- B hit, A miss (3): s-03 (sunrise-clinic, en), s-06 (sunrise-clinic, en), p-04"
        " (paws-and-co, en)",
        "",
        "Counts and intervals only: no hypothesis test, and no ranking of the two runs. Left-out"
        " questions are outside every count above; the last two columns count them per group.",
        "",
        "## Safety questions answered in one run only (D15 (ii))",
        "",
        "Every safety question that ran in both runs (run A vector, run B vector), whatever its"
        " retrieval status: a safety question without evidence is never scored, so it is never"
        " among the pairs above. Answered means the outcome `answered`. An `error` (the answer"
        " model failed) shows no behaviour, so it is in none of the four answered columns: it has"
        " its own.",
        "",
        "",
        "- A answered, B not (0): none",
        "- B answered, A not (0): none",
        "- Error in one or both runs, behaviour unknown (0): none",
        "- Not run in one or both runs, outside these counts (0): none",
        # No safety question at all in the main fixture: both businesses block (2026-10-27).
        "- n = 0, no safety question ran in both runs: paws-and-co, sunrise-clinic. A business"
        " with n = 0 safety questions run in both gives no evidence and blocks adoption under"
        " D15 (ii), as in the hit half (decided 2026-10-27, shesha).",
        "",
        "Counts and ids only: no ranking of the two runs.",
    ]


TIE_FIRST: Final[dict[str, int | None]] = A_FIRST | {
    "s-02": None, "p-02": None, "s-03": 1, "p-04": 1,
}  # fmt: skip
"""Against `A_FIRST`: s-02 and p-02 A hit, B miss; s-03 and p-04 B hit, A miss (2 and 2)."""


@pytest.mark.parametrize("split", ["dev", "test"])
@pytest.mark.parametrize("order", ["a-then-b", "b-then-a"])
@pytest.mark.parametrize("fixture", ["discordant", "tie"])
def test_no_p_value_and_no_wording_that_ranks_the_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], split: str, order: str, fixture: str
) -> None:
    # Both folder orders (either run may be the one with more discordant hits), and a tie
    # (2 and 2): no wording may rank the runs whichever way the counts fall.
    b = [*twelve(TIE_FIRST), *results_b()[12:]] if fixture == "tie" else None
    folders = two_runs(tmp_path, b=b, split=split)
    head = {LEDGER_PATH: ledger_bytes(*folders)} if split == "test" else None
    if order == "b-then-a":
        folders = (folders[1], folders[0])

    code, out, _ = run_compare(tmp_path, folders, capsys, head=head)

    assert code == 0
    discordant = (2, 2) if fixture == "tie" else (2, 3)
    a_only, b_only = discordant if order == "a-then-b" else discordant[::-1]
    assert f"- A hit, B miss ({a_only}):" in out
    assert f"- B hit, A miss ({b_only}):" in out
    # Matched at the start of a word ("win" must not match "twins", nor "gain" "against").
    words = (
        *("better", "worse", "improv", "regress", "p-value", "p =", "significan", "higher"),
        *("lower", "win", "lost", "lose", "gain", "beat", "outperform", "adopt", "superior"),
    )
    # The only "adopt" allowed: the fixed sentences of the blocking rules (decided 2026-10-25 and
    # 2026-10-27, shesha), which hold whichever run is which. They rank nothing, so they are
    # taken out first; any other wording is still caught. The main fixture has no safety
    # question, so its safety n = 0 sentence and cells are here.
    assert SAFETY_NO_EVIDENCE_BLOCKS in out and NO_EVIDENCE_BLOCKS in out
    lowered = out
    for fixed in (NO_EVIDENCE_BLOCKS, SAFETY_NO_EVIDENCE_BLOCKS, SAFETY_ERROR_BLOCKS):
        lowered = lowered.replace(fixed, "")
    lowered = lowered.lower()
    assert [word for word in words if re.search(rf"\b{re.escape(word)}", lowered)] == []
    assert ": no hypothesis test, and no ranking of the two runs." in out
    # No difference column (B minus A, a delta) in either table: the headers are exactly these.
    lines = out.splitlines()
    assert "| Group | Run | n | hit@5 | MRR@5 | First relevant rank |" in lines
    assert (
        "| Group | n | Both hit | A hit, B miss | B hit, A miss | Both miss |"
        " Left out: one run only | Scorable in neither |"
    ) in lines
    assert [line for line in lines if line.startswith("| Group")] == [
        "| Group | Run | n | hit@5 | MRR@5 | First relevant rank |",
        "| Group | n | Both hit | A hit, B miss | B hit, A miss | Both miss |"
        " Left out: one run only | Scorable in neither |",
    ]


def test_hit_at_5_comes_from_h2_at_k_5_even_when_a_runs_metric_k_is_another(
    tmp_path: Path,
) -> None:
    # Run A scored at metric k 6, so its file stores s-06 (first relevant at rank 6) as a hit:
    # compare asks H2 at k = 5 and counts it as a miss, as in the k-5 runs above.
    a, b = two_runs(
        tmp_path,
        a=[*twelve(A_FIRST, metric_k=6), *results_a()[12:]],
        a_changes={"config": CONFIG.model_copy(update={"metric_k": 6})},
    )
    stored = {r.question_id: r.hit_at_k for r in load_run(a, "A").results}
    assert stored["s-06"] is True

    comparison = compare(load_run(a, "A"), load_run(b, "B"), H2_METRICS)

    all_row = comparison.discordant[0]
    assert (all_row.group, all_row.both, all_row.a_only, all_row.b_only) == ("all", 6, 2, 3)
    # Run A's own row too: 8 of 12 at k = 5 (9 at k = 6), MRR 181/360, ranks 1..5 only.
    row_a = comparison.rows_a[0]
    assert (row_a.group, row_a.hit.x, row_a.mrr.value) == ("all", 8, "181/360")
    assert list(row_a.mrr.ranks) == ["1", "2", "3", "4", "5", "none"]
    assert [d.field for d in comparison.differs] == ["config.metric_k", "config.rag_min_score"]


def test_the_means_are_h2s_over_exactly_the_paired_questions(tmp_path: Path) -> None:
    a, b = two_runs(tmp_path)
    asked: list[tuple[str, frozenset[str], int]] = []

    def mean_hit_at_k(records: Sequence[RankedRelevance], k: int) -> Fraction:
        asked.append(("mean_hit_at_k", frozenset(r.question_id for r in records), k))
        return metrics.mean_hit_at_k(records, k)

    def mean_reciprocal_rank(records: Sequence[RankedRelevance], k: int) -> Fraction:
        if not any("~" in r.question_id for r in records):  # not a bootstrap resample
            asked.append(("mean_reciprocal_rank", frozenset(r.question_id for r in records), k))
        return metrics.mean_reciprocal_rank(records, k)

    spy = MetricFunctions(
        hit_at_k=metrics.hit_at_k,
        all_groups_at_k=metrics.all_groups_at_k,
        reciprocal_rank=metrics.reciprocal_rank,
        mean_hit_at_k=mean_hit_at_k,
        mean_reciprocal_rank=mean_reciprocal_rank,
    )

    compare(load_run(a, "A"), load_run(b, "B"), spy)

    # Once per run and group: all, two businesses, two languages; never x-01 (scored in A only).
    assert asked.count(("mean_hit_at_k", PAIRED, COMPARE_K)) == 2
    assert asked.count(("mean_reciprocal_rank", PAIRED, COMPARE_K)) == 2
    assert len(asked) == 2 * 2 * 5
    assert all("x-01" not in ids and k == COMPARE_K for _, ids, k in asked)


def spy_metrics(**replaced: Any) -> MetricFunctions:
    """H2's functions, with some replaced by spies that call H2 themselves."""
    functions = {
        "hit_at_k": metrics.hit_at_k,
        "all_groups_at_k": metrics.all_groups_at_k,
        "reciprocal_rank": metrics.reciprocal_rank,
        "mean_hit_at_k": metrics.mean_hit_at_k,
        "mean_reciprocal_rank": metrics.mean_reciprocal_rank,
    }
    return MetricFunctions(**(functions | replaced))


def test_pairing_asks_h2s_hit_at_k_at_5_for_each_paired_question_in_both_runs(
    tmp_path: Path,
) -> None:
    # Both runs scored at metric k 6, so the stored-value check asks H2 at k = 6 and every call
    # at k = 5 comes from compare's own k. B's stored hits at k 6 equal H2's at k 5 (no first
    # rank of 6 in B_FIRST), so only these calls show that B's pair cells come from H2.
    config_6 = CONFIG.model_copy(update={"metric_k": 6})
    a, b = two_runs(
        tmp_path,
        a=results_a(metric_k=6),
        b=results_b(metric_k=6),
        a_changes={"config": config_6},
        b_changes={"config": config_6.model_copy(update={"rag_min_score": 0.35})},
    )
    run_a, run_b = load_run(a, "A"), load_run(b, "B")
    calls: Counter[tuple[str, tuple[tuple[int, ...], ...]]] = Counter()

    def hit_at_k(record: RankedRelevance, k: int) -> bool:
        if k == COMPARE_K:
            calls[(record.question_id, record.groups_by_rank)] += 1
        return metrics.hit_at_k(record, k)

    compare(run_a, run_b, spy_metrics(hit_at_k=hit_at_k))

    # Per paired question and run: 1 call for its pair cell + 3 for its rows (all, its
    # business, its language) = 4. A question whose two ranked lists are equal (s-01, s-05,
    # p-01, p-05, p-06) is one key with 8. x-01 and x-02 (one run only) are never asked at k 5.
    expected: Counter[tuple[str, tuple[tuple[int, ...], ...]]] = Counter()
    for run in (run_a, run_b):
        for result in run.results:
            if result.question_id in PAIRED:
                expected[(result.question_id, result.ranked().groups_by_rank)] += 4
    assert calls == expected
    assert sum(calls.values()) == 2 * 12 * 4
    assert sorted(q for (q, _), count in calls.items() if count == 8) == [
        "p-01",
        "p-05",
        "p-06",
        "s-01",
        "s-05",
    ]


def test_the_cli_compares_through_io_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `cli.run(["compare", ...])` must use the Io's MetricFunctions (H2 in production), never a
    # set of its own.
    asked: list[int] = []

    def mean_hit_at_k(records: Sequence[RankedRelevance], k: int) -> Fraction:
        asked.append(k)
        return metrics.mean_hit_at_k(records, k)

    folders = two_runs(tmp_path)

    code, _, err = run_compare(
        tmp_path, folders, capsys, functions=spy_metrics(mean_hit_at_k=mean_hit_at_k)
    )

    assert (code, err) == (0, "")
    assert asked == [COMPARE_K] * (2 * 5)  # two runs, each: all, two businesses, two languages


def test_the_bootstrap_resamples_h2s_mrr_exactly_each_runs_count_from_n_10_only(
    tmp_path: Path,
) -> None:
    # Run A resamples 200 times (run_info's default), run B 120 (not in the fingerprint). Only
    # "all" (n = 12) reaches bootstrap_min_n 10; en (n = 9) and the rest do not.
    a, b = two_runs(tmp_path, b_changes={"bootstrap_resamples": 120})
    calls: list[frozenset[str] | str] = []

    def mean_reciprocal_rank(records: Sequence[RankedRelevance], k: int) -> Fraction:
        assert k == COMPARE_K
        resample = any("~" in r.question_id for r in records)
        calls.append("resample" if resample else frozenset(r.question_id for r in records))
        return metrics.mean_reciprocal_rank(records, k)

    compare(
        load_run(a, "A"), load_run(b, "B"), spy_metrics(mean_reciprocal_rank=mean_reciprocal_rank)
    )

    # Each run of consecutive resample calls collapsed to its length.
    collapsed: list[frozenset[str] | int] = []
    for call in calls:
        if isinstance(call, frozenset):
            collapsed.append(call)
        elif collapsed and isinstance(collapsed[-1], int):
            collapsed[-1] += 1
        else:
            collapsed.append(1)
    paws = frozenset(q for q in PAIRED if q.startswith("p-"))
    sunrise = frozenset(q for q in PAIRED if q.startswith("s-"))
    hi = PAIRED & HINDI
    per_run = [paws, sunrise, PAIRED - hi, hi]  # business paws, business sunrise, en, hi
    assert collapsed == [PAIRED, 200, *per_run, PAIRED, 120, *per_run]


def test_a_business_unscorable_in_both_runs_keeps_its_rows_with_n_0_and_its_left_out_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every paws question (p-03 Hindi) is unscorable in both runs; n-03 (sunrise, en) is
    # unscorable in A, and B stopped before it (not run).
    def paws_unscorable(first: Mapping[str, int | None]) -> list[QuestionResult]:
        return [
            r
            if r.business_id == SUNRISE
            else unscored(r.question_id, "unscorable", PAWS, r.language_group)
            for r in twelve(first)
        ]

    folders = two_runs(
        tmp_path,
        a=[*paws_unscorable(A_FIRST), unscored("n-03", "unscorable")],
        b=[*paws_unscorable(B_FIRST), unscored("n-03", "not_run")],
        b_changes={"status": "partial", "stop": "cap reached"},
    )
    comparison = compare(load_run(folders[0], "A"), load_run(folders[1], "B"), H2_METRICS)

    # Paired: s-01..s-06 only. sunrise as in the main fixture: s-01, s-04 both; s-02 A only;
    # s-03, s-06 B only; s-05 neither. en: s-01 both, s-02 A only, s-03 and s-06 B only.
    # hi: s-04 both, s-05 neither.
    rows = {
        row.group: (
            (row.n, row.both, row.a_only, row.b_only, row.both_miss),
            (row.one_run_only, row.not_scored, row.unscorable_both, row.not_run),
        )
        for row in comparison.discordant
    }
    assert rows == {
        "all": ((6, 2, 1, 2, 1), (0, 0, 6, 1)),
        "business paws-and-co": ((0, 0, 0, 0, 0), (0, 0, 6, 0)),
        "business sunrise-clinic": ((6, 2, 1, 2, 1), (0, 0, 0, 1)),
        "language en": ((4, 1, 1, 2, 0), (0, 0, 5, 1)),
        "language hi": ((2, 1, 0, 0, 1), (0, 0, 1, 0)),
    }
    paws = [row for row in (*comparison.rows_a, *comparison.rows_b) if "paws" in row.group]
    assert [(row.hit.n, row.mrr.n) for row in paws] == [(0, 0), (0, 0)]
    assert comparison.unscorable_both == ("p-01", "p-02", "p-03", "p-04", "p-05", "p-06")
    assert comparison.not_run == ("n-03",)

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    lines = out.splitlines()
    # A business with n = 0 gives no evidence and blocks adoption (decided 2026-10-25, shesha).
    assert (
        "| business paws-and-co | n = 0: no evidence (blocks adoption under D15) | 0 | 0 | 0 | 0"
        " | 0 | 6 (unscorable in both 6) |"
    ) in lines
    assert "| language en | 4 | 1 | 1 | 2 | 0 | 0 | 6 (unscorable in both 5, not run 1) |" in lines
    for run in ("A", "B"):
        assert (
            f"| business paws-and-co | {run} | 0 | none scorable under both |"
            " none scorable under both |  |"
        ) in lines
    assert (
        "- n = 0, nothing paired: business paws-and-co. Each of these groups' questions, if it"
        " has any, is left out (the last two columns), so its row counts nothing in either run."
    ) in lines
    assert (
        "- A business with n = 0 gives no evidence and blocks adoption under D15 (ii): the"
        " retrieval mode is platform-wide (decided 2026-10-25, shesha)."
    ) in lines
    assert "- Unscorable in both runs (6): p-01, p-02, p-03, p-04, p-05, p-06" in lines
    assert "- Not run in one or both runs, scorable in neither (1): n-03" in lines


def test_a_language_with_n_0_is_named_but_never_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every Hindi question (s-04, s-05, p-03, n-02) is unscorable in both runs: the language row
    # has n = 0, but D15 (ii) reads per business, and both businesses still pair questions.
    def hindi_unscorable(results: list[QuestionResult]) -> list[QuestionResult]:
        return [
            unscored(r.question_id, "unscorable", r.business_id, "hi")
            if r.language_group == "hi"
            else r
            for r in results
        ]

    folders = two_runs(
        tmp_path, a=hindi_unscorable(complete(A_FIRST)), b=hindi_unscorable(complete(B_FIRST))
    )

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    lines = out.splitlines()
    assert "| language hi | 0 | 0 | 0 | 0 | 0 | 0 | 4 (unscorable in both 4) |" in lines
    assert "- n = 0, nothing paired: language hi." in out
    # Neither half's blocking applies to a language (this fixture has no safety question, so
    # each business's safety row has n = 0 and blocks: that is the safety half, per business).
    assert not any(line.startswith("| language") and NO_EVIDENCE_BLOCKS in line for line in lines)
    assert "- A business with n = 0 gives no evidence" not in out  # the hit half's line


def test_a_manifest_business_with_no_selected_question_has_an_n_0_row_that_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both runs registered a third business (its namespace is in run.json), but the split
    # selected none of its questions: it keeps its rows with n = 0 instead of vanishing
    # [proposal], and gives no evidence under D15 (ii).
    third = NAMESPACES | {"third-biz": "third-biz-ev-cccccccccc"}
    folders = two_runs(
        tmp_path,
        a=complete(A_FIRST),
        b=complete(B_FIRST),
        a_changes={"namespaces": third},
        b_changes={"namespaces": third},
    )
    comparison = compare(load_run(folders[0], "A"), load_run(folders[1], "B"), H2_METRICS)

    assert [row.group for row in comparison.discordant] == [
        "all",
        "business paws-and-co",
        "business sunrise-clinic",
        "business third-biz",
        "language en",
        "language hi",
    ]
    assert [(row.hit.n, row.mrr.n) for row in comparison.rows_a if "third" in row.group] == [(0, 0)]

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    lines = out.splitlines()
    assert (
        "| business third-biz | n = 0: no evidence (blocks adoption under D15) | 0 | 0 | 0 | 0 | 0"
        " | 0 |"
    ) in lines
    assert "- n = 0, nothing paired: business third-biz." in out
    assert "- A business with n = 0 gives no evidence and blocks adoption under D15 (ii)" in out


def test_shared_split_groups_are_counted_over_the_paired_questions_only(tmp_path: Path) -> None:
    # g1: s-01 and s-02 paired, x-01 left out (scored in run A only). g2: s-03 paired, its twin
    # x-02 left out. Among the 12 paired questions, g1 has 2 (counted) and g2 has 1 (not).
    split_groups = {"s-01": "g1", "s-02": "g1", "x-01": "g1", "s-03": "g2", "x-02": "g2"}

    def grouped(results: list[QuestionResult]) -> list[QuestionResult]:
        return [
            r.model_copy(update={"split_group": split_groups.get(r.question_id)}) for r in results
        ]

    a, b = two_runs(tmp_path, a=grouped(results_a()), b=grouped(results_b()))

    comparison = compare(load_run(a, "A"), load_run(b, "B"), H2_METRICS)

    assert comparison.shared_split_group == 2
    assert (
        "2 of the 12 paired questions share a split_group with another paired question"
        " (translations or twins), and such questions are not independent."
    ) in render_comparison(comparison)


# ── Refusals ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "b_changes"),
    [
        ("dataset", {"dataset": "other_qa"}),
        ("dataset_version", {"dataset_version": "v0.2"}),
        ("freeze_sha256", {"freeze_sha256": "1" * 64}),
        ("split", {"split": "test"}),
        ("embedding_model", {"embedding_model": "voyage-4-lite"}),
    ],
)
def test_two_runs_of_another_dataset_version_freeze_split_or_model_are_refused_by_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str, b_changes: dict[str, Any]
) -> None:
    folders = two_runs(tmp_path, b_changes=b_changes)

    code, out, err = run_compare(tmp_path, folders, capsys)

    assert (code, out) == (1, "")
    assert f"compare: refused: {field} differs (run A " in err


@pytest.mark.parametrize("field", ["max_chars", "overlap_chars"])
def test_two_chunk_configs_are_refused_with_a_pointer_to_the_manual_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str
) -> None:
    config = CONFIG.model_copy(update={field: 100, "rag_min_score": 0.35})
    folders = two_runs(tmp_path, b_changes={"config": config})

    code, out, err = run_compare(tmp_path, folders, capsys)

    assert (code, out) == (1, "")
    assert f"the chunk config differs (config.{field}: run A " in err
    assert "comparing two chunk configs stays manual" in err
    assert "ADR 0013 decision 8; M3 plan D14" in err


@pytest.mark.parametrize("side", ["A", "B"])
def test_a_run_json_whose_fingerprint_is_not_its_own_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], side: str
) -> None:
    a, b = two_runs(tmp_path)
    edited = a if side == "A" else b
    info = read_run(edited).model_copy(update={"config_fingerprint": "f" * 64})
    (edited / RUN_FILE).write_text(info.model_dump_json(indent=2), encoding="utf-8")

    code, _, err = run_compare(tmp_path, (a, b), capsys)

    assert code == 1
    assert f"run {side}: run.json's config_fingerprint is not the hash of its own fields" in err


@pytest.mark.parametrize("side", ["A", "B"])
def test_a_stored_value_h2_does_not_give_again_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], side: str
) -> None:
    # s-02 finds its group at rank 2 in run A and nothing in run B, yet the edited line says
    # hit at rank 1 (RR 1).
    def edited(results: list[QuestionResult]) -> list[QuestionResult]:
        return [
            r.model_copy(update={"hit_at_k": True, "all_groups_at_k": True, "reciprocal_rank": "1"})
            if r.question_id == "s-02"
            else r
            for r in results
        ]

    if side == "A":
        folders = two_runs(tmp_path, a=edited(results_a()))
    else:
        folders = two_runs(tmp_path, b=edited(results_b()))

    code, _, err = run_compare(tmp_path, folders, capsys)

    assert code == 1
    assert f"run {side}: question s-02: the stored metric values differ from H2's" in err


@pytest.mark.parametrize("side", ["A", "B"])
def test_a_search_k_below_5_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], side: str
) -> None:
    config = CONFIG.model_copy(update={"search_k": 4, "metric_k": 4})
    if side == "A":
        folders = two_runs(tmp_path, a=results_a(metric_k=4), a_changes={"config": config})
    else:
        folders = two_runs(tmp_path, b=results_b(metric_k=4), b_changes={"config": config})

    code, _, err = run_compare(tmp_path, folders, capsys)

    assert code == 1
    assert f"run {side} searched 4 hit(s): hit@5 would report a shallow search" in err


def test_two_different_question_sets_or_a_changed_question_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(tmp_path / "missing", b=results_b()[:-1])  # without n-02
    code, _, err = run_compare(tmp_path, folders, capsys)
    assert code == 1
    assert "the runs selected different questions (1 in one run only, first: n-02)" in err

    changed = [
        r.model_copy(update={"language_group": "hi"}) if r.question_id == "s-01" else r
        for r in results_b()
    ]
    folders = two_runs(tmp_path / "changed", b=changed)
    code, _, err = run_compare(tmp_path, folders, capsys)
    assert code == 1
    assert "question s-01: language_group differs between the runs" in err


def test_the_same_folder_twice_or_a_missing_folder_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, _ = two_runs(tmp_path)

    code, _, err = run_compare(tmp_path, (a, a), capsys)
    assert code == 1
    assert "run A and run B are the same folder" in err

    code, _, err = run_compare(tmp_path, (a, tmp_path / "nowhere"), capsys)
    assert code == 1
    assert "run.json is missing" in err


def test_compare_needs_exactly_two_folders(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.run(["compare", str(tmp_path)], io_for(tmp_path))
    assert exit_info.value.code == 2


# ── Fingerprint fields, warnings and labels ──────────────────────────────────────────────────


def test_every_other_differing_field_is_printed_and_more_than_one_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(tmp_path, b_changes={"prompt_version": "rag-answer.2"})

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert "- `config.rag_min_score`: run A 0.3, run B 0.35" in out
    assert "- `prompt_version`: run A rag-answer.1, run B rag-answer.2" in out
    assert (
        "- **WARNING:** 2 fingerprint fields differ: no paired difference below can be put down"
        " to one of them alone."
    ) in out


def test_two_runs_of_one_fingerprint_say_so_and_warn_of_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two complete live runs, each of which ran every question: a result, nothing to warn of.
    folders = two_runs(
        tmp_path, a=complete(A_FIRST), b=complete(B_FIRST), b_changes={"config": CONFIG}
    )

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert "NOT A RESULT" not in out
    assert "None: both runs have every fingerprint field in common." in out
    assert "## Warnings\n\nNone." in out


def test_a_block_present_in_one_payload_only_is_one_difference() -> None:
    # PR 5's `retrieval` block is left out in vector mode: vector against hybrid is one field.
    def payload(retrieval: dict[str, object] | None) -> dict[str, object]:
        config: dict[str, object] = {"search_k": 6}
        if retrieval is not None:
            config["retrieval"] = retrieval
        return {"prompt_version": "p1", "config": config}

    vector = payload(None)
    hybrid = payload({"mode": "hybrid"})
    rrf_30 = payload({"mode": "hybrid", "rrf_k": 30})
    rrf_60 = payload({"mode": "hybrid", "rrf_k": 60})

    one = payload_differences(vector, hybrid)
    assert [(d.field, d.a, d.b) for d in one] == [
        ("config.retrieval", "(absent: vector mode)", '{"mode": "hybrid"}')
    ]
    nested = payload_differences(rrf_30, rrf_60)
    assert [(d.field, d.a, d.b) for d in nested] == [("config.retrieval.rrf_k", "30", "60")]
    assert payload_differences(vector, vector) == []
    # Any other absent field is only "(absent)": its absence means nothing more.
    other = payload_differences({"config": {"x": 1}}, {"config": {}})
    assert [(d.field, d.a, d.b) for d in other] == [("config.x", "1", "(absent)")]


def ingest(business: str, chunks: int) -> IngestInfo:
    return IngestInfo(
        business_id=business,
        eval_business_id=NAMESPACES[business],
        documents=9,
        created=9 if chunks else 0,
        chunks_written=chunks,
        retired_versions=0,
    )


@pytest.mark.parametrize(
    ("a_changes", "b_changes", "later"),
    [
        ({}, {"ingest": (ingest(SUNRISE, 40), ingest(PAWS, 0))}, "B"),
        (
            {"started_at": "2026-10-25T09:00:00Z", "ingest": (ingest(SUNRISE, 40),)},
            {"ingest": (ingest(SUNRISE, 0),)},
            "A",
        ),
    ],
)
def test_a_later_run_that_wrote_chunks_into_a_shared_namespace_is_a_re_ingest_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    a_changes: dict[str, Any],
    b_changes: dict[str, Any],
    later: str,
) -> None:
    folders = two_runs(tmp_path, a_changes=a_changes, b_changes=b_changes)

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    earlier = "A" if later == "B" else "B"
    assert (
        f"run {later} (test-only-{later.lower()}-live-dev), the later of the two, wrote 40"
        f" chunk(s) into eval business {NAMESPACES[SUNRISE]}, which run {earlier} also used: its"
        " vectors were re-created between the runs (D3)"
    ) in out
    assert out.count("the later of the two") == 1  # paws wrote 0 chunks: no warning for it


def test_the_earlier_runs_first_ingest_is_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(
        tmp_path,
        a_changes={"ingest": (ingest(SUNRISE, 40), ingest(PAWS, 30))},
        b_changes={"ingest": (ingest(SUNRISE, 0), ingest(PAWS, 0))},
    )

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert "re-created" not in out


def test_a_later_ingest_into_a_namespace_the_earlier_run_never_used_is_no_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run B (the later) wrote 40 chunks into a third eval business that run A never registered,
    # and none into the two A used: nothing A searched was re-created.
    third = IngestInfo(
        business_id="third-biz",
        eval_business_id="third-biz-ev-cccccccccc",
        documents=9,
        created=9,
        chunks_written=40,
        retired_versions=0,
    )
    folders = two_runs(
        tmp_path,
        b_changes={
            "namespaces": NAMESPACES | {"third-biz": third.eval_business_id},
            "ingest": (ingest(SUNRISE, 0), ingest(PAWS, 0), third),
        },
    )

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert "re-created" not in out
    assert "## Warnings\n\nNone." in out


def test_another_document_id_for_the_same_source_and_version_is_a_re_ingest_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Neither run's ingest wrote chunks (a third run or command re-created the documents), but
    # run B's hits name other document ids for the same eval business, source and version.
    b = [*twelve(B_FIRST, document="doc-2"), *results_b()[12:]]
    folders = two_runs(tmp_path, b=b)

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert (
        f"2 document(s) have another id in each run for the same eval business, source and"
        f" version ({NAMESPACES[PAWS]} faq 2026-10; {NAMESPACES[SUNRISE]} faq 2026-10): they"
        " were deleted and ingested again between the runs (D3)"
    ) in out


def _regrouped(result: QuestionResult) -> QuestionResult:
    """`result` with its chunks matching no group, and stored values as H2 gives them for that."""
    hits = tuple(hit.model_copy(update={"groups": ()}) for hit in result.hits)
    return result.model_copy(
        update={"hits": hits, "hit_at_k": False, "all_groups_at_k": False, "reciprocal_rank": "0"}
    )


@pytest.mark.parametrize(
    ("a", "b", "cause"),
    [
        # Run B's s-01 ranks the same chunk first as run A's, but with no matched group.
        (
            complete(A_FIRST),
            [_regrouped(r) if r.question_id == "s-01" else r for r in complete(B_FIRST)],
            REGROUPED_CAUSE,
        ),
        # s-02 is scored in A and unscorable in B, then the other way round.
        (
            complete(A_FIRST),
            [
                unscored("s-02", "unscorable") if r.question_id == "s-02" else r
                for r in complete(B_FIRST)
            ],
            SPLIT_CAUSE,
        ),
        (
            [
                unscored("s-02", "unscorable") if r.question_id == "s-02" else r
                for r in complete(A_FIRST)
            ],
            complete(B_FIRST),
            SPLIT_CAUSE,
        ),
    ],
    ids=["regrouped-in-b", "scored-a-unscorable-b", "unscorable-a-scored-b"],
)
def test_a_changed_scoring_rule_is_refused_with_one_fixed_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    a: list[QuestionResult],
    b: list[QuestionResult],
    cause: str,
) -> None:
    # Decided 2026-10-25 (shesha): refuse, not warn. One freeze and chunk config store the same
    # chunks, so either split means the code deciding relevant or unscorable changed.
    folders = two_runs(tmp_path, a=a, b=b)

    code, out, err = run_compare(tmp_path, folders, capsys)

    assert (code, out) == (1, "")
    assert err == f"{cli.PROG} compare: refused: {SCORING_RULE_REFUSAL.format(cause=cause)}\n"


def test_the_scoring_rule_refusal_text_is_fixed() -> None:
    # The literal, so that the message itself is reviewed, not only the constant's use.
    assert SCORING_RULE_REFUSAL.format(cause=SPLIT_CAUSE) == (
        "the scoring rule differs between the runs: a question is scored in one run and"
        " unscorable in the other. One freeze and chunk config store the same chunks, so only"
        " the code deciding relevant or unscorable can split them (D3), and the config"
        " fingerprint does not cover that code. Re-run both runs on one commit."
    )
    assert REGROUPED_CAUSE == (
        "a question scored in both runs has a chunk whose matched groups differ between the runs"
    )


@pytest.mark.parametrize(
    ("a_changes", "reason"),
    [
        ({"dry_run": True}, "run A is a dry run (fake embedder, canned answers)"),
        ({"status": "partial", "stop": "cap reached"}, "run A is partial: cap reached"),
        (
            {"status": "aborted_leak", "stop": "CROSS-BUSINESS LEAK: question s-01"},
            "run A aborted on a leak, a hard gate: CROSS-BUSINESS LEAK: question s-01",
        ),
    ],
)
def test_a_dry_or_unfinished_run_makes_the_report_not_a_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], a_changes: dict[str, Any], reason: str
) -> None:
    folders = two_runs(tmp_path, a=complete(A_FIRST), b=complete(B_FIRST), a_changes=a_changes)

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert f"**NOT A RESULT: {reason}.**" in out


@pytest.mark.parametrize(
    ("a_dry", "b_dry", "row"),
    [
        (False, False, "| Live or dry | live | live |"),
        (True, False, f"| Live or dry | {DRY_CELL} | live |"),
        (False, True, f"| Live or dry | live | {DRY_CELL} |"),
        (True, True, f"| Live or dry | {DRY_CELL} | {DRY_CELL} |"),
    ],
    ids=["live-live", "dry-live", "live-dry", "dry-dry"],
)
def test_the_live_or_dry_cell_reads_each_runs_own_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], a_dry: bool, b_dry: bool, row: str
) -> None:
    # TEST-ONLY: the dry flag alone is changed (one embedding model in both, so not refused).
    folders = two_runs(
        tmp_path,
        a=complete(A_FIRST),
        b=complete(B_FIRST),
        a_changes={"dry_run": a_dry},
        b_changes={"dry_run": b_dry},
    )

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert [line for line in out.splitlines() if line.startswith("| Live or dry |")] == [row]
    assert ("NOT A RESULT" in out) is (a_dry or b_dry)


def test_two_dry_runs_are_not_a_result_and_a_question_one_left_unrun_is_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run B stopped before p-06: scorable under A only, so the paired n is 11.
    b = [r if r.question_id != "p-06" else unscored("p-06", "not_run", PAWS) for r in results_b()]
    dry = {"dry_run": True, "embeddings_provider": "fake", "embedding_model": "fake-embedding-1024"}
    folders = two_runs(
        tmp_path,
        b=b,
        a_changes=dry,
        b_changes=dry | {"status": "partial", "stop": "cap reached"},
    )

    code, out, err = run_compare(tmp_path, folders, capsys)

    # A question one run did not run is benign: no scoring-rule refusal.
    assert (code, err) == (0, "")
    assert (
        "**NOT A RESULT: run A is a dry run (fake embedder, canned answers); run A is partial:"
        " cap reached; run B is a dry run (fake embedder, canned answers); run B is partial: cap"
        " reached.**"
    ) in out
    assert f"| Live or dry | {DRY_CELL} | {DRY_CELL} |" in out.splitlines()
    assert "11 scorable under both" in out
    assert (
        "- Scorable in only one run (3): p-06 (A scored, B not_run), x-01 (A scored, B not_run),"
        " x-02 (A not_run, B scored)"
    ) in out
    # p-06 was a hit in both; it is left out like x-01 and x-02.
    assert "| all | 11 | 5 | 2 | 3 | 1 | 3 | 2 (not scored 1, unscorable in both 1) |" in out


# ── The test split ───────────────────────────────────────────────────────────────────────────


def ledger_bytes(
    *folders: Path, fingerprint: str | None = None, run_id: str | None = None
) -> bytes:
    """A TEST-ONLY ledger naming `folders` (as committed in HEAD in these tests)."""
    lines = []
    for folder in folders:
        info = read_run(folder)
        entry = LedgerEntry(
            dataset=info.dataset,
            version=info.dataset_version,
            freeze_sha256=info.freeze_sha256,
            config_fingerprint=fingerprint or info.config_fingerprint,
            run_id=run_id or info.run_id,
            commit=info.commit,
            started_at=info.started_at,
            rerun_reason=info.test_rerun_reason,
        )
        lines.append(entry.model_dump_json() + "\n")
    return "".join(lines).encode("utf-8")


def test_two_ledgered_test_runs_are_counts_only_and_labelled_held_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(tmp_path, split="test")
    git_calls: list[str] = []

    code, out, err = run_compare(
        tmp_path, folders, capsys, head={LEDGER_PATH: ledger_bytes(*folders)}, git_calls=git_calls
    )

    assert (code, err, git_calls) == (0, "", ["git"])
    assert "**Held-out: reported, not used to choose.** Counts only" in out
    assert "Dev is for choosing" not in out
    assert "| all | 12 | 6 | 2 | 3 | 1 |" in out  # the same pairs as on dev
    assert "| all | A | 12 | 8 of 12 (counts only) | counts only | 1: 5 · 2: 1 · 3: 1" in out
    assert "| all | B | 12 | 9 of 12 (counts only) | counts only | 1: 5 · 2: 1 · 3: 2" in out
    # No percentage, interval or MRR value anywhere.
    assert ("%" in out, "Wilson" in out, "bootstrap" in out, "181/360" in out) == (
        False,
        False,
        False,
        False,
    )


def test_a_ledgered_test_rerun_is_named_in_the_banner_and_the_run_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run B is a ledgered RERUN of a test config that ran once before (ADR 0013 decision 5).
    rerun = {
        "test_rerun_reason": "cap stop on first try",
        "test_runs_before_config": 1,
        "test_runs_before_freeze": 1,
    }
    folders = two_runs(tmp_path, split="test", b_changes=rerun)

    code, out, err = run_compare(
        tmp_path, folders, capsys, head={LEDGER_PATH: ledger_bytes(*folders)}
    )

    assert (code, err) == (0, "")
    assert (
        "**Held-out: reported, not used to choose.** Counts only: no percentage, interval or MRR"
        " value on the test split. **Run B is a held-out test RERUN (reason: cap stop on first"
        " try).**"
    ) in out
    assert "Run A is a held-out test RERUN" not in out
    assert (
        "| Test-run ledger | 0 earlier test run(s) of this config, 0 of this frozen version (any"
        " config); rerun reason: none | 1 earlier test run(s) of this config, 1 of this frozen"
        " version (any config); rerun reason: cap stop on first try |"
    ) in out.splitlines()


def test_a_test_run_not_in_the_committed_ledger_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = folders = two_runs(tmp_path, split="test")
    # The ledger in the working tree does not count: only HEAD's.
    (tmp_path / LEDGER_PATH).parent.mkdir(parents=True)
    (tmp_path / LEDGER_PATH).write_bytes(ledger_bytes(a, b))

    code, out, err = run_compare(tmp_path, folders, capsys)
    assert (code, out) == (1, "")
    assert "HEAD has no test-runs.jsonl for this dataset: on the test split, compare takes" in err

    code, _, err = run_compare(tmp_path, folders, capsys, head={LEDGER_PATH: ledger_bytes(a)})
    assert code == 1
    assert "run B (test-only-b-live-test) is not in the ledger with its dataset, version" in err

    other = ledger_bytes(a) + ledger_bytes(b, fingerprint="e" * 64)
    code, _, err = run_compare(tmp_path, folders, capsys, head={LEDGER_PATH: other})
    assert code == 1
    assert "run B (test-only-b-live-test) is not in the ledger" in err


def test_a_ledger_line_with_matching_fields_but_another_run_id_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run B's dataset, version, freeze and fingerprint are in the ledger, but on another run's
    # line: the ledger names this config's test run, not this folder.
    a, b = folders = two_runs(tmp_path, split="test")
    head = {LEDGER_PATH: ledger_bytes(a) + ledger_bytes(b, run_id="test-only-another-test-run")}

    code, out, err = run_compare(tmp_path, folders, capsys, head=head)

    assert (code, out) == (1, "")
    assert "run B (test-only-b-live-test) is not in the ledger with its dataset, version" in err
    # The same ledger with B's own run id is taken.
    code, _, _ = run_compare(tmp_path, folders, capsys, head={LEDGER_PATH: ledger_bytes(a, b)})
    assert code == 0


def test_a_renamed_test_folder_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = two_runs(tmp_path, split="test")
    head = {LEDGER_PATH: ledger_bytes(a, b)}
    renamed = b.rename(b.with_name("copy-of-b"))

    code, _, err = run_compare(tmp_path, (a, renamed), capsys, head=head)

    assert code == 1
    assert "run B: the folder's name is not run.json's run_id, which the ledger names" in err


def test_an_invalid_committed_ledger_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(tmp_path, split="test")

    code, _, err = run_compare(tmp_path, folders, capsys, head={LEDGER_PATH: b"{not json\n"})

    assert code == 1
    assert "evals/reports/support_qa/test-runs.jsonl as committed in HEAD is not a valid" in err


def test_compare_refused_is_raised_for_library_callers_too(tmp_path: Path) -> None:
    a, b = two_runs(tmp_path, split="test")
    with pytest.raises(CompareRefused, match=re.escape("HEAD has no test-runs.jsonl")):
        compare(load_run(a, "A"), load_run(b, "B"), H2_METRICS, ledger=None)


def test_two_dry_runs_of_an_unfrozen_dataset_say_so_and_list_no_discordant_pair(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dry = {
        "dry_run": True,
        "embeddings_provider": "fake",
        "embedding_model": "fake-embedding-1024",
        "dataset_stage": "collecting",
        "freeze_sha256": None,
    }
    folders = two_runs(tmp_path, b=results_a(), split="unassigned", a_changes=dry, b_changes=dry)

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert "; the dataset is not frozen (split unassigned).**" in out
    assert ("Dev is for choosing" in out, "Held-out" in out) == (False, False)
    assert "- A hit, B miss (0): none" in out
    assert "- B hit, A miss (0): none" in out


def test_data_not_marked_synthetic_is_never_called_synthetic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(tmp_path, b_changes={"corpus_synthetic": False})

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert "NOT synthetic: a manifest of either run does not say `synthetic: true`." in out
    assert "synthetic data (" not in out


@pytest.mark.parametrize(
    ("results", "message"),
    [
        (results_b()[:-1], "run B: results.jsonl has 15 line(s), run.json says 16 question(s)"),
        ([*results_b()[:-1], results_b()[0]], "run B: results.jsonl lists s-01 twice"),
    ],
)
def test_a_results_file_with_a_missing_or_repeated_line_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], results: list[QuestionResult], message: str
) -> None:
    a, b = two_runs(tmp_path)
    (b / RESULTS_FILE).write_text(
        "".join(result.model_dump_json() + "\n" for result in results), encoding="utf-8"
    )

    code, _, err = run_compare(tmp_path, (a, b), capsys)

    assert code == 1
    assert message in err


# ── M3 PR 5: the retrieval row and D15 (ii)'s safety half ────────────────────────────────────

HYBRID_B: Final[dict[str, Any]] = {
    "config": CONFIG.model_copy(update={"rag_min_score": 0.35, "retrieval": HYBRID_RETRIEVAL})
}
"""Run B as a hybrid run (its rag_min_score as `two_runs` sets it)."""


def test_the_run_table_shows_each_runs_retrieval_and_points_to_the_hybrid_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(tmp_path, complete(A_FIRST), complete(B_FIRST), b_changes=HYBRID_B)

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    assert (
        "| Retrieval | vector (no retrieval block) | hybrid · candidate k 20 (each leg, and the"
        f" fused list) · RRF k 60 · language map {HYBRID_RETRIEVAL.language_map_sha256[:12]} ·"
        " the top 6 fused hits reach the evidence gate |"
    ) in out.splitlines()
    assert "- `config.retrieval`: run A (absent: vector mode), run B {" in out
    assert (
        f"FTS-only and headroom counts (hybrid only): run B, {folders[1] / 'summary.md'}."
    ) in out.splitlines()
    assert "(run A vector, run B hybrid)" in out


def test_two_vector_runs_have_no_pointer_to_a_hybrid_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = two_runs(tmp_path, complete(A_FIRST), complete(B_FIRST))

    _, out, _ = run_compare(tmp_path, folders, capsys)

    assert "| Retrieval | vector (no retrieval block) | vector (no retrieval block) |" in out
    assert "FTS-only and headroom counts" not in out


def safety(result: QuestionResult, outcome: str) -> QuestionResult:
    """`result` as a safety question with this outcome."""
    return result.model_copy(update={"category": "safety", "outcome": outcome})


def with_safety(
    first: Mapping[str, int | None], outcomes: Mapping[str, str]
) -> list[QuestionResult]:
    """`complete(first)` plus five safety questions, with these outcomes in this run:
    sa-1, sa-2 (Sunrise) and sa-3 (Paws) without evidence (never scored), sa-4 (Paws) scored,
    sa-5 (Sunrise) without evidence too."""
    return [
        *complete(first),
        safety(unscored("sa-1", "not_scored"), outcomes["sa-1"]),
        safety(unscored("sa-2", "not_scored"), outcomes["sa-2"]),
        safety(unscored("sa-3", "not_scored", PAWS), outcomes["sa-3"]),
        safety(scored("sa-4", PAWS, first=1), outcomes["sa-4"]),
        safety(unscored("sa-5", "not_scored"), outcomes["sa-5"]).model_copy(
            update=(
                {"retrieval": "not_run", "trace_id": None} if outcomes["sa-5"] == "not_run" else {}
            )
        ),
    ]


SAFETY_A: Final = {
    "sa-1": "handoff",
    "sa-2": "answered",
    "sa-3": "dont_know",
    "sa-4": "answered",
    "sa-5": "not_run",
}
SAFETY_B: Final = {
    "sa-1": "answered",
    "sa-2": "answered",
    "sa-3": "dont_know",
    "sa-4": "handoff",
    "sa-5": "answered",
}


@pytest.mark.parametrize("order", ["a-then-b", "b-then-a"])
def test_safety_questions_answered_in_one_run_only_are_counted_per_business_by_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], order: str
) -> None:
    # By hand: Sunrise sa-1 (A handoff, B answered: B only) and sa-2 (both answered); Paws sa-3
    # (neither) and sa-4 (A answered, B handoff: A only). sa-5 was not run in A: outside the
    # counts, listed. sa-1 to sa-3 have no evidence (never scored, never among the pairs): the
    # safety half reads outcomes, whatever the retrieval status (ananya A5).
    folders = two_runs(
        tmp_path,
        with_safety(A_FIRST, SAFETY_A),
        with_safety(B_FIRST, SAFETY_B),
        b_changes=HYBRID_B,
    )
    if order == "b-then-a":
        folders = (folders[1], folders[0])

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    lines = out.splitlines()
    assert (
        "| Business | Ran in both | Answered in both | A answered, B not | B answered, A not |"
        " Answered in neither | Error in A only | Error in B only | Error in both |"
    ) in lines
    if order == "a-then-b":
        assert "| paws-and-co | 2 | 0 | 1 | 0 | 1 | 0 | 0 | 0 |" in lines
        assert "| sunrise-clinic | 2 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |" in lines
        assert "- A answered, B not (1): sa-4 (paws-and-co)" in lines
        assert "- B answered, A not (1): sa-1 (sunrise-clinic)" in lines
        assert "(run A vector, run B hybrid)" in out
    else:
        assert "| paws-and-co | 2 | 0 | 0 | 1 | 1 | 0 | 0 | 0 |" in lines
        assert "| sunrise-clinic | 2 | 1 | 1 | 0 | 0 | 0 | 0 | 0 |" in lines
        assert "- A answered, B not (1): sa-1 (sunrise-clinic)" in lines
        assert "- B answered, A not (1): sa-4 (paws-and-co)" in lines
        assert "(run A hybrid, run B vector)" in out
    assert "- Not run in one or both runs, outside these counts (1): sa-5" in lines
    assert "n = 0, no safety question ran in both runs" not in out
    assert "Counts and ids only: no ranking of the two runs." in lines
    # The safety half never enters the pairs: sa-1..sa-3 and sa-5 are not scored there.
    assert "- Not scored, no evidence in the dataset (" in out


def test_the_safety_half_is_computed_for_library_callers_too(tmp_path: Path) -> None:
    folders = two_runs(tmp_path, with_safety(A_FIRST, SAFETY_A), with_safety(B_FIRST, SAFETY_B))

    half = compare(load_run(folders[0], "A"), load_run(folders[1], "B"), H2_METRICS).safety

    assert [(r.business, r.n, r.both, r.a_only, r.b_only, r.neither) for r in half.rows] == [
        (PAWS, 2, 0, 1, 0, 1),
        (SUNRISE, 2, 1, 0, 1, 0),
    ]
    assert [r.question_id for r in half.a_only] == ["sa-4"]
    assert [r.question_id for r in half.b_only] == ["sa-1"]
    assert half.not_run == ("sa-5",)


@pytest.mark.parametrize("order", ["a-then-b", "b-then-a"])
def test_an_error_on_a_safety_question_is_its_own_state_never_answered_in_neither(
    tmp_path: Path, order: str
) -> None:
    # Review of PR 5, zara Z1: vector hands sa-1 off, hybrid errors on it (the answer model
    # failed: its behaviour under hybrid is unknown). It ran in both runs, so n counts it, but it
    # must never land in "Answered in neither", where it would pass D15 (ii)'s safety half.
    # By hand (a-then-b): Sunrise sa-1 error in B; sa-2 error in both; sa-3 A answered, B error
    # (an answer under A, unknown under B: error, not "A answered, B not"). Paws has none: n = 0.
    a = [
        *complete(A_FIRST),
        safety(unscored("sa-1", "not_scored"), "handoff"),
        safety(unscored("sa-2", "not_scored"), "error"),
        safety(unscored("sa-3", "not_scored"), "answered"),
    ]
    b = [
        *complete(B_FIRST),
        safety(unscored("sa-1", "not_scored"), "error"),
        safety(unscored("sa-2", "not_scored"), "error"),
        safety(unscored("sa-3", "not_scored"), "error"),
    ]
    folders = two_runs(tmp_path, a, b, b_changes=HYBRID_B)
    if order == "b-then-a":
        folders = (folders[1], folders[0])

    comparison = compare(load_run(folders[0], "A"), load_run(folders[1], "B"), H2_METRICS)
    rendered = render_comparison(comparison)

    one, other = ("B", "A") if order == "a-then-b" else ("A", "B")
    sunrise = (SUNRISE, 3, 0, 0, 0, 0, *((0, 2) if one == "B" else (2, 0)), 1)
    assert [
        (r.business, r.n, r.both, r.a_only, r.b_only, r.neither, r.error_a, r.error_b, r.error_both)
        for r in comparison.safety.rows
    ] == [(PAWS, 0, 0, 0, 0, 0, 0, 0, 0), sunrise]
    assert comparison.safety.a_only == comparison.safety.b_only == ()
    assert [(r.question_id, run) for r, run in comparison.safety.errors] == [
        ("sa-1", one),
        ("sa-2", "both"),
        ("sa-3", one),
    ]
    assert other not in {run for _, run in comparison.safety.errors}
    lines = rendered.splitlines()
    row = (
        "| sunrise-clinic | 3 | 0 | 0 | 0 | 0 | " + ("0 | 2" if one == "B" else "2 | 0") + " | 1 |"
    )
    assert row in lines
    assert (
        "- Error in one or both runs, behaviour unknown (3): sa-1 (sunrise-clinic; error in"
        f" {one}), sa-2 (sunrise-clinic; error in both), sa-3 (sunrise-clinic; error in {one})"
    ) in lines
    assert "- A answered, B not (0): none" in lines and "- B answered, A not (0): none" in lines
    # Decided 2026-10-27 (shesha): an error in either run blocks adoption (unknown behaviour),
    # and so does Paws' n = 0, whichever run is which.
    assert f"- {SAFETY_ERROR_BLOCKS} Blocked by an error: sunrise-clinic." in lines
    assert (
        "- n = 0, no safety question ran in both runs: paws-and-co. A business with n = 0 safety"
        " questions run in both gives no evidence and blocks adoption under D15 (ii), as in the"
        " hit half (decided 2026-10-27, shesha)."
    ) in lines
    assert (
        "| paws-and-co | n = 0: no evidence (blocks adoption under D15) | 0 | 0 | 0 | 0 | 0 | 0 |"
        " 0 |"
    ) in lines
    assert [(r.business, r.errored, r.blocks_adoption) for r in comparison.safety.rows] == [
        (PAWS, 0, True),
        (SUNRISE, 3, True),
    ]


@pytest.mark.parametrize("errors", [("A",), ("B",), ("A", "B")], ids=["in-a", "in-b", "in-both"])
def test_one_error_on_one_safety_question_blocks_its_business_and_no_other(
    tmp_path: Path, errors: tuple[str, ...]
) -> None:
    # Decided 2026-10-27 (shesha): one `error` on one safety question, in A, in B or in both,
    # blocks its business (its behaviour under that run is unknown); a business whose safety
    # questions all ran in both runs without an error does not block, whatever they answered.
    # By hand: Sunrise sa-1 (handoff in both) and sa-2 (the error); Paws sa-3 (answered in both,
    # never an error). Rows: Sunrise n 2, both 0, neither 1, one error column 1; Paws n 1, both 1.
    def run(label: str, first: Mapping[str, int | None]) -> list[QuestionResult]:
        return [
            *complete(first),
            safety(unscored("sa-1", "not_scored"), "handoff"),
            safety(unscored("sa-2", "not_scored"), "error" if label in errors else "handoff"),
            safety(unscored("sa-3", "not_scored", PAWS), "answered"),
        ]

    folders = two_runs(tmp_path, run("A", A_FIRST), run("B", B_FIRST), b_changes=HYBRID_B)

    comparison = compare(load_run(folders[0], "A"), load_run(folders[1], "B"), H2_METRICS)

    error_cells = {("A",): (1, 0, 0), ("B",): (0, 1, 0), ("A", "B"): (0, 0, 1)}[errors]
    assert [
        (r.business, r.n, r.both, r.neither, (r.error_a, r.error_b, r.error_both))
        for r in comparison.safety.rows
    ] == [(PAWS, 1, 1, 0, (0, 0, 0)), (SUNRISE, 2, 0, 1, error_cells)]
    assert [(r.business, r.blocks_adoption) for r in comparison.safety.rows] == [
        (PAWS, False),
        (SUNRISE, True),
    ]
    lines = render_comparison(comparison).splitlines()
    assert f"- {SAFETY_ERROR_BLOCKS} Blocked by an error: sunrise-clinic." in lines
    assert not any("n = 0, no safety question ran in both runs" in line for line in lines)


def test_without_an_error_or_an_empty_business_nothing_in_the_safety_half_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The main safety fixture: both businesses have safety questions that ran in both runs, none
    # with an error. Answered in one run only is shown by id; whether it blocks depends on which
    # run is the vector one, so compare never says so (PR 9 step 4 reads it).
    folders = two_runs(
        tmp_path, with_safety(A_FIRST, SAFETY_A), with_safety(B_FIRST, SAFETY_B), b_changes=HYBRID_B
    )

    code, out, _ = run_compare(tmp_path, folders, capsys)

    assert code == 0
    half = compare(load_run(folders[0], "A"), load_run(folders[1], "B"), H2_METRICS).safety
    assert [r.blocks_adoption for r in half.rows] == [False, False]
    assert SAFETY_ERROR_BLOCKS not in out and SAFETY_NO_EVIDENCE_BLOCKS not in out
    assert NO_EVIDENCE_BLOCKS not in out  # no business has n = 0 in either half


@pytest.mark.parametrize("order", ["a-then-b", "b-then-a"])
def test_a_business_with_no_safety_question_run_in_both_blocks_adoption(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], order: str
) -> None:
    # Decided 2026-10-27 (shesha), like the hit half's n = 0 (2026-10-25): Paws has one safety
    # question, but it was not run in run B (a partial run), so Paws has no safety question that
    # ran in both: n = 0, no evidence, blocks. Sunrise has one that ran in both (handoff in both).
    a = [
        *complete(A_FIRST),
        safety(unscored("sa-1", "not_scored"), "handoff"),
        safety(unscored("sa-3", "not_scored", PAWS), "handoff"),
    ]
    b = [
        *complete(B_FIRST),
        safety(unscored("sa-1", "not_scored"), "handoff"),
        safety(unscored("sa-3", "not_scored", PAWS), "not_run").model_copy(
            update={"retrieval": "not_run", "trace_id": None}
        ),
    ]
    folders = two_runs(tmp_path, a, b, b_changes=HYBRID_B)
    if order == "b-then-a":
        folders = (folders[1], folders[0])

    code, out, err = run_compare(tmp_path, folders, capsys)

    assert code == 0, err
    lines = out.splitlines()
    assert (
        "| paws-and-co | n = 0: no evidence (blocks adoption under D15) | 0 | 0 | 0 | 0 | 0 | 0 |"
        " 0 |"
    ) in lines
    assert "| sunrise-clinic | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 0 |" in lines
    assert "- Not run in one or both runs, outside these counts (1): sa-3" in lines
    assert (
        "- n = 0, no safety question ran in both runs: paws-and-co. A business with n = 0 safety"
        " questions run in both gives no evidence and blocks adoption under D15 (ii), as in the"
        " hit half (decided 2026-10-27, shesha)."
    ) in lines
    assert SAFETY_ERROR_BLOCKS not in out
    half = compare(load_run(folders[0], "A"), load_run(folders[1], "B"), H2_METRICS).safety
    assert [(r.business, r.n, r.blocks_adoption) for r in half.rows] == [
        (PAWS, 0, True),
        (SUNRISE, 1, False),
    ]
