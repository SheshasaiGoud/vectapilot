"""`report` (M4 PR 1): counts per intent, business, language group, file and author, the eval
splits, D3's minimums and the verification state.

Counts describe the dataset; they measure nothing about the router. Every intent and every
language group a business lists is printed, zeros included, so an empty cell is visible. shesha
approves PR 2's training rows from these tables without reading them (the blind order, step 2),
and runs `check` and `report` until the minimums hold (PR 5 step 1). The report prints ids and
counts only, never a row's text.
"""

from collections import Counter
from collections.abc import Iterable, Sequence

from vectapilot_api.intenteval.checks import IntentDataset, minimums
from vectapilot_api.intenteval.rows import (
    EVAL_FILE,
    GROUPS_OF_LANGUAGE,
    LANGUAGE_GROUPS,
    SAMPLE_FILE,
    TRAIN_FILE,
    EvalRow,
    TrainRow,
)
from vectapilot_api.intents.taxonomy import DECISION_ORDER

INTENT_WIDTH = 26


def _groups_of(languages: Iterable[str]) -> list[str]:
    """The row language groups a business's languages allow, in `LANGUAGE_GROUPS` order."""
    allowed = {g for language in languages for g in GROUPS_OF_LANGUAGE.get(language, ())}
    return [g for g in LANGUAGE_GROUPS if g in allowed]


def _table(
    title: str, columns: Sequence[str], counts: Counter[tuple[str, str]], lines: list[str]
) -> None:
    """Intents down, `columns` across, a total column and a total row."""
    widths = [max(len(c), 4) + 2 for c in columns]
    lines.append(
        f"  {title:<{INTENT_WIDTH}}"
        + "".join(f"{c:>{w}}" for c, w in zip(columns, widths, strict=True))
        + f"{'total':>7}"
    )
    for intent in (*DECISION_ORDER, "total"):
        cells = [
            sum(n for (i, c), n in counts.items() if c == column and intent in (i, "total"))
            for column in columns
        ]
        lines.append(
            f"    {intent:<{INTENT_WIDTH - 2}}"
            + "".join(f"{n:>{w}}" for n, w in zip(cells, widths, strict=True))
            + f"{sum(cells):>7}"
        )


def _file_tables(
    dataset: IntentDataset,
    file_name: str,
    rows: Sequence[TrainRow | EvalRow],
    authors: Sequence[str],
    lines: list[str],
) -> None:
    for author in authors:
        mine = [r for r in rows if r.author == author]
        lines.append(f"{file_name} · author {author}: {len(mine)} rows")
        for business_id in dataset.manifest.businesses:
            profile = dataset.businesses.get(business_id)
            ours = [r for r in mine if r.business_id == business_id]
            listed = _groups_of(profile.languages if profile else ())
            extra = sorted({r.language_group for r in ours} - set(listed))
            counts: Counter[tuple[str, str]] = Counter(
                (str(r.intent), r.language_group) for r in ours
            )
            _table(business_id, [*listed, *extra], counts, lines)
        lines.append("")


def render_report(dataset: IntentDataset) -> str:
    """The plain-text report (the module docstring)."""
    manifest = dataset.manifest
    train, evals = dataset.train, dataset.eval_rows
    lines = [
        f"{manifest.dataset} {manifest.version} ({manifest.stage}, synthetic): "
        f"{len(train)} train rows, {len(evals)} eval rows",
        "",
    ]
    _file_tables(dataset, TRAIN_FILE, train, ("claude",), lines)
    _file_tables(dataset, EVAL_FILE, evals, ("shesha", "friend"), lines)

    lines.append(f"{EVAL_FILE} splits")
    splits: Counter[tuple[str, str]] = Counter(
        (str(r.intent), r.split or "unassigned") for r in evals
    )
    _table("intent", ("dev", "test", "unassigned"), splits, lines)
    steering = sum(r.steering for r in evals)
    lines += [f"steering rows ({EVAL_FILE}): {steering}", ""]

    lines.append("minimums (what freeze needs; D3)")
    for minimum in minimums(dataset):
        mark = "ok" if minimum.met else "short"
        lines.append(f"  {minimum.what:<66} {minimum.have:>4} of {minimum.need:<4} {mark}")
    lines.append("")

    verified = sum(r.verified for r in train)
    lines.append(f"verification (D3): {verified} of {len(train)} train rows verified")
    if dataset.sample is None:
        lines.append(f"  {SAMPLE_FILE}: none yet (run `sample` once the training rows exist)")
    else:
        by_id = {r.id: r for r in train}
        pending = sum(i in by_id and not by_id[i].verified for i in dataset.sample)
        lines.append(
            f"  {SAMPLE_FILE}: {len(dataset.sample)} ids ({len(dataset.sample)} of {len(train)} "
            f"train rows), {pending} not verified yet"
        )
    draft = manifest.train_draft_sha256
    if draft is None:
        state = "not recorded yet (PR 2 records it)"
    elif draft == dataset.train_sha256:
        state = "yes"
    else:
        state = "no: train.jsonl changed since the draft was recorded"
    lines.append(f"  {TRAIN_FILE} matches train_draft_sha256: {state}")
    return "\n".join(lines) + "\n"
