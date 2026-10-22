# ADR 0012 — Eval dataset v0 (`support_qa` v0.1)

- **Status:** accepted · 2026-10-22 · chosen by shesha (M2 PR 7 decision list: every
  recommendation accepted). Built in M2 PR 7 (noah-evals) after reviews by kenji, zara, ananya and
  maya. Field names and check details live in the dataset README
  (`evals/datasets/support_qa/v0.1/README.md`), which is the authority on them.

**Context.** M2 must report measured hit@5, MRR and correctness (PR 8's harness, PR 9's live run),
and rule 3 makes every number name its dataset version, commit and configuration. There is no real
pilot business yet, so the playbook's "40+ real questions" becomes 40+ human-written synthetic
questions (plan, decided 2026-10-21, and its "Playbook deviations"). Four things make this harder
than a list of questions: `chunks.id` is a random UUID, new at every ingest, and `chunk_index`
moves with the chunk size, so "the expected chunk" is not a stable label; PR 8 tunes the threshold
and the prompt (ADR 0011), so a held-out split must exist before anything is tuned; the same fact
can sit in two documents (`faq` and `faq-hi`, or `faq` and the fees PDF) while `multi_hop` needs
two different facts; and M2 has no safety check (ADR 0011), yet business-context §6's hard gate is
100 % safety-escalation recall.

**Decision** (shesha, 2026-10-22).
1. **Corpus.** Two fictional businesses, `sunrise-clinic` and `paws-and-co`, under
   `data/sample/<business_id>/`: 18 synthetic documents (Markdown, HTML, CSV and one PDF built from
   its `.pdf.txt` text, no new package), each labelled synthetic in its own text and in its
   business's manifest. Contact details are fake (`+91 00000`, `.example`); 112 is the only real
   number. The Hindi `faq-hi` needs shesha's review before the freeze.
2. **Questions.** JSON Lines in `evals/datasets/support_qa/v0.1/`, written by humans only (shesha
   and friends): no agent writes, completes or suggests a question or a label. Expected evidence is
   a list of **evidence groups**; each group holds interchangeable keys, and a key is `source_id` +
   `version` + a verbatim `anchor` of 8–100 characters, resolved inside the question's own
   business and only into approved public documents. A group is found when a hit matches any of
   its keys. `answered` needs at least 1 group, `multi_hop` at least 2, `out_of_scope` none. A hit
   is relevant by content (same business, same source and version, its text contains the anchor),
   never by chunk number. Correctness labels belong to a run
   (`evals/reports/support_qa/v0.1/<run>/`), not to the dataset; a row holds only an optional
   reference answer.
3. **Split and freeze.** `stratified-systematic-v2` at 30 % test, recorded in the manifest before
   any question existed. `freeze` assigns the splits once and writes `freeze.json`: content hashes
   of the questions, of every corpus file and of each business's corpus manifest; CI then refuses
   any change. A change after the freeze needs a new dataset version (a corrected question names
   the one it `replaces` and keeps its split). If `freeze` refuses on balance, add or remove one
   question; never change the seed. An optional `split_group` field (not an evidence group)
   keeps translations and near-twins in one split.
4. **Categories.** Eight, one per question; when two fit, the first in this order wins: `safety` >
   `handoff_request` > `adversarial` > `out_of_scope` > `ambiguous` > `multi_hop` > `policy` >
   `faq`. `action` (M5) and `document` (M6) are refused. An optional `subtype` refines a category.
5. **Ready to freeze** with at least 40 questions, 10 Hindi or Hinglish (Sunrise), 10
   `friend_blind`, 3 per category, 15 per business, 3 `safety` per business and
   8 dont_know questions with no evidence. A question in a language its business does not list
   expects `handoff`.
6. **Friends** write blind from a signboard-level brief (type of business, kinds of services,
   languages; no document, price, timing or name). Everyday emergencies are allowed; nothing about
   self-harm; no personal data (`import-friends` and `check` screen for it).
7. **Safety results are counts** (`k of n`), not a gate, until M4's router and M7's guardrails.
8. **Tuning.** PR 8 tunes only on `dev` and runs `test` once per reported configuration. Numbers
   from the fake embedder are never results (ADR 0008).

**Consequences.** Expected evidence survives reindexing and chunk-size changes, and every hit's
business is checked, so each scored run is also a leak check. Nobody picks the test questions, and
a frozen version stays reproducible. The price: an anchor is an exact quote, so a corpus edit means
a new document version, new keys and a new dataset version (finish the corpus, `faq-hi` included,
before labelling). 40 questions give a 12-question test split, so a category has 1–2 test questions
and only counts are honest there, never rates. The results describe fictional businesses and
human-written questions, not real customers (M11 still needs a real pilot), and M2 cannot claim
§6's safety gate. Every section of v0.1 fits in one chunk at the default size, so the chunk size
cannot be tuned on it (ananya, F4). H2's metrics are written against the group shape.

**Alternatives rejected.**
- *Chunk ids or `chunk_index` as keys:* ids change at every ingest, indexes with the chunk size.
- *Heading paths as keys:* CSV rows and PDF pages have none, headings repeat and change when a
  document is restructured, and one section can state several facts; an anchor names the fact.
- *A flat list of keys:* cannot tell "any of these passages" from "all of these" (F1).
- *A per-id hash split* (test when `hash(id) mod 100 < 30`): with 40 questions a small stratum
  (friends, Hindi, one business, one category) can land almost all in one split; the systematic
  rule gives each its share, give or take one. Its v1, which never put the first position in test
  and so almost never a friend's question, was replaced before any question existed (kenji, K1).
- *YAML:* needs PyYAML (a rule-11 decision) and multi-line diffs; JSONL gives one-line diffs and
  per-line errors, and matches `gateway_smoke`.
- *Correctness labels in the rows:* a label judges one answer from one run; in the row it would
  change the frozen hash at every run and mix "what is correct" with "was this run correct".
- *Agent-written questions:* Claude wrote the corpus, so its questions would echo the documents'
  wording and flatter retrieval; the data must not share the corpus's author.
