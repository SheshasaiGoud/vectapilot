# support_qa v0.1 — eval v0

**Synthetic.** The questions are written by people (the team, i.e. shesha, and one or two
friends who have not seen the documents) in a customer's voice, about the two fictional
businesses of [`data/sample/`](../../../../data/sample/README.md). No agent writes, completes or
suggests a question (decided 2026-10-21). Every report built on this dataset says it is
synthetic, names this version, the commit and the configuration (business-context §6). The
rules below are shesha's decisions of 2026-10-22 (items 1-8 under PR 7 in
[`docs/plans/m2-pr-checks.md`](../../../../docs/plans/m2-pr-checks.md)).

| | |
|---|---|
| Version | v0.1, created 2026-10-22, **stage `collecting`: 0 questions so far** |
| Used by | M2 PR 8 (harness, baseline on the dev split) and PR 9 (shesha's live run) |
| Businesses | `sunrise-clinic` (human healthcare; en, hi) · `paws-and-co` (veterinary + pet store; en, te) |
| Corpus | `data/sample/<business_id>/`: 18 documents (9 per business), checked by `tests/evalset/` |
| Requirements to freeze | 40+ questions · 10+ Hindi or Hinglish to a business that lists `hi` (Sunrise only) · 10+ `friend_blind` · 3+ per category (all but the reserved ones) · 15+ per business · 3+ `safety` per business · 8 dont_know questions with no evidence (`manifest.json` → `requirements`) |
| Corpus review | `freeze` refuses while any document is `review: pending` (today: Sunrise's `faq-hi`, until shesha has read it) |
| Split | 30 % test by `stratified-systematic-v2` (below), assigned once by `freeze` |
| Left out at import | `action` / `document` rows dropped by `import-friends`: none so far (update this line with each import) |

Guides: how to write and label questions, [`docs/learning/m2-eval-questions.md`](../../../../docs/learning/m2-eval-questions.md)
(shesha) · the one-page brief for friends, [`friends-brief.md`](friends-brief.md), with
[`friends-template.csv`](friends-template.csv).

## Files

- `manifest.json` — `dataset`, `version`, `created`, `synthetic` (always `true`), `authored_by`,
  `stage` (`collecting` | `frozen`), `corpus` (folder from the repository root), `businesses`,
  `split_rule`, `requirements`, `previous_version` (the version whose splits this one keeps).
- `questions.jsonl` — one question per line (JSON Lines, UTF-8; Hindi stays as written). Empty
  until questions exist.
- `freeze.json` — written by `freeze`, never by hand: every question's split and content hash,
  every corpus file's SHA-256, and a hash of each business's corpus manifest.
- `friends-brief.md`, `friends-template.csv` — what friends get (no documents).

Why JSON Lines (decided): one question per line gives one-line diffs and per-line error
messages, needs no new package (YAML would need PyYAML, a rule-11 decision), and matches
`gateway_smoke`. The price: JSON escaping by hand. `import-friends` turns a filled CSV into draft
rows, so friends' questions are copied, not retyped.

## Question schema

| Field | Type | Meaning |
|---|---|---|
| `id` | slug | Unique; never reused for a changed question. Suggested: `sunrise-001`, `paws-001` |
| `replaces` | slug or `null` | In a later version only: the id of the earlier question this one corrects (it keeps that question's split) |
| `split_group` | slug or `null` | Questions that must share one split: a question and its translations, or the same text asked of both businesses (twins). Not an evidence group |
| `business_id` | business slug | One of the manifest's `businesses` |
| `question` | text, 1–2,000 characters | Exactly as the customer would type it, typos included |
| `language` | `en` \| `hi` \| `te` | Hinglish is `hi` with `code_mixed: true` |
| `code_mixed` | bool, default `false` | Hindi (or Telugu) mixed with English |
| `category` | see below | `action` and `document` are refused (reserved) |
| `subtype` | see below, or `null` | Optional, for `safety` and `out_of_scope` only |
| `author_type` | `team` \| `friend_blind` | `team` = shesha and the team; `friend_blind` = written from the brief, without the documents |
| `saw_documents` | bool | Must be `false` for `friend_blind` |
| `expected_outcome` | `answered` \| `dont_know` \| `handoff` | What a correct reply does (ADR 0011's outcomes) |
| `expected_evidence` | list of groups of keys | The evidence a correct answer rests on (below) |
| `reference_answer` | text or `null` | Optional: what a correct reply must say, for the correctness labeller |
| `split` | `dev` \| `test` \| `null` | `null` while collecting; set by `freeze` only |
| `notes` | text or `null` | Anything; not part of the content hash |
| `created` | date | When the question was written |
| `synthetic` | `true` | Always |

Rules the check enforces per row:

- **Evidence:** `answered` has 1 or more groups (`multi_hop` 2 or more; `ambiguous` one per
  reading); `dont_know` has none (a document that answers it makes it `answered`);
  `out_of_scope` has none and is not `answered`; `handoff` may have some (what the reply points
  to, e.g. the emergency guidance) or none. A key appears once in a row, in one group.
- **Outcome by category:** `safety` and `handoff_request` expect `handoff` (§3.5); `ambiguous`
  expects `answered`.
- **Language:** a question in a language its business does not list expects `handoff` (Paws &
  Co lists en and te, so a Hindi question to Paws & Co is a hand-off case; it does not count
  towards the Hindi minimum). A code-mixed question is not `en`.
- **Repeats:** no two rows have the same text (case and spacing ignored), except the same text
  in two businesses with one shared `split_group` (twins).
- **Placeholders:** an id `example` or `example-...` is refused, and so is any text with a
  marker, in any case or full-width letters: `example only` in any spacing or punctuation, or
  `test-only` / `test_only` joined by a hyphen, underscore or dash. Plain words pass: "the
  blood test only" is a real question.

**No personal data, checked** (`evalset/personal_data.py`, the same screen the corpus test uses).
`question`, `notes`, `reference_answer` and every anchor are refused when they hold a run of 9+
digits that is not a fake `+91 00000 ddddd`, 112 or an ISO date; an Aadhaar-shaped number; or an
e-mail address outside `.example`. The problem names the question and the field, never the text.
It is a screen, not a proof: names and addresses pass it, so invent them. `import-friends` never
copies a friend's comment (it counts them; read them in the CSV).

### Evidence: groups of stable keys, not chunk ids

```json
"expected_evidence": [
  [{"source_id": "<doc>", "version": "<v>", "anchor": "<quote>"}, {"source_id": "<other doc>", "version": "<v>", "anchor": "<quote>"}],
  [{"source_id": "<doc>", "version": "<v>", "anchor": "<quote>"}]
]
```

**A key** (decided: `source_id` + `version` + verbatim `anchor`). `chunks.id` is a random UUID,
new at every ingest; `chunk_index` moves whenever the chunk size changes, and nothing records the
chunk size stored chunks were made with (re-ingesting an existing document does not re-chunk).
So a key names the document as the business does (`source_id` + `version`, unique per business)
and quotes 8–100 characters of it (`anchor`: whitespace-insensitive, a line break in the document
matches a space; case-sensitive). Quote only the clause that states the fact. The key is always
resolved inside the question's own business, so `fees` means that business's `fees`. A key may
only point at an approved, public document: draft, retired and staff-only documents must never
answer a customer, so they are never evidence; questions about them expect `dont_know` or
`handoff`.

**A group** is one piece of evidence and its interchangeable keys: the same fact stated in
several places (`faq` and `faq-hi`, the FAQ and the fees PDF). Every group is needed; any key
of a group counts as finding that group.

**When labelling**, `check` loads the document with the real loader, finds the quote exactly once,
and lists every chunk that holds all of it at the default chunk size (or `--max-chars`/
`--overlap`).

**When scoring (PR 8), a hit is relevant by content, not by chunk number**
(`evalset/relevance.py`): it belongs to the question's business (taken from the hit's document,
never from the question, so this doubles as a leak check), its `(source_id, version)` equals the
key's, and its normalised text contains the anchor. `matched_groups` gives, for each ranked hit,
the groups it finds; hit@k and MRR on those ranked groups are shesha's H2. That works at any
chunk size and survives reindexing. A key that no chunk holds at a config (an anchor can
straddle a chunk boundary at small sizes) is never dropped: when no key of its group is held, the
question is reported as unscorable there; when another key of the group still finds it, the key
is counted in the report as not held at that config. The report adds a conservative hit@k that
counts every unscorable question as a miss, and two chunk configs are compared on dev only, over
the questions scorable under both (PR 8; decided by shesha, 2026-10-22, ADR 0013).

### Categories

| Category | One line |
|---|---|
| `faq` | one fact from one passage: hours, fees, services, products, doctors or vets |
| `policy` | a rule or procedure: booking, cancelling, the refund or return policy, what to bring |
| `multi_hop` | needs two or more passages, from one document or several, combined |
| `ambiguous` | could mean two things: answered, with evidence for every reading |
| `out_of_scope` | not in this business's approved public documents: an honest no-answer |
| `adversarial` | tries to make the assistant break a rule: leak, invent, obey injected text |
| `safety` | an emergency or a request for professional judgement: diagnosis, dosing, urgency |
| `handoff_request` | asks for a human, or hits a mandatory hand-off rule (complaint, refund request) |
| `action` | asks the assistant to do something (book, cancel, order): tools, M5 |
| `document` | about a document the customer uploads: extraction, M6 |

**When two fit, one order decides** (the first wins): `safety` > `handoff_request` > `adversarial` > `out_of_scope` > `ambiguous` > `multi_hop` > `policy` > `faq`.
Refunds: a customer *asking for* a refund is `handoff_request` (outcome `handoff`); a question
*about* the refund policy is `policy`.

`action` (M5) and `document` (M6) are reserved: v0.1 refuses them, and `import-friends` drops the
rows shesha triages as such and reports how many (the "Left out at import" line above). The
router (M4) maps the categories onto its intents later: `safety` and `handoff_request` are §3.5's
mandatory hand-off triggers; `faq`, `policy`, `multi_hop` and `ambiguous` are the RAG route;
`out_of_scope` and `adversarial` are the refusal and guardrail cases (M7).

**Subtypes** (optional, hashed):

| Category | Subtype | One line |
|---|---|---|
| `safety` | `emergency` | someone, or a pet, may need urgent help now |
| `safety` | `professional_judgement` | asks for a diagnosis, a dose, or whether something is serious |
| `out_of_scope` | `off_domain` | nothing to do with this business |
| `out_of_scope` | `unanswerable` | about this business, but no document says it |
| `out_of_scope` | `non_public_only` | only a staff-only, retired or draft document says it |

`safety` questions are the ones §6's 100 % safety-escalation recall gate is about, and the gate
is per business, so each business needs 3 or more (`min_safety_per_business`). **M2 has no
safety check** (ADR 0011: M4's router and M7's guardrails), so M2 numbers on `safety` are
expected to be poor and are reported, never gated. They are reported as counts (`k of n`),
never as a rate: a 100 % on one or two held-out cases proves nothing. How M7 gates recall (on
every safety row, or on a dedicated safety set) is M7's decision (zara's review, Z2).

### Correctness labels (the human slot)

A label judges one answer from one run, so it belongs to the run, not to the question (decided):
PR 8 writes each run into `evals/reports/support_qa/v0.1/<run>/`, and shesha labels its answers
there in `labels.jsonl` (`CorrectnessLabel`: `question_id`, `run_id`, `label` = `correct` |
`incorrect`, `labelled_by`, `labelled_on`, `note`; loaded by `load_labels`). The dataset row
holds only the optional `reference_answer`, which says what "correct" means for that question.
An answer's "these prices are fictional" caveat is ignored: label on the facts (decided).

## Split rule and freeze

**The rule, fixed before any question exists** (`manifest.json` → `split_rule`,
`stratified-systematic-v2`, 30 % test; `evalset/split.py` has the details): questions that share
a `split_group` are one unit and always share a split; every other question is a unit of its own. Line
up all the units by author type, language group, business, category, then
`sha256("support_qa:<id>")`, and send `round(U × 30 / 100)` of the `U` units to test at evenly
spaced positions that start at a seeded offset. Evenly spaced positions give every run of
neighbours in that line its share, give or take one: friends' questions, Hindi and Hinglish
questions and each business each get about 30 % in test. A fixed repair then gives every
category found in 2 or more units at least one question in each split, by swapping with the
nearest unit whose move leaves every other category where it was (the total stays the same).
Nobody picks which question is test.

v1 (replaced on 2026-10-22, before any question existed; kenji's review, K1) applied the spacing
inside each category with no offset: the first position was never test, and since `friend_blind`
sorted first, friends' questions almost never reached test.

**Balance is checked before anything is frozen.** `ready` prints a dry run of the split (dev and
test per author, language group, business and category; it writes nothing). `freeze` refuses when
`friend_blind`, the Hindi and Hinglish questions or a business has fewer than
`max(1, count // 5)` in test or none in dev, or a category found in 2 or more units is missing
from a split.

### If freeze refuses

- **Review pending:** shesha reviews the document named and sets its manifest entry's `review` to
  `done` (`faq-hi` today). Then `ready` again.
- **Missing:** write the questions `ready` lists as missing.
- **Balance** (decided): add or remove **one** question (one that belongs in the dataset anyway),
  then run `ready` again. Never change the seed or the percentage: trying seeds until the split
  looks right is choosing the test set by hand. Record here which question was added or removed
  and why.

**Freeze, once:** when `ready` passes, `freeze` writes each row's `split`, `freeze.json` and
`stage: frozen`. Commit the three files together. From then on the CI test
(`tests/evalset/test_dataset_v0_1.py`) runs the frozen checks: the requirements, no pending
review, and that no frozen question changed (content hash of every field but `split` and
`notes`, so `split_group`, `subtype` and `expected_evidence` included), moved split, disappeared or
was added, that no corpus file changed, and that no business's corpus manifest changed (status,
audience, version, language, content kind: what may answer a customer).

**Tune on dev only.** PR 8 picks the threshold and the prompt on `dev`
(`checks.select_split(questions, "dev")`) and runs `test` once per reported configuration. A
number measured after tuning on test is not a result. The chunk size is *not* tunable on this
corpus as it stands: every section fits in one chunk at the default size, so a smaller size
changes almost nothing (ananya's review, F4; adding longer documents is open for shesha).

## Adding a version

Never edit a frozen version: its numbers must stay reproducible. A new document version also
forces new keys (keys name the version), so finish the corpus edits and shesha's review of
`faq-hi` *before* labelling starts.

1. Copy this folder to `v0.2/` (or `v1.0/` for an incompatible schema change). Delete the copy's
   `freeze.json`; set `version`, `created`, `stage: collecting` and
   `previous_version: "v0.1"` in its manifest; set every row's `split` back to `null`.
2. Change or add questions. An unchanged question keeps its id. A changed one, even a label-only
   fix, gets a new id **and `replaces: "<old id>"`**: it then keeps the old question's split. A
   new question that joins an existing `split_group` takes that split group's split. The check
   refuses an old id with new content, a `replaces` that names an id not in the previous freeze
   or still present, an id replaced twice, and a split group that joins questions frozen in
   different splits.
3. If a corpus document changes, add it as a new document version (data/sample/README.md).
4. Update this README (counts, what changed and why), then `ready` and `freeze`. `freeze` keeps
   every v0.1 id's split, gives every `replaces` row its old split, and runs the rule on the
   other new units only. So a question tuned on in dev never becomes test, *provided a corrected
   question names the one it replaces*: a rewrite filed as a brand-new question is invisible to
   the check.
5. Point `files.DEFAULT_DATASET` and `tests/evalset/test_dataset_v0_1.py` at the new version.

## Commands

From `apps/api` (`uv run python -m vectapilot_api.evalset --help`):

```bash
uv run python -m vectapilot_api.evalset check     # what CI checks, for the current stage
uv run python -m vectapilot_api.evalset ready     # ... plus what is still missing to freeze
uv run python -m vectapilot_api.evalset report    # counts per business, category, split, ...
uv run python -m vectapilot_api.evalset lookup sunrise-clinic "cancel"   # find an anchor
uv run python -m vectapilot_api.evalset show paws-and-co fees            # a document, by chunk
uv run python -m vectapilot_api.evalset import-friends ~/Downloads/friend-a.csv  # draft rows
uv run python -m vectapilot_api.evalset freeze    # once, when ready passes (writes 3 files)
```

`make eval-check` runs `check` and `report` from the repository root.
