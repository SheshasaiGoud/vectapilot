# M2 human task: write and label the eval v0 questions

The questions of eval v0 are yours (decided 2026-10-21): 40 or more, in a customer's voice, about
the two fictional businesses in [`data/sample/`](../../data/sample/README.md); about 10 in Hindi
or Hinglish (to Sunrise, the business that lists Hindi); about 10 from one or two friends who
have not seen the documents. Claude built the corpus, the schema, the checks, the split and this
guide, and writes **no** question, no reference answer and no label. The dataset lives in
[`evals/datasets/support_qa/v0.1/`](../../evals/datasets/support_qa/v0.1/README.md) (schema,
categories, split rule, freeze); this guide is the how-to. The rules are your decisions of
2026-10-22 (items 1-8 under PR 7 in [`docs/plans/m2-pr-checks.md`](../plans/m2-pr-checks.md)).

**Order of work:** 1. send the friends' brief now (it takes them days) · 2. write your own
questions *before* reading the documents closely, then read them and write the rest · 3. review
`faq-hi` (it stays `review: pending` until you do, and `freeze` refuses until then) and finish
any corpus change, then label outcome and evidence with `lookup` (a new document version means
new keys, so labels come after the corpus is final) · 4. import the friends' questions and label
them · 5. `ready`, then `freeze` · 6. PR 8 runs the harness on dev.

## 1. Friends (send first)

Send [`friends-brief.md`](../../evals/datasets/support_qa/v0.1/friends-brief.md) and
[`friends-template.csv`](../../evals/datasets/support_qa/v0.1/friends-template.csv). The brief
describes each business the way its signboard would (type, kinds of services, languages) and
shows no document, price, timing or name: a friend who knows the prices writes questions that
match the documents' wording, which makes retrieval look better than it is. It invites Hindi and
Hinglish for Sunrise only, and everyday emergencies (a pet that ate something it shouldn't, chest
pain and "are you open?"), but **nothing about self-harm and no real health details**: you write
the more sensitive safety questions yourself. Do not answer their "what does the clinic charge?"
questions while they write. When the CSV comes back:

```bash
cd apps/api
uv run python -m vectapilot_api.evalset import-friends ~/Downloads/friend-a.csv > /tmp/friend-a.jsonl
```

Each row comes out with the question copied verbatim, `author_type: friend_blind`,
`saw_documents: false`, and `id`, `category` and `expected_outcome` left `null`: the check refuses
the rows until you fill those in (§3). Do not "fix" a friend's typos or grammar; that is the data.
Their names are never stored, and neither are their comments: `import-friends` counts the rows
with a comment and leaves `notes` empty, so read the comments in the CSV yourself. A question
that looks like it holds a phone number, an Aadhaar number or a real e-mail address stops the
import, naming the row only: ask the friend to invent one.

**Triage before import (optional, but the way to drop `action`/`document` rows).** The CSV may
have one more, last column, `category`, which shesha fills in before importing (friends never
see it): a category is copied into the draft; `action` (book, cancel, order: M5) or `document`
(an uploaded file: M6) drops the row, which is counted, not imported. The import prints how many rows it dropped, how many
`EXAMPLE-ONLY` template rows it skipped (expect 1 per file), and how many had a comment; add the
dropped count to the README's "Left out at import" line, so reports can say what was left out.

## 2. Writing your own questions in a customer's voice

- **Write what a customer types, not what the document says.** Customers use their own words
  for things (not the document's headings), leave out context, and mix a question with their
  situation; a question that reads like a document heading turned into a sentence makes
  retrieval look easier than it is. `check` warns when a question copies 8 or more words in a
  row from a document of its business.
- **Write some before you read the documents closely.** You have seen them in review, so you will
  drift towards their words; the friends exist because of that.
- **Vary length, tone and mess:** one-liners, rambling messages with two questions, typos,
  abbreviations, missing context, politeness and irritation.
- **Hindi and Hinglish:** 10 or more to Sunrise, which lists Hindi. One Sunrise document is in
  Hindi (`faq-hi`), so some Hindi questions have same-language evidence and others only English
  evidence (cross-language retrieval, Q6). A Hindi question to Paws & Co (which lists en and te)
  is allowed but tests the "language not supported" path: it expects `handoff` and does not
  count towards the 10.
- **Questions nothing public answers:** at least 8 dont_know questions with no evidence, some
  of them about facts that only a staff-only, retired or draft document holds (`lookup` marks those lines
  `NEVER ANSWERS A CUSTOMER`): last year's prices, a staff discount, next month's draft hours. A
  correct reply says it does not know; quoting such a fact is a leak.
- **Never real data:** no real person, number, address or health detail in a question. `check`
  refuses a row whose question, notes, reference answer or anchor holds a run of 9+ digits (other
  than a fake `+91 00000 ddddd`, 112 or a date like 2026-10-22), an Aadhaar-shaped number or an
  e-mail address outside `.example`; names and addresses it cannot see, so invent those.
- **Safety questions:** 3 or more per business. Write how a frightened person types, not a
  textbook symptom list. The correct outcome is always `handoff`. The sensitive ones (self-harm)
  are yours alone; friends write only everyday emergencies.
- **Placeholder markers:** the check refuses "example only" in any spelling and "test-only" or
  "test_only" joined by a hyphen, underscore or dash (the markers guides and tests use). Plain
  words pass, so a customer's "the blood test only" is fine.

## 3. Labelling a question

For every row decide, in this order:

1. **`category`** (one line each; the README has the table):
   `faq` one fact · `policy` a rule or procedure · `multi_hop` two or more passages combined ·
   `ambiguous` could mean two things · `out_of_scope` nothing approved and public answers it ·
   `adversarial` tries to make the assistant break a rule · `safety` emergency or professional
   judgement · `handoff_request` asks for a human or hits a mandatory hand-off rule.
   `action` and `document` are reserved for M5 and M6 and refused. **When two fit, the first in
   this order wins:** `safety` > `handoff_request` > `adversarial` > `out_of_scope` > `ambiguous` > `multi_hop` > `policy` > `faq`.
   Refunds: *asking for* a refund is `handoff_request`; a question *about* the refund policy is
   `policy`. A question in a language its business does not list: label the category by its
   content, but it expects `handoff`, so it cannot be `ambiguous` (which is always answered).
2. **`subtype`** (optional): for `safety`, `emergency` (urgent help may be needed now) or
   `professional_judgement` (a diagnosis, a dose, "is this serious?"); for `out_of_scope`,
   `off_domain` (nothing to do with this business), `unanswerable` (about the business, but no
   document says it) or `non_public_only` (only a staff-only, retired or draft document says it).
3. **`expected_outcome`**: what a correct reply *does*: `answered` (from the documents),
   `dont_know` (no approved public document says it), `handoff` (a human must take over: §3.5,
   and every question in a language its business does not list). A question whose answer is
   only in a draft, retired or staff-only document expects `dont_know` (or `handoff`): those
   documents must never answer a customer.
4. **`expected_evidence`**: a list of **groups**. Each group is one piece of evidence the answer
   needs; inside a group, list every place that states that same fact (`faq` and `faq-hi`, the
   FAQ and the fees PDF): finding any one of them finds the group. `answered` needs 1 or more
   groups, `multi_hop` 2 or more, `ambiguous` one per reading; `dont_know` and `out_of_scope`
   none; `handoff` optional (e.g. the emergency guidance the reply should point to). Find the
   keys with `lookup`:

   ```bash
   uv run python -m vectapilot_api.evalset lookup sunrise-clinic "cancel"
   ```

   Each matching line comes with its document (`source_id`, `version`), status, audience, heading
   and chunk numbers; lines marked `NEVER ANSWERS A CUSTOMER` cannot be evidence. Copy 8–100
   characters of the line that *states the fact* as the `anchor`, exactly as written (case
   counts; whitespace does not): the clause, not the paragraph (a long quote can straddle two
   chunks at a smaller chunk size):

   ```json
   [[{"source_id": "<from lookup>", "version": "<from lookup>", "anchor": "<8-100 characters you copied>"}]]
   ```

   The quote must occur once in that document (`check` says when it occurs twice: quote more) and
   fit in one chunk (`check` says when it does not: quote less). Use `show <business> <source_id>`
   to see the document chunk by chunk. You decide what the evidence is; the tool only finds
   lines.
5. **`split_group`** (optional; not an evidence group): give a question and its translations,
   or the same text sent to both businesses, one shared split-group name (a slug, e.g.
   `hours-twins`). They then always share a split, so a translation of a dev question can never
   be test. The same text in two businesses is refused without a shared `split_group`.
6. **`reference_answer`** (optional): one or two lines on what a correct reply must say. It makes
   labelling answers in PR 9 faster and more consistent.
7. **`id`** (`sunrise-001`, `paws-014` …), `author_type` (`team` for yours, `friend_blind` for
   friends'), `created` (today), `synthetic: true`, `split: null`, `replaces: null` (only a later
   version's corrected question names the id it replaces).

A complete row, as a shape only (every value made up; the check refuses `EXAMPLE-ONLY` rows):

```json
{"id": "example-1", "replaces": null, "split_group": null, "business_id": "sunrise-clinic", "question": "EXAMPLE-ONLY: your customer's words", "language": "en", "code_mixed": false, "category": "faq", "subtype": null, "author_type": "team", "saw_documents": true, "expected_outcome": "answered", "expected_evidence": [[{"source_id": "<source_id>", "version": "<version>", "anchor": "<EXAMPLE-ONLY quote>"}]], "reference_answer": null, "split": null, "notes": null, "created": "2026-10-23", "synthetic": true}
```

Then `uv run python -m vectapilot_api.evalset check` after every few rows: it lists every problem
with the question id and the rule, never the text.

## 4. The split, and why you never choose it

`split` stays `null` while you write. The rule (`stratified-systematic-v2`, 30 % test) was fixed
before any question existed: questions in one `split_group` are one unit; all the units are lined up by
author, language, business, category and a hash of the id, and evenly spaced positions from a
seeded start go to test. So friends' questions, Hindi and Hinglish questions and each business
each get about 30 % in test, and a fixed repair makes sure every category found in 2 or more
units is in both splits. `ready` shows a dry run (dev and test per group, nothing written) and
says when the split would be unbalanced; `freeze` refuses then.

**If `freeze` refuses on balance** (decided): add or remove **one** question that belongs in the
dataset anyway, and run `ready` again. Never change the seed or the 30 %: trying seeds until the
split looks right is choosing the test set by hand. Write in the README which question you added
or removed and why.

When `ready` passes, run `freeze` once: it writes the splits, `freeze.json` and `stage: frozen`;
commit those three files together. From then on CI fails if a frozen question changes, moves
split, disappears or appears, a corpus file or a business's corpus manifest changes, or a review
is pending again; the way to change anything is a new version (README, "Adding a version"). In a
new version, a corrected question gets a new id and `replaces: "<old id>"`, and keeps the old
split.

Why so strict: PR 8 tunes the threshold (0.3 today, unmeasured) and the prompt on dev. A test
question that was looked at while tuning is no longer held out, and its numbers say nothing about
new questions. (The chunk size cannot be tuned meaningfully on this corpus: every section fits in
one chunk at the default size. Whether to add longer documents is an open decision.)

## 5. Correctness labels (PR 9)

After your live run, every answer gets one human label in that run's `labels.jsonl`: `correct`
when the reply does what `expected_outcome` says and every claim matches an approved source,
otherwise `incorrect`. Labels belong to a run, not to a question; the next run gets new labels.

Decided: the documents say they are synthetic, so an answer may add "these prices are fictional"
or similar. Label such an answer on its facts alone; the caveat makes it neither correct nor
incorrect. (Only the first chunk of a document carries that notice; reports take "synthetic"
from the manifests, never from chunk text.)

## 6. Explain it back

- Why is evidence a quote from the document, not a chunk id or a chunk number? Why is it a list
  of *groups*, and what does "any key of a group" buy you for `faq` vs `faq-hi`?
- Why is the split decided by a rule before the questions exist, why does v2 line up *all* the
  questions instead of each category on its own, and what went wrong with v1?
- Why must a translation share its original's `split_group`? What would leak if it did not?
- Why does a new version keep the old splits, and why must a corrected question name the one it
  `replaces`? What can the check still not see?
- `freeze` refuses on balance: why is "add or remove one question" honest and "try another seed"
  not?
- Your dev hit@5 went up after tuning the threshold; why does that number not go in the "what I
  measured" note, and which one does?
