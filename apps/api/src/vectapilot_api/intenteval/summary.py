"""`summary.md` of an intent run folder (M4 PR 4; plan, "Scoring rules"). Recomputed from the
folder's files every time (`summarize` rewrites it); nothing here is typed in by hand.

Order of every summary: the label (DRY RUN first when dry), provenance (dataset version, freeze
hash, commit, fingerprint), status and the fail-closed count per business *before any rate*, then
safety-escalation recall with every miss by id, the other mandatory rules, route-level accuracy
next to intent macro-F1 and the confusion matrix (H2), the breakdown per business and language
group, calibration (classifier rows), cost and latency with their labels, and for a hybrid folder
the paired comparison. Counts come with Wilson intervals, never as a bare percentage; every
slice (each business, and each business x language group with scored rows) gets macro-F1 or "not
shown (n too small)" with the reason (every intent needs 5 gold rows and 5 split units in the
slice: `scoring.macro_f1_withheld`) and its confusion matrix; the comparison lists discordant
rows by id per slice. H2's functions come in as an `IntentMetrics` bundle: while H2 is unwritten
they raise `NotImplementedError`, and no summary is written.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Final

from vectapilot_api.evalrun.budget import usd
from vectapilot_api.evalrun.intervals import BootstrapInterval, wilson_interval
from vectapilot_api.intenteval import scoring
from vectapilot_api.intenteval.metrics import (
    COLUMNS,
    FAIL_CLOSED_COLUMN,
    RELIABILITY_BINS,
    IntentMetrics,
    ReliabilityBin,
)
from vectapilot_api.intenteval.rows import LANGUAGE_GROUPS
from vectapilot_api.intenteval.runinfo import DRY_LABEL, IntentRunInfo, ResultLine
from vectapilot_api.intents.policy import RoutePolicy
from vectapilot_api.intents.taxonomy import DECISION_ORDER
from vectapilot_api.measure.latency import latency_summary

LATENCY_MIN_N: Final = 20
"""p95 only from 20 samples (M1 H3's rule, ADR 0007)."""
MIX_NOTE: Final = "on the eval mix, not a traffic estimate"
LOCAL_LABEL: Final = "local: shesha's machine and network"
DRY_LATENCY_LABEL: Final = "dry run: the plumbing's own time, never a result"
SCOPE_NOTE: Final = (
    "Safety-escalation recall is reported, not gated, in M4 (gated from M7). Under D5 (a) the"
    " dataset has no self-harm messages: this recall excludes crisis messages and is never"
    " quoted as safety-escalation coverage. Telugu and e-commerce messages are not measured."
)


@dataclass(frozen=True)
class Context:
    """What a summary needs besides the folder's own run.json and results."""

    policies: Mapping[str, RoutePolicy]
    businesses: Sequence[str]
    """The dataset manifest's order."""
    metrics: IntentMetrics
    embedding_usd_per_message: Decimal | None = None
    """The router's own embedding call per eval row (from the embed folder), or None."""


def _pct(value: float) -> str:
    return f"{value * 100:.1f} %"


def counted(x: int, n: int) -> str:
    """`x of n (95 % Wilson low to high)`, or `not measured` for n = 0."""
    if n == 0:
        return "not measured"
    interval = wilson_interval(x, n)
    return f"{x} of {n} (95 % Wilson {_pct(interval.low)} to {_pct(interval.high)})"


def _ids(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "none"


def _fraction(value: Fraction) -> str:
    return f"{float(value):.3f} ({value})"


def _interval(boot: scoring.UnitBootstrap) -> str:
    interval: BootstrapInterval = boot.interval
    return (
        f"{float(interval.low):.3f} to {float(interval.high):.3f} (95 % percentile bootstrap"
        f" over {interval.n} split units, seed {interval.seed}, B {interval.resamples};"
        f" approximate at small n; {boot.lacking} of {interval.resamples} resamples lacked an"
        " intent, whose F1 then counts as 0)"
    )


def _fingerprint(info: IntentRunInfo) -> str:
    """The fields this mode uses (a None field belongs to another mode)."""
    fp = info.fingerprint
    parts = [
        ("artifact", None if fp.artifact_sha256 is None else fp.artifact_sha256[:12]),
        ("T", fp.temperature),
        ("tau", "none (no confidence band)" if fp.no_confidence_band else fp.tau),
        ("sigma", fp.sigma),
        ("prompt", fp.router_prompt_version),
        ("catalog", fp.catalog_version),
        ("model", fp.router_model),
    ]
    return ", ".join(f"{name} {value}" for name, value in parts if value is not None) or "none"


def header(info: IntentRunInfo, lines: Sequence[ResultLine]) -> list[str]:
    out = [f"# intents {info.kind} run {info.run_id}", ""]
    if info.dry_run:
        out += [f"**{DRY_LABEL}** ({info.dry_reason or 'dry'}).", ""]
    split: str = info.split or "all rows"
    if info.split == "dev":
        split += " (used for tuning T, tau and sigma: never a test result)"
    out += [
        f"- Dataset: {info.dataset} {info.dataset_version} ({info.dataset_stage}), freeze"
        f" {(info.freeze_sha256 or 'none')[:12]}, files at the commit:"
        f" {'yes' if info.dataset_committed else 'NO'} · split: {split}",
        f"- Commit {info.commit[:12]}"
        + (" (with uncommitted changes)" if info.uncommitted_changes else ""),
        f"- Fingerprint {info.fingerprint_sha256[:12]}: {_fingerprint(info)}",
    ]
    if info.inputs:
        out.append(f"- Inputs: {', '.join(info.inputs)} (hybrid: the LLM answers replayed)")
    if info.split == "test":
        rerun = (
            "the first ledgered run of this mode"
            if info.test_runs_before == 0
            else f"a rerun ({info.test_runs_before} before; reason {info.test_rerun_reason})"
        )
        out.append(f"- Test: {rerun}")
    not_run = sum(line.decided_by == "not_run" for line in lines)
    status = info.status + (f" ({info.stop})" if info.stop else "")
    out.append(
        f"- Status: {status} · {len(lines) - not_run} rows scored, {not_run} not run"
        + (" · partial: never a result" if info.status != "complete" else "")
    )
    return out


def fail_closed_lines(lines: Sequence[ResultLine], businesses: Sequence[str]) -> list[str]:
    by_business = scoring.fail_closed_by_business(lines)
    parts = ", ".join(f"{b} {by_business[b]}" for b in businesses)
    return [
        "",
        f"Fail-closed rows (the LLM raised a GatewayError; counted before any rate): {parts}.",
    ]


def safety_section(lines: Sequence[ResultLine], context: Context) -> list[str]:
    out = [
        "",
        "## Safety-escalation recall",
        "",
        SCOPE_NOTE,
        "",
        "| business | decided as safety | missed | handed off, wrong priority | not decided"
        " (fail-closed) |",
        "|---|---|---|---|---|",
    ]
    for business in [*context.businesses, None]:
        rows = [line for line in lines if business is None or line.business_id == business]
        recall = scoring.safety_recall(rows, context.policies)
        out.append(
            f"| {business or 'all'} | {counted(recall.k, recall.n)} | {_ids(recall.missed)} |"
            f" {_ids(recall.wrong_priority)} | {_ids(recall.not_decided)} |"
        )
    total = scoring.safety_recall(lines, context.policies)
    out += [
        "",
        f"The claim is the count: {total.n - total.k} of {total.n} safety rows not decided as"
        " safety, never a percentage.",
    ]
    return out


def mandatory_section(lines: Sequence[ResultLine], context: Context) -> list[str]:
    out = ["", "## Other mandatory rules (handed off at any priority)", ""]
    for business in [*context.businesses, None]:
        rows = [line for line in lines if business is None or line.business_id == business]
        recall = scoring.mandatory_recall(rows, context.policies)
        out.append(
            f"- {business or 'all'}: {counted(recall.k, recall.n)}, of which"
            f" {recall.fail_closed} fail-closed; not handed off: {_ids(recall.missed)}"
        )
    return out


def matrix_lines(lines: Sequence[ResultLine], metrics: IntentMetrics) -> list[str]:
    rows = scoring.scored(lines)
    matrix = metrics.confusion_matrix([r.gold for r in rows], [r.final_intent for r in rows])
    names = [c.value if c is not None else FAIL_CLOSED_COLUMN for c in COLUMNS]
    out = ["| gold \\ decided | " + " | ".join(names) + " |", "|---" * (len(names) + 1) + "|"]
    for gold in DECISION_ORDER:
        cells = " | ".join(str(matrix.cell(gold, column)) for column in COLUMNS)
        out.append(f"| {gold.value} | {cells} |")
    return out


def accuracy_section(
    lines: Sequence[ResultLine], context: Context, info: IntentRunInfo
) -> list[str]:
    route = scoring.route_accuracy(lines, context.policies)
    intent = scoring.intent_accuracy(lines)
    out = [
        "",
        "## Route-level accuracy and intent macro-F1",
        "",
        "- Route (rag, or hand-off x priority: what the product acts on):"
        f" {counted(route.x, route.n)}",
        f"- Intent (fail-closed is wrong): {counted(intent.x, intent.n)}",
    ]
    out.append(macro_f1_line(lines, context, info))
    return [*out, "", "Confusion matrix (H2):", "", *matrix_lines(lines, context.metrics)]


def macro_f1_line(lines: Sequence[ResultLine], context: Context, info: IntentRunInfo) -> str:
    """Macro-F1 with its unit bootstrap, or why a slice too small does not show it."""
    withheld = scoring.macro_f1_withheld(lines)
    if withheld is None:
        value = scoring.macro_f1_of(lines, context.metrics)
        boot = scoring.bootstrap_macro_f1(
            lines, context.metrics, seed=info.bootstrap_seed, resamples=info.bootstrap_resamples
        )
        grouped = sum(len(unit) for unit in scoring.units(lines) if len(unit) > 1)
        return (
            f"- Macro-F1 over the eight intents (H2): {_fraction(value)}; {_interval(boot)};"
            f" {grouped} rows sit in shared split units"
        )
    return f"- Macro-F1 not shown (n too small): {withheld}"


def _slices(
    lines: Sequence[ResultLine], businesses: Sequence[str]
) -> list[tuple[str, list[ResultLine]]]:
    """Each business (all language groups), then each of its language groups, with scored rows."""
    out: list[tuple[str, list[ResultLine]]] = []
    for business in businesses:
        mine = [line for line in lines if line.business_id == business]
        if scoring.scored(mine):
            out.append((f"{business}, all language groups", mine))
        for group in LANGUAGE_GROUPS:
            rows = [line for line in mine if line.language_group == group]
            if scoring.scored(rows):
                out.append((f"{business}, {group}", rows))
    return out


def breakdown_section(
    lines: Sequence[ResultLine], context: Context, info: IntentRunInfo
) -> list[str]:
    out = [
        "",
        "## Per business and language group",
        "",
        "Channel: not applicable offline.",
        "",
        "| business | language group | route | intent | safety decided as safety |",
        "|---|---|---|---|---|",
    ]
    for business in context.businesses:
        for group in LANGUAGE_GROUPS:
            rows = [
                line
                for line in lines
                if line.business_id == business and line.language_group == group
            ]
            if not scoring.scored(rows):
                out.append(f"| {business} | {group} | not measured | not measured | not measured |")
                continue
            route = scoring.route_accuracy(rows, context.policies)
            intent = scoring.intent_accuracy(rows)
            safety = scoring.safety_recall(rows, context.policies)
            out.append(
                f"| {business} | {group} | {counted(route.x, route.n)} |"
                f" {counted(intent.x, intent.n)} | {counted(safety.k, safety.n)} |"
            )
    for name, rows in _slices(lines, context.businesses):
        out += [
            "",
            f"### {name}",
            "",
            macro_f1_line(rows, context, info),
            "",
            "Confusion matrix (H2):",
            "",
            *matrix_lines(rows, context.metrics),
        ]
    return out


def _bins_table(raw: Sequence[ReliabilityBin], calibrated: Sequence[ReliabilityBin]) -> list[str]:
    out = [
        "| bin | raw n | raw mean confidence | raw accuracy | calibrated n | calibrated mean"
        " confidence | calibrated accuracy |",
        "|---|---|---|---|---|---|---|",
    ]
    for a, b in zip(raw, calibrated, strict=True):

        def cells(bin_: ReliabilityBin) -> str:
            if bin_.n == 0:
                return f"{bin_.n} | — | —"
            mean = bin_.mean_confidence if bin_.mean_confidence is not None else 0.0
            return f"{bin_.n} | {mean:.3f} | {bin_.correct} of {bin_.n}"

        close = "]" if a.high == 1 else ")"
        out.append(f"| [{float(a.low):.1f}, {float(a.high):.1f}{close} | {cells(a)} | {cells(b)} |")
    return out


IN_SAMPLE_NOTE: Final = (
    "On dev the calibrated values are in-sample (T was fitted on these rows): optimistic. The"
    " raw-against-calibrated comparison counts on test only."
)


def calibration_section(
    lines: Sequence[ResultLine], context: Context, info: IntentRunInfo
) -> list[str]:
    rows = [
        line
        for line in scoring.scored(lines)
        if line.top_probability is not None and line.top_probability_raw is not None
    ]
    if not rows:
        return []
    right = [line.classifier_intent is line.gold for line in rows]
    raw_conf = [line.top_probability_raw or 0.0 for line in rows]
    cal_conf = [line.top_probability or 0.0 for line in rows]
    raw = context.metrics.reliability_bins(raw_conf, right)
    calibrated = context.metrics.reliability_bins(cal_conf, right)
    ece_raw = context.metrics.expected_calibration_error(raw)
    ece_cal = context.metrics.expected_calibration_error(calibrated)
    out = [
        "",
        f"## Calibration of the classifier's top-1 ({RELIABILITY_BINS} equal-width bins, H2)",
        "",
        *_bins_table(raw, calibrated),
        "",
        f"ECE at n = {len(rows)}: raw (T = 1) {ece_raw:.3f}, calibrated {ece_cal:.3f}. ECE is"
        " biased upward at small n (a bin's accuracy is noisy, and the gap is taken in absolute"
        " value).",
        *(["", IN_SAMPLE_NOTE] if info.split == "dev" else []),
        "",
        "Per business (counts only): "
        + "; ".join(
            f"{b} {sum(r.business_id == b for r in rows)} rows,"
            f" {sum(ok for r, ok in zip(rows, right, strict=True) if r.business_id == b)} top-1"
            " right"
            for b in context.businesses
        ),
    ]
    return out


def _latency(samples: Sequence[float]) -> str:
    if not samples:
        return "not measured"
    summary = latency_summary(samples, min_n=LATENCY_MIN_N)
    p95 = "p95 not shown (n < 20)" if summary.p95_ms is None else f"p95 {summary.p95_ms:.1f} ms"
    return f"n {summary.n}, median {summary.median_ms:.1f} ms, {p95}, max {summary.max_ms:.1f} ms"


def cost_section(lines: Sequence[ResultLine], info: IntentRunInfo, context: Context) -> list[str]:
    rows = scoring.scored(lines)
    if not rows:
        return []
    label = DRY_LATENCY_LABEL if info.dry_run else LOCAL_LABEL
    if info.kind == "hybrid":
        label += "; composed from replayed calls"
    out = ["", f"## Cost and latency ({MIX_NOTE})", ""]
    llm_costs = [line.cost_usd for line in rows if line.cost_usd is not None]
    llm_total = sum(llm_costs, Decimal(0))
    incomplete = any(not line.cost_complete for line in rows if line.decided_by != "classifier")
    per_thousand = llm_total / len(rows) * 1000
    embedding = context.embedding_usd_per_message
    if info.kind in ("classifier", "hybrid"):
        own = "not known" if embedding is None else usd(embedding * 1000)
        out.append(
            f"- Embedding (the router's own query embedding) per 1,000 messages: {own}; marginal"
            " when reusing retrieval's vector: $0 (a projection, nothing measured)"
        )
    if info.kind in ("llm", "hybrid"):
        bound = " (a lower bound: a call's cost was incomplete)" if incomplete else ""
        if info.dry_run:
            bound += " (dry run: the fake provider's usage at list prices, never a cost)"
        out.append(
            f"- LLM calls per 1,000 messages: {usd(per_thousand)}{bound} ({len(llm_costs)} calls"
            f" for {len(rows)} rows)"
        )
    if info.kind == "hybrid" and embedding is not None:
        total = embedding * 1000 + per_thousand
        out.append(
            f"- Total per 1,000 messages (the router's own embedding and its LLM calls):"
            f" {usd(total)}"
        )
    latency = [line.latency_ms for line in rows if line.latency_ms is not None]
    out.append(f"- Latency per message ({label}): {_latency(latency)}")
    if info.kind in ("classifier", "hybrid"):
        scoring_ms = [line.scoring_ms for line in rows if line.scoring_ms is not None]
        out.append(
            f"- Scoring alone (a projection of reusing retrieval's vector): {_latency(scoring_ms)}"
        )
    if info.kind == "hybrid":
        shares = scoring.fallback_share(lines)
        parts = "; ".join(f"{i.value} {counted(c.x, c.n)}" for i, c in shares.items())
        asked = sum(c.x for c in shares.values())
        out.append(
            f"- Fallback share ({MIX_NOTE}): {counted(asked, len(rows))}; per gold intent: {parts}"
        )
        kept = sorted(line.id for line in rows if line.decided_by == "classifier_kept")
        out.append(f"- classifier_kept (the LLM would have loosened the route): {_ids(kept)}")
    return out


def comparison_section(
    modes: Mapping[str, Sequence[ResultLine]], info: IntentRunInfo, context: Context
) -> list[str]:
    """Classifier and hybrid each against LLM-only, paired by row and split unit."""
    llm = modes["llm"]
    out = ["", "## Comparison against LLM-only (paired)", ""]
    for name in ("classifier", "hybrid"):
        lines = modes[name]
        pairs = scoring.discordant(lines, llm)
        out += [
            f"### {name} vs LLM-only",
            "",
            f"- {name} right, LLM wrong: {_ids(pairs.only_first)}; safety rows:"
            f" {_ids(pairs.only_first_safety)}",
            f"- LLM right, {name} wrong: {_ids(pairs.only_second)}; safety rows:"
            f" {_ids(pairs.only_second_safety)}",
        ]
        for business in context.businesses:
            for group in LANGUAGE_GROUPS:

                def keep(line: ResultLine, b: str = business, g: str = group) -> bool:
                    return line.business_id == b and line.language_group == g

                mine = [line for line in lines if keep(line)]
                if not scoring.scored(mine):
                    continue
                d = scoring.discordant(mine, [line for line in llm if keep(line)])
                out.append(
                    f"  - {business}, {group}: {name} right, LLM wrong: {_ids(d.only_first)};"
                    f" safety rows: {_ids(d.only_first_safety)} · LLM right, {name} wrong:"
                    f" {_ids(d.only_second)}; safety rows: {_ids(d.only_second_safety)}"
                )
        withheld = scoring.macro_f1_withheld(lines) or scoring.macro_f1_withheld(llm)
        if withheld is None:
            boot = scoring.bootstrap_difference(
                lines,
                llm,
                context.metrics,
                seed=info.bootstrap_seed,
                resamples=info.bootstrap_resamples,
            )
            n = len(scoring.scored(lines))
            if boot.interval.low <= 0 <= boot.interval.high:
                verdict = f"no difference detected at n = {n}"
            else:
                verdict = f"a difference at n = {n} (never 'as good as' either way)"
            out.append(
                f"- Macro-F1 difference ({name} minus LLM-only): {_interval(boot)}: {verdict}"
            )
        else:
            out.append(f"- Macro-F1 difference not shown (n too small for macro-F1): {withheld}")
        out.append("")
    return out


def render(
    info: IntentRunInfo,
    lines: Sequence[ResultLine],
    context: Context,
    *,
    modes: Mapping[str, Sequence[ResultLine]] | None = None,
) -> str:
    """The whole summary.md (module docstring). Raises `NotImplementedError` while H2 is
    unwritten; nothing is then written."""
    out = header(info, lines)
    out += fail_closed_lines(lines, context.businesses)
    out += safety_section(lines, context)
    out += mandatory_section(lines, context)
    out += accuracy_section(lines, context, info)
    out += breakdown_section(lines, context, info)
    out += calibration_section(lines, context, info)
    out += cost_section(lines, info, context)
    if modes is not None:
        out += comparison_section(modes, info, context)
    return "\n".join(out).rstrip("\n") + "\n"
