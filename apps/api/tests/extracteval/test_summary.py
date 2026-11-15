"""`summary.md` (`extracteval/summary.py`): the team code around H2, with stand-in metrics.

`RecordingMetrics` returns canned results (never H2) and records the fields each call was given,
so these tests check what the harness hands to H2: per business, per level, accepted precision,
review recall's subset, and one call per document for the bootstrap. The "also by id" lists and
the cost lines are team code over results.jsonl and are checked directly. Every line is made up.
"""

from dataclasses import replace
from fractions import Fraction
from typing import Any, Final

from tests.extracteval.fakes import (
    NO_SILENT,
    RecordingMetrics,
    canned_metrics,
    consistent_metrics,
)
from tests.extracteval.runjson import extract_run_info
from vectapilot_api.evalrun.intervals import bootstrap_interval, wilson_interval
from vectapilot_api.extracteval.metrics import (
    DocumentCount,
    ExtractMetrics,
    FieldCount,
    ScoredField,
    SilentErrors,
)
from vectapilot_api.extracteval.runinfo import DRY_LABEL, DocumentLine, FieldLine
from vectapilot_api.extracteval.summary import (
    INCONSISTENT,
    LEVELS_LABEL,
    NO_LEVEL_ROW,
    Context,
    field_interval,
    identity_holds,
    render,
    routed_fields,
    scored_fields,
)

S, P = "sunrise-clinic", "paws-and-co"


def line(name: str, status: str | None, **changes: Any) -> FieldLine:
    base: dict[str, Any] = {
        "field": name,
        "type": "text",
        "status": status,
        "level": "high" if status == "accepted" else None,
        "reasons": (),
        "page": 1 if status == "accepted" else None,
        "quote_hits": 1 if status == "accepted" else 0,
        "has_value": status == "accepted",
        "gold_null": False,
        "correct": None if status is None else True,
    }
    merged = base | changes
    merged.setdefault("routed_level", merged["level"])  # as routed: nothing dropped
    merged.setdefault("proposal_correct", merged["correct"])
    return FieldLine(**merged)


def document(
    doc_id: str, business: str, run: str, *fields: FieldLine, **changes: Any
) -> DocumentLine:
    base: dict[str, Any] = {
        "id": doc_id,
        "business_id": business,
        "kind": "referral_letter" if business == S else "vaccination_record",
        "split": "dev",
        "tags": (),
        "run": run,
        "status": "complete" if run == "scored" else None,
        "fields": fields,
    }
    return DocumentLine(**(base | changes))


DOCUMENTS: Final = (
    document(
        "a",
        S,
        "scored",
        line("name", "accepted"),
        line("born", "accepted", type="date"),
        latency_ms=120.0,
        input_tokens=900,
        output_tokens=200,
        cost_usd="0.0020",
        llm_calls=1,
    ),
    document(
        "b",
        S,
        "scored",
        line("name", "review", reasons=("truncated",), correct=False),
        line("born", "review", reasons=("truncated",), gold_null=True),
        status="needs_review",
        failure="truncated",
        error="OutputTruncated",
        latency_ms=300.0,
        cost_usd="0.0040",
        llm_calls=1,
    ),
    document(
        "c",
        P,
        "scored",
        line("pet", "accepted", correct=False),
        line(
            "owner",
            "review",
            level="medium",
            reasons=("routing_invariant", "unsure"),
            has_value=True,
            page=1,
            corrected=True,
        ),
        status="needs_review",
        tags=("injection",),
        latency_ms=200.0,
        cost_usd="0.0030",
        llm_calls=1,
    ),
    document("d", P, "not_run", line("pet", None), line("owner", None)),
    document("e", P, "harness_error", line("pet", None), line("owner", None), error="ValueError"),
)


def context(metrics: Any = None) -> Context:
    return Context(businesses=(S, P), metrics=metrics or canned_metrics(), resamples=200)


def test_scored_fields_flattens_results_into_h2s_input() -> None:
    fields = scored_fields(DOCUMENTS)
    assert [f.key for f in fields] == [
        "a/name",
        "a/born",
        "b/name",
        "b/born",
        "c/pet",
        "c/owner",
        "d/pet",
        "d/owner",
        "e/pet",
        "e/owner",
    ]
    owner = fields[5]
    assert owner == ScoredField(
        document="c",
        business_id=P,
        field="owner",
        scored=True,
        document_status="needs_review",
        status="review",
        level="medium",
        reasons=("routing_invariant", "unsure"),
        has_value=True,
        gold_null=False,
        correct=True,
    )
    assert not fields[6].scored and fields[6].correct is None


def test_the_harness_hands_h2_the_subsets_the_scoring_rules_name() -> None:
    recording = RecordingMetrics()
    render(extract_run_info("r", dry=True), DOCUMENTS, context(recording.bundle()))
    accuracy = recording.given("field_accuracy")
    everything = tuple(sorted(f.key for f in scored_fields(DOCUMENTS)))
    sunrise = ("a/born", "a/name", "b/born", "b/name")
    paws = ("c/owner", "c/pet", "d/owner", "d/pet", "e/owner", "e/pet")

    assert everything in accuracy  # overall, not-scored fields included (for their ids)
    assert sunrise in accuracy and paws in accuracy  # per business
    assert ("a/born", "a/name", "c/pet") in accuracy  # level high, and accepted precision
    assert accuracy.count(("a/born", "a/name", "c/pet")) == 2
    assert ("c/owner",) in accuracy  # level medium
    assert () in accuracy  # level low: none
    assert ("b/born", "b/name") in accuracy  # no level (no value)
    for doc_fields in (("a/born", "a/name"), ("b/born", "b/name"), ("c/owner", "c/pet")):
        assert doc_fields in accuracy  # one call per document for the bootstrap
    # Per field (paws-and-co / pet): d and e are not scored, kept so their ids are listed.
    assert ("c/pet", "d/pet", "e/pet") in accuracy
    assert ("b/name", "c/pet") in recording.given("review_rate")  # review recall's subset
    # The identity check first (all fields), then the section (all, per business), then review.
    assert recording.given("silent_errors") == [everything, everything, sunrise, paws, everything]
    # All, per business, then per tag in TAGS' order (only c carries one: injection).
    assert recording.given("straight_through") == [
        everything,
        sunrise,
        paws,
        (),
        (),
        ("c/owner", "c/pet"),
        (),
        (),
    ]
    assert ("c/owner", "c/pet") in recording.given("review_rate")  # tag injection
    assert recording.given("review_precision") == [everything]


def test_the_summary_lists_by_id_and_labels_a_dry_run() -> None:
    text = render(extract_run_info("r", dry=True), DOCUMENTS, context())
    assert text.splitlines()[2].startswith(f"**{DRY_LABEL}**")
    assert "3 documents scored, 2 not scored" in text
    assert "- Not run: d" in text
    assert "- Harness errors (not scored): e (ValueError)" in text
    assert "- Failures (E3, scored as all-review): truncated: b" in text
    assert "- Truncations: b" in text
    assert "- `routing_invariant` fields (the invariant check corrected routing): c/owner" in text
    assert "- `injection` documents with an accepted value not in gold: 1 of 1 (c)" in text
    assert "n 3, median 200.0 ms, max 300.0 ms, no p95 (n < 20)" in text
    assert "Cost per document: n 3, median $0.003, max $0.004" in text
    assert "dry run: the plumbing's own time, never a result" in text


def test_a_live_summary_has_no_dry_label_and_names_the_split() -> None:
    text = render(extract_run_info("r", split="test"), DOCUMENTS, context())
    assert DRY_LABEL not in text
    assert "- Test: the first ledgered run of this config" in text
    assert "local: shesha's machine and network" in text


def test_the_bootstrap_resamples_documents_and_sums_their_counts() -> None:
    counts = {"a": (2, 2), "b": (0, 2), "c": (1, 2)}
    fields = scored_fields(DOCUMENTS)

    def count(group: Any) -> tuple[int, int]:
        return counts[group[0].document]

    found = field_interval(fields, count, context())
    expected = bootstrap_interval(
        [counts["a"], counts["b"], counts["c"]],
        lambda sample: Fraction(sum(k for k, _ in sample), sum(n for _, n in sample)),
        seed=context().seed,
        resamples=200,
    )
    assert found == expected
    assert found is not None and found.n == 3  # documents, not fields
    unscored = [f for f in fields if not f.scored]
    assert field_interval(unscored, count, context()) is None


INVENTED: Final = document(
    "f",
    S,
    "scored",
    # H1 (here the stand-in, then the invariant check) routed an invented patient name at low;
    # "no quote, no value" dropped it, and the gold is null: stored, it reads correct.
    line(
        "name",
        "review",
        reasons=("routing_invariant",),
        gold_null=True,
        correct=True,
        routed_level="low",
        proposal_correct=False,
        corrected=True,
        unverified=True,
    ),
    status="needs_review",
)


def test_correctness_by_level_scores_a_dropped_guess_as_wrong_at_its_routed_level() -> None:
    stored = {f.key: f for f in scored_fields((INVENTED,))}["f/name"]
    routed = {f.key: f for f in routed_fields((INVENTED,))}["f/name"]
    assert (stored.level, stored.correct, stored.has_value) == (None, True, False)
    assert (routed.level, routed.correct, routed.has_value) == ("low", False, True)

    recording = RecordingMetrics()
    text = render(
        extract_run_info("r", dry=True), (*DOCUMENTS, INVENTED), context(recording.bundle())
    )
    calls = recording.fields_given("field_accuracy")
    low = [c for c in calls if c and all(f.level == "low" for f in c)]
    assert low == [(routed,)]  # "low: wrong", once
    no_level = [tuple(sorted(f.key for f in c)) for c in calls if c and c[0].level is None]
    assert ("b/born", "b/name") in no_level  # no value proposed: the truncated document
    assert all("f/name" not in keys or len(keys) == 1 for keys in no_level)
    # f/name is never in the "no level" row (its only level-less call is its document's
    # bootstrap call, which scores the stored value).
    level_rows = [c for c in calls if c and {f.key for f in c} == {"b/born", "b/name", "f/name"}]
    assert level_rows == []
    assert LEVELS_LABEL in text
    assert f"| {NO_LEVEL_ROW} |" in text


def test_an_inconsistent_h2_opens_the_summary_with_a_warning() -> None:
    # canned_metrics: silent "canned/silent", but the misses are b/name and c/pet.
    assert not identity_holds(DOCUMENTS, canned_metrics())
    dry = render(extract_run_info("r", dry=True), DOCUMENTS, context()).splitlines()
    assert dry[2].startswith(f"**{DRY_LABEL}**")  # the DRY label stays first
    assert dry[4] == INCONSISTENT
    live = render(extract_run_info("r"), DOCUMENTS, context()).splitlines()
    assert live[2] == INCONSISTENT

    assert identity_holds(DOCUMENTS, consistent_metrics())
    text = render(extract_run_info("r"), DOCUMENTS, context(consistent_metrics()))
    assert INCONSISTENT not in text and "Inconsistent" not in text


def _silent(*keys: str) -> SilentErrors:
    owners = tuple(sorted({key.split("/")[0] for key in keys}))
    return SilentErrors(
        FieldCount(len(keys), 6, tuple(sorted(keys))),
        DocumentCount(len(owners), 3, owners, wilson_interval(len(owners), 3)),
    )


def test_the_identity_compares_silent_errors_with_review_recalls_misses() -> None:
    # canned review_rate counts only "canned/review", so both wrong fields (b/name, c/pet) are
    # misses: a stand-in reporting exactly those as silent passes; one fewer or one more fails.
    def reporting(silent: SilentErrors) -> ExtractMetrics:
        return replace(canned_metrics(), silent_errors=lambda fields: silent)

    assert identity_holds(DOCUMENTS, reporting(_silent("b/name", "c/pet")))
    assert not identity_holds(DOCUMENTS, reporting(_silent("b/name")))
    assert not identity_holds(DOCUMENTS, reporting(_silent("a/name", "b/name", "c/pet")))
    assert not identity_holds(DOCUMENTS, reporting(NO_SILENT))


def test_review_rate_and_straight_through_by_tag_and_the_stress_note() -> None:
    tagged = tuple(d.model_copy(update={"tags": d.tags or ("absent",)}) for d in DOCUMENTS)
    text = render(extract_run_info("r"), tagged, context(consistent_metrics()))
    for tag in ("absent", "confusable", "injection", "image_page", "table"):
        assert f"  - tag {tag}: " in text  # review rate
        assert f"- tag {tag}: " in text  # straight-through
    assert text.count("Every scored document carries a stress tag") == 2
    # One untagged scored document: no claim that every document is a stress document.
    untagged = render(extract_run_info("r"), DOCUMENTS, context(consistent_metrics()))
    assert "Every scored document carries a stress tag" not in untagged
