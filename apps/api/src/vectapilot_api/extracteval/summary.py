"""`summary.md` of an extraction run folder (M6 PR 4): derived from run.json and results.jsonl
through H2 (`metrics.py`), rewritten each time `summarize` runs.

**Team code around H2.** `scored_fields` turns results.jsonl into H2's input; every subset is
chosen here and handed to H2's five functions (the plan's "correctness by level, accepted
precision and review recall come from the harness calling them on subsets"):
- per business: all five on that business's fields;
- per level (`high`, `medium`, `low`, and "no value proposed"): `field_accuracy` on the scored
  fields **as routed** (`routed_fields`: each field's level after routing and the invariant
  check, and whether its proposal was the gold, before "no quote, no value" dropped an
  unverified value), so a guess H1 marked `low` is counted as a wrong `low`, never as a correct
  "no value" (M6 PR 4 review, ananya-rag). Ordinal levels checked, not calibrated probabilities;
- accepted precision: `field_accuracy` on the accepted fields;
- per field: `field_accuracy` on one kind's one field;
- per tag (`absent`, `confusable`, ...): `review_rate` and `straight_through` on the fields of
  the documents carrying it. v0.1's slots each require a stress tag, so the summary says so when
  every scored document has one: those two rates then describe stress documents only;
- review recall: `review_rate` on the scored fields that are not correct (wrong or missing); its
  misses are exactly the silent errors (a field that is not correct and not in review is accepted
  or absent), so the two must agree. When they do not, the summary opens with "Inconsistent" and
  `run` and `summarize` exit 1 (`identity_holds`; zara-safety's PR 4 review): a wrong H2 cannot
  produce a clean headline.
**Intervals:** documents get H2's Wilson; field accuracy and review rate get `evalrun`'s seeded
percentile bootstrap over documents (H2 is called once per document, the resample sums its
counts; it undercovers at small n); every other field-level rate says "no interval".

What it says, in this order: the run's identity (the DRY label first when dry), silent errors,
field accuracy (overall, per business, per level, accepted precision, per field), review (rate,
recall, precision), straight-through documents, the "also by id" lists (not scored, failures by
reason, truncations, `routing_invariant` and `internal_error` fields, withheld and unverified
values, signal errors, `injection` documents with an accepted value not in gold), latency, tokens
and cost per document, and spend. **Ids, codes and counts only:** results.jsonl holds no value,
quote or document text, and `values.jsonl` is never read here.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from statistics import median
from typing import Final

from vectapilot_api.evalrun.budget import usd
from vectapilot_api.evalrun.intervals import BootstrapInterval, bootstrap_interval
from vectapilot_api.evalrun.report import DEFAULT_BOOTSTRAP_SEED, DEFAULT_RESAMPLES
from vectapilot_api.extracteval.gold import TAGS
from vectapilot_api.extracteval.metrics import (
    ACCEPTED,
    LEVELS,
    Accuracy,
    DocumentCount,
    ExtractMetrics,
    FieldCount,
    ScoredField,
)
from vectapilot_api.extracteval.runinfo import DRY_LABEL, DocumentLine, ExtractRunInfo
from vectapilot_api.measure.latency import latency_summary

LATENCY_MIN_N: Final = 20
"""p95 only from 20 samples (M1 H3's rule, ADR 0007)."""
LOCAL_LABEL: Final = "local: shesha's machine and network; never compared with a target"
DRY_LATENCY_LABEL: Final = "dry run: the plumbing's own time, never a result"
NO_INTERVAL: Final = "no interval"
TRUNCATED: Final = "truncated"
INTERNAL_ERROR: Final = "internal_error"
ROUTING_INVARIANT: Final = "routing_invariant"
INJECTION: Final = "injection"


@dataclass(frozen=True)
class Context:
    businesses: Sequence[str]
    """The dataset manifest's order."""
    metrics: ExtractMetrics
    seed: int = DEFAULT_BOOTSTRAP_SEED
    resamples: int = DEFAULT_RESAMPLES


def scored_fields(documents: Sequence[DocumentLine]) -> tuple[ScoredField, ...]:
    """H2's input: one `ScoredField` per (document, field), in file order."""
    return tuple(
        ScoredField(
            document=document.id,
            business_id=document.business_id,
            field=line.field,
            scored=document.run == "scored",
            document_status=document.status,
            status=line.status,
            level=line.level,
            reasons=line.reasons,
            has_value=line.has_value,
            gold_null=line.gold_null,
            correct=line.correct,
        )
        for document in documents
        for line in document.fields
    )


def routed_fields(documents: Sequence[DocumentLine]) -> tuple[ScoredField, ...]:
    """The fields as routed, for correctness per level (module docstring): `level` is the
    level routing gave the proposal (`FieldLine.routed_level`), `correct` whether that proposal
    was the gold (`proposal_correct`) and `has_value` whether one was routed with a level."""
    return tuple(
        ScoredField(
            document=document.id,
            business_id=document.business_id,
            field=line.field,
            scored=document.run == "scored",
            document_status=document.status,
            status=line.status,
            level=line.routed_level,
            reasons=line.reasons,
            has_value=line.routed_level is not None,
            gold_null=line.gold_null,
            correct=line.proposal_correct,
        )
        for document in documents
        for line in document.fields
    )


def _pct(value: float) -> str:
    return f"{value * 100:.1f} %"


def _ids(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "none"


def _wilson(count: DocumentCount) -> str:
    if count.interval is None:
        return "not measured (n = 0)"
    interval = count.interval
    return f"{count.x} of {count.n} (95 % Wilson {_pct(interval.low)} to {_pct(interval.high)})"


def _bootstrap_text(interval: BootstrapInterval | None) -> str:
    if interval is None:
        return NO_INTERVAL
    return (
        f"bootstrap over {interval.n} documents {_pct(float(interval.low))} to"
        f" {_pct(float(interval.high))}"
    )


def _fraction(k: int, n: int) -> str:
    return f"{k} of {n}" if n else "not measured (n = 0)"


def _by_document(fields: Sequence[ScoredField]) -> list[list[ScoredField]]:
    groups: dict[str, list[ScoredField]] = {}
    for item in fields:
        if item.scored:
            groups.setdefault(item.document, []).append(item)
    return list(groups.values())


def field_interval(
    fields: Sequence[ScoredField],
    count: Callable[[Sequence[ScoredField]], tuple[int, int]],
    context: Context,
) -> BootstrapInterval | None:
    """The seeded percentile bootstrap of a field-level rate over documents (module docstring):
    `count` (an H2 call) gives each scored document's `(k, n)`; a resample's rate is its summed k
    over its summed n. None with no scored document."""
    counts = [count(group) for group in _by_document(fields)]
    counts = [(k, n) for k, n in counts if n > 0]
    if not counts:
        return None

    def rate(sample: Sequence[tuple[int, int]]) -> Fraction:
        return Fraction(sum(k for k, _ in sample), sum(n for _, n in sample))

    return bootstrap_interval(counts, rate, seed=context.seed, resamples=context.resamples)


def _accuracy_counts(metrics: ExtractMetrics) -> Callable[[Sequence[ScoredField]], tuple[int, int]]:
    def count(fields: Sequence[ScoredField]) -> tuple[int, int]:
        result = metrics.field_accuracy(fields)
        return result.correct, result.n

    return count


def _review_counts(metrics: ExtractMetrics) -> Callable[[Sequence[ScoredField]], tuple[int, int]]:
    def count(fields: Sequence[ScoredField]) -> tuple[int, int]:
        result = metrics.review_rate(fields)
        return result.k, result.n

    return count


# ── Sections ─────────────────────────────────────────────────────────────────────────────────


def header(info: ExtractRunInfo, documents: Sequence[DocumentLine], context: Context) -> list[str]:
    out = [f"# doc_extraction run {info.run_id}", ""]
    if info.dry_run:
        out += [f"**{DRY_LABEL}** ({info.dry_reason or 'dry'}).", ""]
    split: str = info.split
    if info.split == "dev":
        split += (
            " (tuning: a prompt change is a new EXTRACT_PROMPT_VERSION, a rule change a new"
            " EXTRACTION_RULES_VERSION)"
        )
    fp = info.fingerprint
    limits = fp.limits
    out += [
        f"- Dataset: {info.dataset} {info.dataset_version} ({info.dataset_stage}), freeze"
        f" {(info.freeze_sha256 or 'none')[:12]}, dataset and configuration files at the commit:"
        f" {'yes' if info.dataset_committed else 'NO'} · split: {split}",
        f"- Commit {info.commit[:12]}"
        + (" (with uncommitted changes)" if info.uncommitted_changes else ""),
        f"- Fingerprint {info.fingerprint_sha256[:12]}: prompt {fp.extract_prompt_version}, rules"
        f" {fp.extraction_rules_version}, alias {fp.alias} → {fp.model}, catalog"
        f" {fp.catalog_version}; limits: {limits.max_bytes} bytes, {limits.max_pages} pages,"
        f" {limits.max_characters} characters, attempt {limits.attempt_timeout_s:g} s, deadline"
        f" {limits.deadline_s:g} s, {limits.max_retries} retries, {limits.max_repairs} repair(s)"
        + (f"; fixtures {fp.fixtures_sha256[:12]}" if fp.fixtures_sha256 else ""),
        "- Businesses: "
        + "; ".join(
            f"{b} (business file {p.business_sha256[:12]}, "
            + ", ".join(f"{k} {s[:12]}" for k, s in sorted(p.templates.items()))
            + ")"
            for b, p in fp.businesses.items()
        ),
        f"- Provider: {info.llm_provider}",
    ]
    if info.split == "test":
        rerun = (
            "the first ledgered run of this config"
            if info.test_runs_before == 0
            else f"a rerun ({info.test_runs_before} before; reason {info.test_rerun_reason})"
        )
        out.append(f"- Test: {rerun}")
    scored = sum(d.run == "scored" for d in documents)
    status = info.status + (f" ({info.stop})" if info.stop else "")
    out.append(
        f"- Status: {status} · {scored} documents scored, {len(documents) - scored} not scored"
        + (" · partial: never a result" if info.status != "complete" else "")
    )
    out += [
        "",
        "Every count is over scored documents and their fields; a document not scored (not run, or"
        ' a harness error) is outside every n and listed by id under "Also by id". Documents:'
        " 95 % Wilson. Field accuracy and review rate: seeded bootstrap over documents (seed"
        f" {context.seed}, {context.resamples} resamples; it undercovers at small n); other"
        " field-level rates: no interval. Levels are ordinal (checks in code), never"
        " probabilities.",
    ]
    return out


def _business(fields: Sequence[ScoredField], business_id: str) -> list[ScoredField]:
    return [item for item in fields if item.business_id == business_id]


def silent_section(fields: Sequence[ScoredField], context: Context) -> list[str]:
    metrics = context.metrics
    overall = metrics.silent_errors(fields)
    rows = [("all", overall)] + [
        (f"business {b}", metrics.silent_errors(_business(fields, b))) for b in context.businesses
    ]
    out = [
        "",
        "## Silent errors (target 0; they judge the routing)",
        "An `accepted` field that is not correct, or an `absent` field whose gold has a value.",
        "",
        "| | fields | documents with at least one |",
        "|---|---|---|",
    ]
    out += [
        f"| {name} | {_fraction(r.fields.k, r.fields.n)} | {_wilson(r.documents)} |"
        for name, r in rows
    ]
    out.append(f"\nSilent fields: {_ids(overall.fields.fields)}")
    return out


def _accuracy_row(name: str, result: Accuracy, interval: str = NO_INTERVAL) -> str:
    return f"| {name} | {_fraction(result.correct, result.n)} | {interval} |"


LEVELS_LABEL: Final = (
    'Correctness by level (levels as routed, before "no quote, no value"): each field at the'
    " level routing and the invariant check gave its proposal, scored on that proposal, so a"
    " dropped guess counts as a wrong value at its own level. Ordinal levels from checks in code,"
    " never probabilities: the claim is k of n per level."
)


def accuracy_section(
    fields: Sequence[ScoredField], routed: Sequence[ScoredField], context: Context
) -> list[str]:
    metrics = context.metrics
    count = _accuracy_counts(metrics)
    scored = [item for item in fields if item.scored]
    overall = metrics.field_accuracy(fields)
    out = [
        "",
        "## Field accuracy (every scored field, whatever its status)",
        "",
        "| | correct | interval |",
        "|---|---|---|",
        _accuracy_row("all", overall, _bootstrap_text(field_interval(fields, count, context))),
    ]
    for business_id in context.businesses:
        mine = _business(fields, business_id)
        interval = _bootstrap_text(field_interval(mine, count, context))
        out.append(_accuracy_row(f"business {business_id}", metrics.field_accuracy(mine), interval))
    accepted = [item for item in scored if item.status == ACCEPTED]
    out.append(_accuracy_row("accepted precision", metrics.field_accuracy(accepted)))
    as_routed = [item for item in routed if item.scored]
    out += ["", LEVELS_LABEL, "", "| level (as routed) | proposal correct | interval |"]
    out.append("|---|---|---|")
    for level in LEVELS:
        subset = [item for item in as_routed if item.level == level]
        out.append(_accuracy_row(level, metrics.field_accuracy(subset)))
    no_level = [item for item in as_routed if item.level is None]
    out.append(_accuracy_row("no level (no value proposed)", metrics.field_accuracy(no_level)))
    out += ["", "Per field:", "", "| business / field | correct |", "|---|---|"]
    names = sorted({(item.business_id, item.field) for item in fields})
    for business_id, name in names:
        subset = [i for i in fields if (i.business_id, i.field) == (business_id, name)]
        result = metrics.field_accuracy(subset)
        out.append(f"| {business_id} / {name} | {_fraction(result.correct, result.n)} |")
    out.append(f"\nWrong fields: {_ids(overall.wrong)}")
    return out


def _tagged(
    documents: Sequence[DocumentLine], fields: Sequence[ScoredField], tag: str
) -> list[ScoredField]:
    """The fields of the documents that carry `tag`."""
    ids = {document.id for document in documents if tag in document.tags}
    return [item for item in fields if item.document in ids]


def tag_note(documents: Sequence[DocumentLine]) -> list[str]:
    """Says so when every scored document carries a tag (module docstring)."""
    scored = [d for d in documents if d.run == "scored"]
    if not scored or not all(d.tags for d in scored):
        return []
    return [
        "- Every scored document carries a stress tag (v0.1's slots each require one: "
        + ", ".join(TAGS)
        + "), so these rates describe stress documents only and read lower than everyday"
        " documents would."
    ]


def recall_misses(fields: Sequence[ScoredField], metrics: ExtractMetrics) -> list[str]:
    """Review recall's misses: the scored fields that are not correct and that `review_rate`
    does not count as in review, by key, sorted."""
    wrong = [item for item in fields if item.scored and item.correct is False]
    return sorted({item.key for item in wrong} - set(metrics.review_rate(wrong).fields))


def identity_holds(documents: Sequence[DocumentLine], metrics: ExtractMetrics) -> bool:
    """Whether review recall's misses are exactly the silent errors, as they are by definition
    (module docstring). False means H2 or the results are wrong: no number may be read."""
    fields = scored_fields(documents)
    return list(metrics.silent_errors(fields).fields.fields) == recall_misses(fields, metrics)


INCONSISTENT: Final = (
    "**Inconsistent: review recall's misses are not exactly the silent errors, as they must be by"
    " definition. H2 or this run's results are wrong: read no number in this summary until it is"
    " fixed (`run` and `summarize` exit 1).**"
)


def review_section(
    fields: Sequence[ScoredField], documents: Sequence[DocumentLine], context: Context
) -> list[str]:
    metrics = context.metrics
    count = _review_counts(metrics)
    rate = metrics.review_rate(fields)
    out = [
        "",
        "## Review",
        f"- Review rate (staff work: fields in review of all fields): {_fraction(rate.k, rate.n)}"
        f" ({_bootstrap_text(field_interval(fields, count, context))})",
    ]
    for business_id in context.businesses:
        mine = _business(fields, business_id)
        result = metrics.review_rate(mine)
        interval = _bootstrap_text(field_interval(mine, count, context))
        out.append(f"  - business {business_id}: {_fraction(result.k, result.n)} ({interval})")
    for tag in TAGS:
        result = metrics.review_rate(_tagged(documents, fields, tag))
        out.append(f"  - tag {tag}: {_fraction(result.k, result.n)} ({NO_INTERVAL})")
    out += tag_note(documents)
    wrong = [item for item in fields if item.scored and item.correct is False]
    recall: FieldCount = metrics.review_rate(wrong)
    misses = sorted({item.key for item in wrong} - set(recall.fields))
    silent = metrics.silent_errors(fields).fields.fields
    out.append(
        "- Review recall (wrong-or-missing fields routed to review):"
        f" {_fraction(recall.k, recall.n)} ({NO_INTERVAL}); misses: {_ids(misses)}"
    )
    if list(silent) != misses:
        out.append(
            "  - **Inconsistent:** the misses should be exactly the silent errors (see the top of"
            " this summary)."
        )
    precision = metrics.review_precision(fields)
    by_reason = ", ".join(f"{reason} {count}" for reason, count in precision.by_reason) or "none"
    out += [
        f"- Review precision (reviewed fields with a value that is not correct, of reviewed fields"
        f" with a value): {_fraction(precision.wrong, precision.n)} ({NO_INTERVAL}); wrong:"
        f" {_ids(precision.fields)}",
        f"  - Reviewed without a value: {precision.without_value} (by reason: {by_reason}), of"
        f" which {precision.null_gold} had a null gold (review not needed)",
    ]
    return out


def straight_section(
    fields: Sequence[ScoredField], documents: Sequence[DocumentLine], context: Context
) -> list[str]:
    metrics = context.metrics
    overall = metrics.straight_through(fields)
    out = [
        "",
        "## Straight-through documents (complete, and every field correct)",
        f"- all: {_wilson(overall)}; documents: {_ids(overall.documents)}",
    ]
    out += [
        f"- business {b}: {_wilson(metrics.straight_through(_business(fields, b)))}"
        for b in context.businesses
    ]
    out += [
        f"- tag {tag}: {_wilson(metrics.straight_through(_tagged(documents, fields, tag)))}"
        for tag in TAGS
    ]
    return out + tag_note(documents)


def _keys(documents: Sequence[DocumentLine], keep: Callable[[DocumentLine, str], bool]) -> str:
    return _ids(
        sorted(f"{d.id}/{f.field}" for d in documents for f in d.fields if keep(d, f.field))
    )


def by_id_section(documents: Sequence[DocumentLine], fields: Sequence[ScoredField]) -> list[str]:
    not_run = sorted(d.id for d in documents if d.run == "not_run")
    errors = sorted(
        f"{d.id} ({d.error or 'unknown'})" for d in documents if d.run == "harness_error"
    )
    failures: dict[str, list[str]] = {}
    for document in documents:
        if document.run == "scored" and document.failure is not None:
            failures.setdefault(document.failure, []).append(document.id)
    failure_text = "; ".join(f"{r}: {_ids(sorted(ids))}" for r, ids in sorted(failures.items()))
    flags = {(d.id, f.field): f for d in documents for f in d.fields}
    injection = sorted(d.id for d in documents if d.run == "scored" and INJECTION in d.tags)
    planted = sorted(
        {
            item.document
            for item in fields
            if item.document in injection and item.status == ACCEPTED and item.correct is False
        }
    )
    signal = sorted(f"{d.id} ({d.signal_errors})" for d in documents if d.signal_errors)
    return [
        "",
        "## Also by id",
        f"- Not run: {_ids(not_run)}",
        f"- Harness errors (not scored): {_ids(errors)}",
        f"- Failures (E3, scored as all-review): {failure_text or 'none'}",
        f"- Truncations: {_ids(failures.get(TRUNCATED, []))}",
        "- `routing_invariant` fields (the invariant check corrected routing): "
        + _keys(documents, lambda d, f: flags[(d.id, f)].corrected),
        "- `internal_error` fields: "
        + _keys(documents, lambda d, f: INTERNAL_ERROR in flags[(d.id, f)].reasons),
        "- Withheld values (longer than the field allows): "
        + _keys(documents, lambda d, f: flags[(d.id, f)].withheld),
        "- Unverified values (dropped: no quote, no value): "
        + _keys(documents, lambda d, f: flags[(d.id, f)].unverified),
        f"- Signal errors (a check failed unexpectedly): {_ids(signal)}",
        f"- `injection` documents with an accepted value not in gold: {len(planted)} of"
        f" {len(injection)} ({_ids(planted)})",
    ]


def _latency(samples: Sequence[float]) -> str:
    if not samples:
        return "not measured"
    summary = latency_summary(samples, min_n=LATENCY_MIN_N)
    p95 = "no p95 (n < 20)" if summary.p95_ms is None else f"p95 {summary.p95_ms:.1f} ms"
    return f"n {summary.n}, median {summary.median_ms:.1f} ms, max {summary.max_ms:.1f} ms, {p95}"


def _per_document(values: Sequence[Decimal], render: Callable[[Decimal], str]) -> str:
    if not values:
        return "not measured"
    return f"n {len(values)}, median {render(median(values))}, max {render(max(values))}"


def cost_section(documents: Sequence[DocumentLine], info: ExtractRunInfo) -> list[str]:
    scored = [d for d in documents if d.run == "scored"]
    samples = [d.latency_ms for d in scored if d.latency_ms is not None]
    label = DRY_LATENCY_LABEL if info.dry_run else LOCAL_LABEL
    costs = [d.cost_usd for d in scored]
    complete = all(d.cost_complete for d in scored)
    tokens_in = [Decimal(d.input_tokens) for d in scored]
    tokens_out = [Decimal(d.output_tokens) for d in scored]
    cost = info.cost
    return [
        "",
        "## Latency, tokens and cost per document",
        f"- Latency ({label}): {_latency(samples)}",
        f"- Input tokens: {_per_document(tokens_in, lambda v: f'{v:g}')}; output tokens:"
        f" {_per_document(tokens_out, lambda v: f'{v:g}')}",
        f"- Cost per document: {_per_document(costs, usd)}"
        + ("" if complete else " (a lower bound: a call's cost was incomplete)"),
        f"- Spend: {usd(cost.total_usd)}"
        + ("" if cost.llm_cost_complete else " (a lower bound: a call's cost was incomplete)")
        + f" in {cost.llm_calls} calls"
        + ("" if cost.cap_usd is None else f", cap {usd(cost.cap_usd)}"),
    ]


def render(info: ExtractRunInfo, documents: Sequence[DocumentLine], context: Context) -> str:
    """The whole summary. A `NotImplementedError` from H2 passes through: `runs.write_summary`
    reports it as "H2 not written yet", with the results kept."""
    fields = scored_fields(documents)
    out = header(info, documents, context)
    if not identity_holds(documents, context.metrics):
        at = 4 if info.dry_run else 2  # first, after the title and a dry run's DRY label
        out[at:at] = [INCONSISTENT, ""]
    out += silent_section(fields, context)
    out += accuracy_section(fields, routed_fields(documents), context)
    out += review_section(fields, documents, context)
    out += straight_section(fields, documents, context)
    out += by_id_section(documents, fields)
    out += cost_section(documents, info)
    return "\n".join(out) + "\n"
