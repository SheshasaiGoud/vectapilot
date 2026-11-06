"""D3's verification sample (M4 PR 1): which training rows shesha checks by hand.

D3 (a): Claude drafts the training rows and shesha verifies at least 20 % of them, seeded and
stratified, *after* every eval row is committed (the blind order, step 3; PR 5 step 2). `sample`
draws the list once PR 2's rows exist and writes it to `verification-sample.txt` (one id per line,
committed with PR 2); shesha then checks each listed row's label, corrects it when wrong, and sets
`verified: true`. The changed labels, `x of n`, go in the note.

**The draw.** Strata are (intent, business, language group); in each stratum the rows are ordered
by `sha256("<seed>:<id>")` (a fixed shuffle nobody steers by choosing text) and the first
`ceil(n * percent / 100)` are taken. Rounding up per stratum gives every non-empty stratum at least
one row, so a small cell (Hinglish emergencies, say) is never skipped, and the whole sample is at
least `percent` of train. Integer arithmetic; the list is sorted by id, so a re-draw diffs cleanly.

**What `freeze` checks** (`sample_problems`): the file exists, every listed id is a training row
and verified, and the list holds at least `percent` of the current training rows (a training row
added later can push the share below it: draw again, and verify the new ids). Verifying rows
beyond the list is allowed; choosing which rows to verify instead of the list is not, and two
checks hold it (noah's review): while `train.jsonl` still matches `train_draft_sha256`, `check`
(and so CI) requires the committed list to equal `draw_sample` (`checks.sample_draw_problems`; a
label correction changes the strata, so after the first one git history is the audit); and
`freeze.json` pins the list's ids, so it cannot be swapped for another verified 20 % after the
freeze.
"""

import hashlib
import re
from collections import defaultdict
from collections.abc import Sequence
from typing import Final

from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.rows import SAMPLE_FILE, TrainRow, Verification
from vectapilot_api.intents.taxonomy import DECISION_ORDER

_ID: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
"""A row id (`gateway.types.Slug`'s pattern; its length limit is checked by the rows)."""


def draw_sample(train: Sequence[TrainRow], verification: Verification) -> tuple[str, ...]:
    """The ids of the verification sample (the module docstring), sorted."""
    strata: defaultdict[tuple[int, str, str], list[TrainRow]] = defaultdict(list)
    key: tuple[int, str, str]
    for row in train:
        key = (DECISION_ORDER.index(row.intent), row.business_id, row.language_group)
        strata[key].append(row)
    chosen: list[str] = []
    for key in sorted(strata):
        rows = sorted(
            strata[key],
            key=lambda r: hashlib.sha256(f"{verification.seed}:{r.id}".encode()).hexdigest(),
        )
        take = (len(rows) * verification.percent + 99) // 100
        chosen += [r.id for r in rows[:take]]
    return tuple(sorted(chosen))


def render_sample(ids: Sequence[str]) -> str:
    return "".join(f"{row_id}\n" for row_id in ids)


def load_sample(text: str) -> tuple[str, ...]:
    """Parse `verification-sample.txt`: one id per line, blank lines skipped. Raises
    `DatasetError` for a line that is not an id, or an id listed twice."""
    ids: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        value = line.strip()
        if not value:
            continue
        if not _ID.match(value):
            raise DatasetError(f"{SAMPLE_FILE} line {number} is not a row id")
        ids.append(value)
    if len(set(ids)) != len(ids):
        raise DatasetError(f"{SAMPLE_FILE} lists an id more than once")
    return tuple(ids)


def sample_problems(
    train: Sequence[TrainRow], sample: Sequence[str] | None, verification: Verification
) -> list[str]:
    """Why the verification does not yet meet D3 (the module docstring, "What freeze checks")."""
    if sample is None:
        return [f"no {SAMPLE_FILE}: run `sample`, then verify every row it lists"]
    rows = {r.id: r for r in train}
    problems = [
        f"{SAMPLE_FILE} lists {i}, which is not a training row" for i in sample if i not in rows
    ]
    unverified = sum(i in rows and not rows[i].verified for i in sample)
    if unverified:
        problems.append(f"{unverified} of {len(sample)} sampled training rows are not verified yet")
    if len(sample) * 100 < verification.percent * len(train):
        problems.append(
            f"{SAMPLE_FILE} lists {len(sample)} of {len(train)} training rows, fewer than "
            f"{verification.percent} %: run `sample` again and verify the new ids"
        )
    return problems
