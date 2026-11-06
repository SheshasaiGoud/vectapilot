# intents v0.1 — the intent router's dataset (M4)

**Synthetic.** Messages a customer might send to the two fictional businesses of
[`data/sample/`](../../../../data/sample/README.md), each labelled with one of eight intents.
Training rows are drafted by Claude (M4 PR 2); eval rows are written and labelled by people only,
shesha and friends, never by an agent (D3 (a), ADR 0012). Every report built on this dataset says
it is synthetic and names this version, the commit and the configuration (business-context §6).
The rules below are fixed before any row exists: shesha's decisions of 2026-11-05 (D1–D8 in
[`docs/plans/m4-pr-checks.md`](../../../../docs/plans/m4-pr-checks.md)) and
[ADR 0015](../../../../docs/adr/0015-intent-router.md).

| | |
|---|---|
| Version | v0.1, created 2026-11-05, **stage `collecting`: 0 training rows, 0 eval rows** |
| Used by | M4 PR 2 (training draft), PR 4 (embedding, training, evaluation), PR 5 (shesha's runs) |
| Businesses | From their manifests in `data/sample/`: `sunrise-clinic` (`human_healthcare`; en, hi) · `paws-and-co` (`veterinary`; en, te: its rows are English, M4 has no Telugu rows) |
| Minimums to freeze (D3) | 160+ eval rows · 20+ eval rows per intent · per business, 10+ eval rows of each safety intent of its type (`emergency` and `professional_judgement` for both) · per business that lists `hi`, 6+ eval safety rows in Hindi or Hinglish, 3+ of them `emergency` · 40+ training rows per intent (PR 2's proposal) |
| Verification (D3) | 20 %+ of the training rows, seeded and stratified (`sample`), checked by shesha after every eval row is committed |
| Split | 50 % of eval units to test by `stratified-systematic-v2` (below), assigned once by `freeze` |
| Near-duplicates | character 3-gram Jaccard at or above 70 % (below) |

Writers' brief (for shesha and friends): [`writers-brief.md`](writers-brief.md).

## The intents

One intent per message. The definitions are `vectapilot_api/intents/taxonomy.py`'s, word for word
(a test keeps this table, the writers' brief and the module equal; PR 3's fallback prompt quotes
the same lines).

| Intent | One line |
|---|---|
| `emergency` | safety risk or distress, including thoughts of self-harm: a person or an animal may need urgent help now |
| `professional_judgement` | asks for a clinician's or vet's judgement: diagnosis, prognosis or second opinion, what a symptom or test result means, how serious it is, or whether to start, stop or change a medicine, diet or dose |
| `complaint` | unhappy with the service, the staff, a product or an earlier reply, or threatens legal action |
| `refund_or_payment` | asks for money back or reports a payment problem: a refund, a wrong or double charge, a failed payment, a disputed price (asking about the refund policy is `question`) |
| `human_request` | asks to talk to a person instead of the assistant |
| `action_request` | asks the business to do something: book, change or cancel an appointment, place or change an order, arrange a delivery |
| `question` | asks for information a business of this kind could give (hours, prices, services, products, policies, preparation), even about a service this business may not offer |
| `out_of_scope` | not a support request to a business of this kind: general knowledge, chit-chat, or anything unrelated to its services |

**When two fit, the first wins (the decision order):** `emergency` > `professional_judgement` > `complaint` > `refund_or_payment` > `human_request` > `action_request` > `question` > `out_of_scope`.

Two rules follow from it, and the gold labels are the only defence against these shapes that M4
can measure:
- **Any emergency wins.** A message with an emergency anywhere in it is `emergency`, however
  short that part, whatever else it asks, and even when it is phrased as a question.
- **Label what the message is, never what it asks to be filed as.** Anything a message says about
  its own classification is part of the message; a steering row keeps the intent of its real
  request. (Labelled with the intent it asks for, a successful injection would score as correct,
  and an emergency would drop out of safety recall's n.)

The order is monotone in route urgency (`intents/policy.py`): `emergency` is an urgent hand-off;
`professional_judgement`, `complaint` and `refund_or_payment` high; `human_request` and
`action_request` normal; `question` and `out_of_scope` go to RAG, which abstains by itself. So a
message labelled by the first intent that fits is never routed less urgently than it should be.
`out_of_scope` is decided from the text alone: a question about a service the business may not
offer is `question` (unlike `support_qa`'s document-based `out_of_scope`).

## Content rules

- **Synthetic, and no personal data.** Two fictional businesses; no real message, person, phone
  number, address or health detail. `check` screens every text with `evalset/personal_data.py`
  (a run of 9+ digits other than a fake `+91 00000 ddddd`, 112 or an ISO date; an Aadhaar-shaped
  number; an e-mail address outside `.example`) and names the row, never the text. Names and
  addresses pass the screen, so invent them.
- **Emergencies are everyday emergencies. No self-harm rows** (D5 (a)), although the `emergency`
  definition names thoughts of self-harm: eval rows in the LLM band go to the provider in live
  runs, where such messages may fall under its 2-year flagged retention (ADR 0007). Safety recall
  measured on this dataset therefore excludes crisis messages; it is never presented as
  safety-escalation coverage before M7.
- **Languages.** A row's language is one its business lists, and `en` or `hi`: Hinglish is
  `language: hi` with `code_mixed: true` (as in `support_qa`, never `hi-Latn`). No Telugu rows
  (carried from M3), so Paws & Co's rows are English. No e-commerce rows (there is no fictional
  shop in `data/sample/`).
- **Authors.** `train.jsonl` holds `author: claude` rows only; `eval.jsonl` holds `shesha` and
  `friend` rows only. No agent writes, completes, suggests or labels an eval row.
- **No placeholders.** An id `example` or `example-...` is refused, and so is any text with a
  marker such as `EXAMPLE-ONLY` (`evalset`'s rule, in any case or spacing).

## The blind order (D3 (a))

Train and eval must not leak by author or by order of writing.
1. PR 2's training rows are drafted while `eval.jsonl` is empty, and their builder never opens
   that file. At merge, `train.jsonl`'s SHA-256 goes into `manifest.json` (`train_draft_sha256`).
2. kenji and zara review PR 2's text; shesha approves it from `report`'s tables, unread.
3. shesha and friends write every eval row **without reading `train.jsonl`**. shesha reads
   training rows only in the verification step (PR 5 step 2), after the eval rows are committed.
4. A later training row is drafted without opening `eval.jsonl`, and the note says so.

Who saw which rows, and when, goes in shesha's note (`docs/notes/m4-what-i-measured.md`).

## Files

- `manifest.json` — `dataset`, `version`, `created`, `synthetic` (always `true`), `authored_by`,
  `stage` (`collecting` | `frozen`), `corpus` (the business manifests' folder, from the
  repository root), `businesses`, `split_rule`, `near_duplicate`, `requirements` (the minimums
  above), `verification` (the sample's percent and seed), `train_draft_sha256` (`null` until PR 2
  merges; provenance, not a pin).
- `train.jsonl`, `eval.jsonl` — one row per line (JSON Lines, UTF-8; Hindi stays as written).
  Empty until rows exist.
- `verification-sample.txt` — written by `sample`: the training row ids shesha verifies, one per
  line.
- `freeze.json` — written by `freeze`, never by hand.
- `writers-brief.md` — what eval writers get.

## Row schema

| Field | File | Meaning |
|---|---|---|
| `id` | both | A slug, unique across both files; never reused for a changed row |
| `business_id` | both | One of the manifest's `businesses` |
| `language` | both | `en` or `hi`, one the business lists |
| `code_mixed` | both | `true` for Hinglish (`hi` only); default `false` |
| `text` | both | 1–500 characters, exactly as a customer would type it, typos included |
| `intent` | both | One of the eight intents: the first in the decision order that fits |
| `author` | both | `claude` in train only; `shesha` or `friend` in eval only |
| `verified` | train | Whether shesha has checked the label (required; `false` as drafted) |
| `split` | eval | `dev` or `test`, set by `freeze` only; `null` while collecting |
| `split_group` | eval | Rows that must share one split: required for a message and its translations, for one message written for both businesses, and for any near-duplicate pair |
| `steering` | eval | Optional, default `false`: `true` when the message tries to steer its own classification; reported apart (PR 5). Its `intent` is that of its real request, never the one it asks for |
| `created` | both | When the row was written |
| `synthetic` | both | Always `true` |

A field from the other file is refused, and so is a row a schema cannot place (an eval row by
`claude`, a training row without `verified`).

## Near-duplicates

`check` compares every training row with every eval row, and every eval row with every other, by
character 3-gram Jaccard on normalised text (`intenteval/similarity.py`: Unicode NFKC, case-folded,
punctuation and symbols to spaces, whitespace collapsed). A pair at or above **70 %** (fixed now,
in `manifest.json`) is a near-duplicate. `check` lists pairs by id and never edits a row.

- **A training row and an eval row:** refused. The way out: drop the training row, by its id. Do
  not read the eval row's text to decide, and never edit an eval row to slip under the threshold
  (that would choose eval text by looking at train).
- **Two eval rows** (identical text included): they must share a `split_group`, so they land in
  the same split. If they are not meant as a pair, drop one.

It is a screen, not proof of independence: a translation or a paraphrase passes it, which is why
translations need a `split_group` by rule.

## Split rule and freeze

**The rule, fixed before any row exists** (`manifest.json` → `split_rule`,
`stratified-systematic-v2`, 50 % test; details in `intenteval/split.py`): eval rows that share a
`split_group` are one unit and always share a split; every other eval row is a unit of its own.
Line up the units by author, language group, business, intent (in the decision order), then
`sha256("intents:<id>")`, and send `round(U × 50 / 100)` of the `U` units to test at evenly spaced
positions from a seeded offset (`evalset/split.py`'s integer primitives). Evenly spaced positions
give every run of neighbours in the line its share, give or take one: each author, each language
group, each business and each intent get about half in test. A fixed repair then tries to give
every intent found in 2 or more units at least one row in each split; `freeze` refuses if it
cannot (below). Nobody picks which row is test. Never change the seed or the percentage: trying
seeds until a split looks right is choosing the test set by hand.

**Freeze, once** (PR 5 step 3): `freeze` refuses while `check` has a problem, a minimum is short,
or the verification sample is incomplete. It also refuses, writing nothing, when the split it
would write is unbalanced: an intent found in 2 or more units missing from a split; a business's
safety intent (spanning 2 or more units) missing from a split, since safety recall is judged per
business; or the friends' rows, the Hindi and Hinglish rows or any business (each 2 or more rows)
with fewer than a fifth in test, or none in dev. **The way out:** add or remove one eval row (one
that belongs in the dataset anyway) and run `freeze` again, and record here which row and why.
Otherwise it writes each eval row's `split`, `freeze.json` (every row's content hash, each eval
row's split, the split and near-duplicate rules, each business's type and languages, and the
verification sample's ids) and `stage: frozen`. Commit the three files together. From then on the
CI test (`tests/intenteval/test_dataset_intents_v0_1.py`) fails when a frozen row changes, moves
split, disappears or is added, or a rule, a business's type or languages, or the verification
sample change.

**Tune on dev only.** τ, σ and the temperature are fitted on dev (PR 4); test runs once per mode
(ADR 0015 decision 9).

## Verification sample (D3)

`sample` draws at least 20 % of the training rows: in each (intent, business, language group)
stratum, the rows ordered by `sha256("intents-verify:<id>")`, rounded up, so every non-empty
stratum gets at least one. It writes `verification-sample.txt` (committed with PR 2). After every
eval row is committed, shesha checks each listed row's label, corrects it when wrong and sets
`verified: true`; the number of changed labels, `x of n`, goes in the note. `freeze` refuses
unless every listed id is a verified training row and the list holds at least 20 % of the
current training rows (after training rows are added, draw again and verify the new ids).
While `train.jsonl` still matches `train_draft_sha256`, `check` (and so CI) requires the committed
list to be exactly the seeded draw; after shesha's first correction, git history is the audit, and
`freeze.json` pins the list, so it cannot be swapped for another verified 20 % after the freeze.

## Adding a version

Never edit a frozen version: its numbers must stay reproducible. v0.1 is the first version, so the
tools do not yet carry an earlier version's splits. Before a v0.2 exists, add what
`support_qa` has (`previous_version`, a `replaces` field, pinned splits), so a row tuned on in dev
never becomes test.

## Commands

From `apps/api` (`uv run python -m vectapilot_api.intenteval --help`):

```bash
uv run python -m vectapilot_api.intenteval check    # what CI checks, for the current stage
uv run python -m vectapilot_api.intenteval report   # counts, minimums, verification state
uv run python -m vectapilot_api.intenteval sample   # writes verification-sample.txt
uv run python -m vectapilot_api.intenteval freeze   # once, when the minimums hold (writes 3 files)
```

`make intent-check` runs `check` and `report` from the repository root. No command calls a model,
a network or a database.
