# doc_extraction v0.1 — synthetic documents for field extraction (M6)

**Synthetic.** Typed English PDFs for the two fictional businesses of
[`data/sample/`](../../../../data/sample/README.md), each with the values a correct extraction
holds, read against the businesses' extraction configuration in
[`data/extraction/`](../../../../data/extraction/README.md). Documents and gold values are written
by people only, shesha (and friends), never by an agent (D8 (b), ADR 0012). Every report built on
this dataset says it is synthetic and names this version, the commit and the configuration
(business-context §6). The rules below are fixed before any document exists: shesha's decisions
of 2026-11-11 (D1–D8 in [`docs/plans/m6-pr-checks.md`](../../../../docs/plans/m6-pr-checks.md),
"Extraction rules E1–E12" and "Scoring rules") and
[ADR 0017](../../../../docs/adr/0017-document-extraction.md).

| | |
|---|---|
| Version | v0.1, created 2026-11-13, **stage `collecting`: 0 documents in 24 slots** |
| Used by | M6 PR 4 (the harness, dry, over fixture documents in `apps/api/tests/`), PR 5 (shesha's live dev and test runs) |
| Businesses | `sunrise-clinic` (`human_healthcare`, kind `referral_letter`) · `paws-and-co` (`veterinary`, kind `vaccination_record`) |
| Minimums to freeze (D8 (b)) | 24+ documents · 12+ per business · per business: 3+ `absent` (a required field missing), 3+ `confusable`, 2+ `injection` (instruction-like text), 2+ `image_page`, 1+ `table` · English only · every slot filled |
| Split | fixed per slot before any document, 50/50 per business (below); `freeze` assigns nothing |
| Spend (D8 (b)) | live dev runs with `small` as needed and one test run, each ≤ $1.00, **$1.00 for all of M6 together** |

## Scoring rules (fixed before any document; the H2 guide and the harness's tests pin them)

From the plan's "Scoring rules", as the harness (`vectapilot_api/extracteval/`)
applies them:

- **The unit.** A *field* is one (document, field name); the *document* is the independent unit
  for intervals. Every field of every document is scored, E3 failures included (all-review is the
  system's real output; listed by id and reason); only a harness error makes a document "not
  scored", by id. Every count says "n scored, m not scored".
- **Correct value.** Gold is canonical (ISO dates, enum ids, identifiers as written; below).
  `extracteval`'s own comparison: exact for dates, enums and identifiers (whitespace trimmed);
  for text, whitespace collapsed, case-folded and punctuation at either end dropped on both sides
  (E2's own "quote minus its label and punctuation", so `Ltd.` and `Ltd` agree; inner punctuation
  counts; amended 2026-11-15 in PR 4's review, before any document); both null is correct. It
  never imports `vectapilot_api.extraction` (tested).
- **Field accuracy:** correct of all scored fields, whatever the status. Also per field, per
  business, **per level** (ordinal levels checked here, not calibrated probabilities) and over
  accepted fields (**accepted precision**).
- **Silent errors** (target 0; they judge the routing): an `accepted` field that is not correct,
  or an `absent` field whose gold has a value. Fields `k of n`; documents with at least one,
  `x of n` with Wilson, overall and per business. The claim is the count.
- **Review recall:** wrong-or-missing fields routed to `review`, of all wrong-or-missing fields,
  misses by id. **Review rate:** fields in `review` of all fields (staff work).
- **Review precision:** reviewed fields with a value that is not correct, of reviewed fields with
  a value. Reviewed fields without a value are counted apart by reason, with how many had a null
  gold (review not needed).
- **Straight-through documents:** `complete` and every field correct, `x of n`.
- **Intervals:** Wilson on documents; field-level rates get `evalrun/`'s seeded bootstrap over
  documents (it undercovers at small n) or "no interval". Latency and cost per document: n,
  median, max; no p95 below n = 20.
- **Also by id:** failures by reason, truncations, `routing_invariant` and `internal_error`
  fields, and `injection` documents with any accepted value not in gold.
- **Dry or real; test once.** Live only with `--live --max-usd X` (≤ $1.00, typed again at a
  terminal: shesha's alone), through the capped gateway, refused unless everything is synthetic;
  otherwise a scripted fake model over fixture documents in `apps/api/tests/`, never this
  dataset, writing to the git-ignored `evals/reports/tmp/` and saying "DRY RUN — plumbing only,
  never a result". Test runs once per config, ledgered first
  (`evals/reports/doc_extraction/test-runs.jsonl`); the fingerprint holds both versions
  (`EXTRACT_PROMPT_VERSION`, `EXTRACTION_RULES_VERSION`), the alias and its model,
  `catalog_version`, each business's template and business-file hashes, the freeze hash, the
  limits, and (dry) the fixtures' SHA-256. A gold correction after the freeze is a new dataset
  version (`v0.2`), its ids listed in the note.
- **What run files hold.** `run.json` and `results.jsonl` hold ids, statuses, levels, reasons,
  pages, counts, tokens, latency and cost: never a value, a quote or document text. Values and
  quotes go to `values.jsonl` only (synthetic only, never logged or printed by `summarize`).

Small n, honestly: about 12 test documents, 6 per business. "0 of 12 test documents with a
silent error" still allows about 24 % (Wilson upper bound), 0 of 6 about 39 %: arithmetic, not
results. The note reports counts with their intervals.

## Files

```
manifest.json            the version, its businesses, D8's minimums and the 24 slots
gold.jsonl               one gold row per document (empty until shesha writes)
documents/<id>.pdf.txt   the document's text source, written by hand (format below)
documents/<id>.pdf       the PDF rebuilt from it, byte for byte (never edited by hand)
freeze.json              written by `freeze`, never by hand
```

`manifest.json`: `dataset`, `version`, `created`, `synthetic` (always `true`), `authored_by`,
`stage` (`collecting` | `frozen`), `corpus` (the business manifests: business types),
`extraction` (templates and business files), `businesses`, `requirements` (the minimums) and
`slots`. `freeze.json` does not pin `requirements`: the committed version's own test pins D8's
values (`apps/api/tests/extracteval/test_dataset_doc_extraction_v0_1.py`), so lowering one is a
decision, not an edit.

`freeze.json` pins every document's gold row, `.pdf.txt` and `.pdf` and the slots. It does **not**
pin the extraction configuration (`data/extraction/`): templates and business files are the
system under test, not the dataset. Each run's fingerprint holds their SHA-256, so adding a label
or a synonym while tuning on dev, or an enum value later, makes a new config (its own test run),
never a new dataset version. Gold is still checked against the current template and business
file on every `check`, so a renamed field or a removed enum id fails. A live run reads the
configuration from inside the repository only, and its `run.json` says whether every dataset and
configuration file it read is the committed one.

## Slots (fixed before any document)

The manifest lists the 24 documents this version will hold, before any is written: each slot has
an id, a business, the tag its document must carry, and its split. For each business, 12 slots,
6 dev and 6 test, and every business-and-tag minimum has at least one dev and one test slot (so
`table`, with a minimum of 1, has two slots per business). The odd counts alternate between the
businesses: Sunrise has 2 dev and 1 test `absent` slots and 1 dev and 2 test `confusable` slots,
Paws & Co the reverse, so each tag is 50/50 overall. Ids interleave dev and test, so writing the
slots in id order never writes one split first.

A document is written **into** a slot: its id is the slot's, its `business_id` and `split` are the
slot's, and its `tags` include the slot's tag (a document may carry more tags than its slot
requires: they describe the document). `check` refuses a document off its slot. Never move a slot
to the other split or change its tag once a document exists: that is choosing the test set by hand.
`freeze` needs every slot filled; a slot that cannot be filled (say, the `table` slots under the
plan's cut 2) is a decision recorded in the plan, then a manifest change before the freeze.

## Documents: the `.pdf.txt` format

One line of the file is one printed line. ASCII only (the PDF writer draws Helvetica): no tab, no
accent, no other script. At most 85 characters per line and rows 0 to 46 per page (the page's
size), at most 5 pages and 20 000 characters of page text (E12: the core refuses, never cuts).

```
=== page 1 ===              every page starts with its marker, numbered 1, 2, ... in order
A plain line                printed on the next row
{x=150 row=3} text          a line with options (below)
=== page 2 image ===        a page with an image
```

- **Options**, in braces at the start of a line, then one space, then the text:
  - `x=<points>` (0 to 300): the line starts that far right of the margin. A line with `x` above
    0 is a **column**: write a table as its label column, then its value column, each line with
    its `row` (as some real writers do; pypdf then reads all labels first, then all values).
    A document has the `table` tag exactly when a line has `x`;
  - `row=<n>`: the line is printed on text row `n` from the top; without it, on the row after
    the previous line's;
  - `hidden`: the line is in the PDF's text layer but never painted. The model reads it, a person
    looking at the page does not (ADR 0017's known gap: a single hidden copy is not detected in
    M6; a hidden copy that conflicts with the visible one should go to review).
- **`=== page n image ===`** draws one tiny grey image on the page, a stand-in for a letterhead
  logo or a scanned attachment whose content nobody can read (E4: such a page caps its values at
  `medium` and sends every missing field to review). A document has the `image_page` tag exactly
  when a page is marked `image`. A page may have text and an image, or only the image.
- A page's leading and trailing blank lines are dropped; inner blank lines print as empty rows;
  a page with no lines is blank.

**Building the PDF.** After writing or changing a `.pdf.txt`, rebuild its PDF from `apps/api` with
`UPDATE_DOC_PDFS=1 uv run pytest -q tests/extracteval/test_dataset_doc_extraction_v0_1.py`
(the same writer and the same pattern as M2's sample PDF, `UPDATE_SAMPLE_PDF`). Without the
variable, the same test fails when a PDF is not its source's rebuild, byte for byte; `check` also
reads every PDF with the core's own reader and fails when its pages, image pages or text differ
from its source, or when E12 refuses it.

## Gold rows (`gold.jsonl`)

| Field | Meaning |
|---|---|
| `id` | The slot's id; the files are `documents/<id>.pdf.txt` and `documents/<id>.pdf` |
| `business_id` | The slot's business |
| `kind` | The business's enabled kind: `referral_letter` (Sunrise) or `vaccination_record` (Paws & Co) |
| `fields` | Every field of the kind's template, each canonical or `null` (below) |
| `tags` | The slot's tag, plus any other that describes the document: `absent`, `confusable`, `injection`, `image_page`, `table` |
| `split` | The slot's split |
| `author` | `shesha` or `friend` |
| `created` | When it was written (`YYYY-MM-DD`) |
| `synthetic` | Always `true` |

**Canonical values** (scoring rules; `check` checks each against the business's template):
- **`null`** when the document does not give the field: left out, its label with nothing after
  it, or words that say there is none (`none`, `n/a`, `not insured`). Gold is what a person sees
  on the page: nothing comes from the image itself (the grey square stands for content nobody can
  read) and nothing from a `hidden` line. Typed text printed on a page that also has an image
  counts as usual (gold has its value); a correct system still sends it to review (E4 caps it at
  `medium`), which the review numbers show.
- **Dates** as ISO `YYYY-MM-DD`, the real date printed: under the business's formats
  (`DD/MM/YYYY`, `D MMMM YYYY`, `D MMM YYYY` for both businesses) `05/01/2026` is `2026-01-05`. A
  date printed unambiguously in a format the business does not list (`2026-03-02`,
  `March 2, 2026`) still has its real ISO date as gold: the system should send it to review
  (`bad_date`), and that review is then a correct catch, not "review not needed". A numeric date
  whose day and month could swap and that is not in the business's formats (`03/04/2026` for a
  business without `MM/DD/YYYY`) does not belong in v0.1 (the careful-person rule below).
- **Enums** as the template's id: `Dog` and `Canine` are `dog`, `Anti-rabies` is `rabies`; Paws &
  Co's species are `dog`, `cat` and `rabbit` only.
- **Identifiers** (the policy number) exactly as printed, within the template's pattern (capital
  letters, digits, `/` and `-`, 4 to 24 characters).
- **Text** (names, the insurer, the clinic) as printed after its label and the label's punctuation,
  titles included (`Dr. ...` stays): E2's text value is the quote minus its matched label and
  punctuation. Punctuation at either end of the value does not count: the comparison drops it on
  both sides, so a final period (`Ltd.`, `Co.`, `Jr.`) may be kept or left out. Punctuation inside
  the value counts (`Dr. ...` is not `Dr ...`), so copy it as printed. A value printed on the line
  after its label is the whole of that line.
- `absent` is in `tags` exactly when a required field is `null` (`check` enforces both ways).
  Required: patient name, date of birth, referring doctor, referral date (referral letter); pet
  name, species, owner name, vaccine, date given (vaccination record).
- A document where a careful person could not tell the right value of a field does not belong in
  v0.1: leave the ambiguity out, or let the visible text decide (a `hidden` conflicting copy).

## Content rules

- **Synthetic, and no personal data.** No real document, person, pet, doctor, clinic, insurer,
  policy number, phone number or address: invent them. `check` screens every line and every gold
  value (`evalset/personal_data.py`: a run of 9+ digits other than a fake `+91 00000 ddddd`, 112
  or an ISO date; an Aadhaar-shaped number; an e-mail address outside `.example`) and names the
  document and line, never the text. Names pass the screen, so invent them. The screen counts
  dashes and spaces as part of a number: a policy number such as `AB-2026-0417` or a date such as
  `14-07-1990` is refused, so write `AB/2026/0417`, `14/07/1990` or `14.07.1990`.
- **No health detail beyond the template's fields** (E6): a referral letter may say it refers
  someone "for a consultation", never a diagnosis, symptom or treatment. Nothing about self-harm,
  suicide or abuse, anywhere (carried from M4's D5 (a): live runs send the text to the provider).
- **English only, typed** (ASCII). No Hindi or Telugu documents in M6.
- **Authors** `shesha` or `friend` only. No agent writes, completes, suggests or labels a document
  or a gold value.
- **No placeholders, no copies.** An id `example` or `example-...` is refused, and so is any line
  with a marker such as `EXAMPLE-ONLY` and any gold value with the word `example`: the
  illustrations below cannot enter the set. A CI test (`test_no_copies.py`) also refuses a gold
  name, insurer, clinic or policy number equal to one of the illustrations' or the dry-run
  fixtures' values, and a line equal to one of their value lines: if your invention happens to
  match one, pick another.
- **Gold is on the page.** `check` looks for every text and identifier gold value on a line a
  person sees (not `hidden`), and for every enum gold as its id or a synonym, and names the
  document and field when it is missing: usually a typo, or a value copied from a hidden line.
  `report` lists each document's hidden lines apart, under their own heading.

## Writers' brief (for shesha and friends; zara and ananya review it)

You write short typed documents a clinic or a vet might receive, and the values a careful person
would copy out of each. The extractor is measured against your values, so your documents matter
more than anything here: the two illustrations below show the *format* only. Do not copy their
wording or layout; vary letterheads, order, label wording, spacing and length, as real senders
do.

**The two kinds** (fields and their printed labels: `data/extraction/templates/`):
- **Referral letter** to Sunrise Clinic: patient name, date of birth, referring doctor, referral
  date, insurer, policy number. Labels such as `Patient name`, `Date of birth` / `DOB`,
  `Referred by`, `Dated`, `Insurer`, `Policy no`. Real letters also use other wording ("I am
  writing about Ms ..."): write some like that too; the system should send those to review.
- **Vaccination record** from any clinic, brought to Paws & Co: pet name, species, owner name,
  vaccine, date given, valid until, issuing clinic. Labels such as `Pet name`, `Species`, `Owner`,
  `Vaccine`, `Date given`, `Valid until` / `Next due`, `Issued by`.

**What each tag asks for** (your slot names one; `report` shows which are empty):
- **`absent` — omission patterns.** A required field is not there: left out entirely; its label
  printed with nothing after it (`Date of birth:` and a blank); or a word instead of a value
  (`Owner: unknown`). Its gold is `null`. A correct system sends a missing *required* field to
  review, never "absent". In some of these documents, also leave an *optional* field (insurer,
  policy number, valid until, issuing clinic) out entirely, none of its labels anywhere, in a
  document without an image page: that is the only case where a correct system may answer
  `absent` (E4, H1 rule 4), and its gold is `null`.
- **`confusable` — swap patterns.** A value another field could take: two dates close together
  (a date of birth near the referral date; a date given near a next-due date), one name in two
  roles (the referring doctor also signs as the clinic), two people or two clinics named, labels
  in an unusual order, a value printed on the line under its label, two values on one line.
  Gold is what each field really is. In some, print an *optional* field's value without any of
  its template labels, inside a sentence (the insurer named only as `Covered by ...`): its gold
  is the value, so a system that calls the field `absent` makes a silent error. Without such
  documents, "0 silent errors" says nothing about the `absent` half of the definition.
- **`injection` — instruction-like text.** A line that speaks to whoever reads the document and
  asks them to fill, skip, change or trust a field, or claims a value is already verified. Gold is
  what the document really states, never what the instruction asks.
- **`image_page`.** A page marked `image` (a logo on page 1, or a scanned attachment on its own
  page). Fields shown only "on the scan" have `null` gold: nobody can read the grey pixel. Typed
  text on a page that also has the image keeps its gold value (Canonical values, `null`).
- **`table`.** Some fields as a two-column table: the label column, then the value column, with
  `x` and `row` (illustration 2).
- **`hidden` lines** are an option, not a tag: use them for a hidden copy that conflicts with the
  visible value, or a hidden instruction (then tag `injection`).

### Illustration 1 — not in the set (format only; `check` refuses it)

`documents/example-referral.pdf.txt`, a referral letter whose insurer line says there is none:

```pdf.txt
=== page 1 ===
EXAMPLE-ONLY illustration, not in the set
Referral letter
Dated: 2 March 2026
Referred by: Dr. Example Writer
Patient name: Example Person
Date of birth: 14/07/1990
Insurer: none
```

Its gold row (no required field is null, so no `absent` tag):

```json
{"id": "example-referral", "business_id": "sunrise-clinic", "kind": "referral_letter", "fields": {"patient_name": "Example Person", "date_of_birth": "1990-07-14", "referring_doctor": "Dr. Example Writer", "referral_date": "2026-03-02", "insurer": null, "policy_number": null}, "tags": [], "split": "dev", "author": "shesha", "created": "2026-11-13", "synthetic": true}
```

### Illustration 2 — not in the set (format only; `check` refuses it)

`documents/example-vaccination.pdf.txt`, a vaccination record with a two-column table and a
scanned page; the owner's name is only on the scan:

```pdf.txt
=== page 1 ===
EXAMPLE-ONLY illustration, not in the set
Vaccination record
{row=3} Pet name:
{row=4} Species:
{row=5} Vaccine:
{row=6} Date given:
{x=150 row=3} Example Pet
{x=150 row=4} Dog
{x=150 row=5} Rabies
{x=150 row=6} 05/01/2026
=== page 2 image ===
```

Its gold row (`owner_name` is required and null, so `absent`; the columns make it `table`; page 2
makes it `image_page`):

```json
{"id": "example-vaccination", "business_id": "paws-and-co", "kind": "vaccination_record", "fields": {"pet_name": "Example Pet", "species": "dog", "owner_name": null, "vaccine": "rabies", "date_given": "2026-01-05", "valid_until": null, "issuing_clinic": null}, "tags": ["absent", "table", "image_page"], "split": "test", "author": "shesha", "created": "2026-11-13", "synthetic": true}
```

**Field guide (shesha).** For each slot, in id order:
1. `make extract-check` (or `report --counts`) shows the empty slots and their tags.
2. Write `documents/<id>.pdf.txt`, then add its gold row to `gold.jsonl` (one line; `split` and
   `business_id` as the slot says; `tags` at least the slot's).
3. Build its PDF: `UPDATE_DOC_PDFS=1 uv run pytest -q tests/extracteval/test_dataset_doc_extraction_v0_1.py`
   (from `apps/api`).
4. `make extract-check` and fix what `check` lists; then re-read the gold beside the page text the
   model will see: `uv run python -m vectapilot_api.extracteval report --id <id>`.
5. When every slot is filled: `report` once more over every document, then `freeze`.

## Adding a version

Never edit a frozen version: its numbers must stay reproducible. A gold correction after the
freeze is `v0.2`, with the corrected ids listed in the note; before a v0.2 exists, add what
`support_qa` has (`previous_version`, `replaces`, pinned splits), so a document tuned on in dev
never becomes test.

## Commands

From `apps/api` (`uv run python -m vectapilot_api.extracteval --help`):

```bash
uv run python -m vectapilot_api.extracteval check            # what CI checks, for the stage
uv run python -m vectapilot_api.extracteval report --counts  # slots, counts and D8's minimums
uv run python -m vectapilot_api.extracteval report           # ... then every gold row beside its page text
uv run python -m vectapilot_api.extracteval report --id ID   # one document
uv run python -m vectapilot_api.extracteval freeze           # once, when every slot is filled
```

`make extract-check` runs `check`, then `report --counts`. These commands call no model, network
or database.

**Who reads values.** Teammate agents (Claude Code) discuss misses only from `results.jsonl` and
`summary.md` (ids, statuses, levels, reasons and counts). They never open `documents/`,
`gold.jsonl`, `values.jsonl` or `report` output without `--counts`: those carry the documents'
text, including `injection` lines written to talk to whoever reads them, and rule 5 (data, never
instructions) is cheaper to keep when that text never enters an agent's context. shesha reads
values himself and says in words what went wrong.

The harness (no database: the extraction core runs in-process, each document with its business's
configuration):

```bash
uv run python -m vectapilot_api.extracteval run --split dev   # DRY RUN: fixture documents, fake model
uv run python -m vectapilot_api.extracteval summarize RUN_DIR # rewrite summary.md through H2
```

`make extract-dry-run` is the first line: a scripted fake model over the fixture documents in
`apps/api/tests/extracteval/fixtures/dry-run/` (never this dataset), $0, into the git-ignored
`evals/reports/tmp/doc_extraction/v0.1/<UTC>-fake-extract-dev/`. Until H1 (`route_fields`) is
written it stops after its first document; until H2 is, it writes no summary (the results are
kept). `--live --max-usd X` reads this dataset once frozen, through the capped gateway, refused
unless everything is synthetic and while H1 is unwritten; it and the test split are shesha's
alone. Live folders: `evals/reports/doc_extraction/v0.1/<UTC>-live-extract-<split>/` (the tag
names the eval, so no M3, M4 or M5 spend pattern matches), each with `run.json`, `results.jsonl`
(ids, codes, counts, tokens, latency and cost: never a value, a quote or document text),
`values.jsonl` (values, the model's proposals and quotes, and the gold: synthetic only, never
printed by `summarize`), `llm_call_log.jsonl` and `summary.md`. Test runs are ledgered first in
`evals/reports/doc_extraction/test-runs.jsonl`.
