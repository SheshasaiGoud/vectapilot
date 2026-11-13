# M6 hand-write: review routing per field (H1)

This is M6's `[HAND-WRITE]` piece that shesha writes (decided D7 (b)). PR 2 built everything
around it: the page text, the prompt, the one structured model call, and the types routing reads
and returns. **H1**, `route_fields`, is the part that answers M6's interview question, *"how do
you know when to trust an extraction?"*: it turns facts that code computed about each proposed
value into a decision (`accepted`, `review` or `absent`), a level and the reasons, and says
whether the whole document still needs a human. About 50 lines (the plan's estimate, not a
target). The decisions behind every rule are in [ADR 0017](../adr/0017-document-extraction.md)
(decisions 4 and 5) and the plan's "H1" section and E1–E4
([`m6-pr-checks.md`](../plans/m6-pr-checks.md)).

> **Status (2026-11-12, M6 PR 2 scaffold).** The body raises `NotImplementedError`; its tests are
> strict xfails. shesha writes it (D7 (b), decided 2026-11-11). No number in this guide is a
> result: every field, value and document below is made up to explain a rule.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Branch | Who |
|---|---|---|---|---|---|---|
| **H1** | `route_fields` in [`extraction/routing.py`](../../apps/api/src/vectapilot_api/extraction/routing.py) (the last function) | [`tests/extraction/test_routing.py`](../../apps/api/tests/extraction/test_routing.py): 29 tests, 45 cases | about 50 lines | 1.5 h (decided) | `m6/field-routing` | shesha (D7) |

- **The docstring is the spec.** `route_fields`'s docstring states the contract in six numbered
  rules (the plan's "H1" section, word for word in substance); the tests are that contract made
  executable, with section headings named after the rules. Each test builds its expected decision
  *before* calling your function.
- **Already there and tested** (passing now, in the same file, above your function):
  - `FieldSignals`: one field's facts. `has_value`; for a value `quote_found`, `value_in_quote`,
    `label_in_quote`, `codes` (failed type, pattern, length and rule checks), `unsure`,
    `from_ocr`, `shared_value`, `conflicting_values`, `date_ambiguous`, `image_on_page`; for a
    missing value `label_present`.
  - `DocumentSignals`: `fields` (a tuple of `FieldSignals`, each name at most once),
    `has_image_page` and `failure` (one of `FAILURES`, or None).
  - `ReviewPolicy`: `fields` (schema order), `required`, `always_review`; `for_kind` builds it
    from a business's configuration.
  - `FieldDecision(field, status, level, reasons)` and `ExtractionDecision` (`.of(decisions)`
    computes the document's status; `.by_field` reads one field). Both **check themselves when
    built**: an accepted field must be `high` with no reasons, an absent one has neither, a field
    in review has at least one reason. So an impossible decision raises `ValueError` the moment
    you build it, which is a quick way to find a slip.
  - `Level`, `FieldStatus`, `DocumentStatus` and `Reason` (one closed vocabulary), with
    `CHECK_FAILURES` (the eight `codes`) and `FAILURES` (E3's five).
- **Not your job:** computing the signals (PR 2's `signals.py`), the invariant check that runs
  after you (`invariants.py`, team code), the all-review extraction the service builds when
  something fails before routing, storage (PR 3) and the metrics (H2).

## 1. How to work

No database, no network, no key:

```bash
cd apps/api
git switch main && git pull                  # once PR 2 is merged
git switch -c m6/field-routing
uv run pytest tests/extraction/test_routing.py -v -p no:randomly   # 45 xfailed
```

**Reading the output (strict xfail, as in M1–M5).**

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but decides wrongly or raises something else | `FAILED`, with the assert diff |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

**One switch.** The module is marked with one `pytestmark` (as M4's `test_decide.py`). When every
case shows `XPASS(strict)`, delete that `pytestmark` statement and run the full check:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:randomly
```

If PR 3's route has merged before you finish, the switch has moved to `PENDING_HAND_WRITE` in
[`tests/canary/read_paths.py`](../../apps/api/tests/canary/read_paths.py) (as M5's `run_turn`):
remove `"route_fields"` there instead.

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say
so in your PR. `mypy` (strict) and `ruff` apply as everywhere. A change to `routing.py` bumps
`EXTRACTION_RULES_VERSION` (plan, "Scoring rules"; PR 2's `signals.py` defines it): set the new
version there, run `tests/extraction/test_rules_version.py`, and add the fingerprint it prints to
`extraction/rules_fingerprints.py` (keep the old entry). Commit with
your own message, push, open a PR. Reviews: `@agent-ananya-rag` and `@agent-zara-safety`. Then
explain it back (§6).

## 2. The concepts (hint level 1)

### 2.1 Signals are facts; decisions are yours
The model proposes `{value, quote, unsure}` per field. Team code then checks each proposal
against the document and records **facts**: is the quote on one page? is the value really the
quote's value? does the quote name this field? Those facts are `FieldSignals`. Your function never
looks at text: it reads facts and decides. That split is why the checks can be tested on their
own, and why your rules fit in one screen.

### 2.2 Levels, not percentages
"Confidence" here is an ordinal level with named reasons (decided D4 (a)): `low` when a check
failed, `medium` when something makes the value doubtful, `high` otherwise. There is no number,
because no number would be calibrated (rule 3): "0.87" would look precise and mean nothing. A level
is easy to explain to a reviewer ("medium: the quote has no label") and the eval checks it by
correctness *per level* (H2). The model's `unsure` can only lower a level, never raise one (E1).

### 2.3 Fail closed
A wrong accepted value is a **silent error**: nobody looks at it again. A value sent to review
costs a minute of staff time. So every doubt goes to review, and only a value with no doubt at all
and outside the always-review list is accepted. If the extraction failed (the model was down, the
answer was cut off), every field goes to review with that reason (rule 5). Two guards surround
you: the service builds an all-review extraction without you if anything fails before routing,
and the invariant check after you forces back to review any accepted or absent field its signals
do not justify. A guardrail does not trust the check it guards.

### 2.4 Unseen is not absent (E4)
`absent` claims "the document has no such value". You may only claim it when nothing suggests
otherwise: the field is not required, none of its labels is printed anywhere, and no page has an
image (text inside an image is text nobody read; with no OCR engine in M6, every image is unseen
text). Example: an insurer field the model left empty, while the page prints "Insurer: Lotus
Health" — `label_present` is set, so it goes to review. That is how "leave the insurer empty",
planted in the document, fails.

### 2.5 Why labels
A value is trusted only if the document prints it **next to this field's label** (E2). Without the
label check, a date of birth offered as the referral date (a swap) has a real quote and a real
value, and would pass. `label_in_quote` catches it; `shared_value` catches one name used for two
fields; `conflicting_values` catches a label printed twice with different values (for example a
hidden copy). A value must also run to the end of its field: `Asha` quoted as `Patient name: Asha`
from the line `Patient name: Asha Rao` passes every other check, so signals give it the code
`value_cut`, which makes it `LOW` like any other code.

## 3. The contract, with hand-checkable examples
The policy in the tests: fields `name`, `born` (both required), `insurer` (optional) and `policy`
(optional, always review). `ok(field)` means every check passed.

| Signals | Decision | Rule |
|---|---|---|
| `ok("insurer")` | accepted, `high`, no reasons | 2, 3 |
| `ok("name", quote_found=False)` | review, `low`, {`quote_not_found`} | 2, 3 |
| `ok("insurer", unsure=True)` | review, `medium`, {`unsure`} | 2, 3 |
| `ok("name", quote_found=False, unsure=True)` | review, `low`, {`quote_not_found`, `unsure`} | 2, 3 |
| `ok("policy")` | review, `high`, {`always_review`} | 3 |
| `missing("name")` | review, no level, {`not_found`} | 4 |
| `missing("insurer", label_present=True)` | review, no level, {`label_present`} | 4 |
| `missing("insurer")`, a page has an image | review, no level, {`image_page`} | 4 |
| `missing("insurer")`, no image | absent, no level, no reasons | 4 |
| `missing("policy")`, no image | absent (always review is about values) | 4 |
| no signals for `born` | review, no level, {`missing_signal`} | 1 |
| signals for a field not in the policy | `ValueError` | 1 |
| `failure=truncated` | every field review, no level, {`truncated`} | 5 |
| any field in review | document `needs_review` | 6 |

Note the last-but-two rows: a missing *value* on an image page is review, but a *value* found on
another page stays `high` (`image_on_page` is about the quote's own page).

## 4. Test by test, with level-2 hints
- **Rule 1** (5 tests): loop over `policy.fields`, not over `doc.fields`; that gives schema order
  and catches missing signals. Check for unknown names first, before the failure rule.
- **Rule 2** (9 tests, 20 cases): collect the `low` reasons and the `medium` reasons as two sets;
  the level follows from which set is non-empty. Each lowering signal has a reason of the same
  name; "no label in the quote" is `label_not_in_quote`.
- **Rule 3** (4 tests): the reasons of a value in review are both sets plus `always_review` if it
  applies. An accepted field has an empty set, which `FieldDecision` checks.
- **Rule 4** (6 tests): three independent conditions, each adding its own reason; none means
  `absent`.
- **Rule 5** (2 tests, 6 cases): the failure wins over every signal.
- **Rule 6** (2 tests): `ExtractionDecision.of` already computes the status.
- **Pure** (1 test): same inputs, same output, nothing logged, the inputs untouched.

## 5. The hint ladder, the time-box and decisions
The ladder is M1's to M5's: level 1 = §2, level 2 = §4, level 3 = pseudo-code in chat on request
(never in this file), level 4 = a pairing review where you type. **Time-box: 1.5 h** of writing
(reading this guide does not count). When it is up and you are stuck, go one level up. If you ask
Claude to write it, Claude reminds you of the learning goal once and respects your decision; the
explain-back stays part of acceptance.

**TODOs (shesha):**
1. Read `route_fields`'s docstring and §2; run the tests (45 xfailed).
2. Write the body in `extraction/routing.py`; no other file changes except the switch and
   `EXTRACTION_RULES_VERSION`.
3. When every case shows `XPASS(strict)`, remove the switch (§1); run the full check.
4. PR from `m6/field-routing`; reviews by ananya-rag and zara-safety; the explain-back (§6).

**Decided 2026-11-11 (shesha), as they bear on H1:** D4 (a) levels from checks in code, never the
model's own score; D5 (a) review per field, a document `complete` only if no field is in review;
D1 (a) no OCR engine, so pages with images send missing fields to review; D7 (b) shesha writes H1.
Decided 2026-11-12 (PR 1 review): every identifier field is in `always_review`, enforced when a
template loads, so an identifier is never accepted without a human.

## 6. Explain it back (part of acceptance)
- What is the difference between a signal and a decision, and why does the split make both
  easier to test?
- Why is confidence a level with reasons and not a percentage? How will H2 check that the levels
  mean something?
- Walk through "a date of birth offered as the referral date": which signal catches it, which
  level does it get, and where does it end up?
- Why is a missing field on a document with a logo `review`, not `absent`? What does that cost,
  and where will the review rate show it?
- Why does a failure put *every* field in review, and why is the all-review extraction for an
  exception built by the service rather than by your function?
- What does the invariant check after your function protect against, if your function is tested?
