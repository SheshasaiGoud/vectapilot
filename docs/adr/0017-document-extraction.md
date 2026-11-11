# ADR 0017 — Document extraction: configured schemas, verified quotes, levels, review by field

- **Status:** accepted · 2026-11-11, drafted by maya-tech-lead from ananya-rag's playbook targets
  (`.claude/agents/ananya-rag.md`), reviewed read-only the same day by ananya-rag, zara-safety
  and noah-evals (findings applied; adaptations and rejections in the plan's "Review notes").
  shesha decided D1–D8 on 2026-11-11, each as recommended (`docs/plans/m6-pr-checks.md`,
  "Decided 2026-11-11 (shesha)"); parts marked "decided Dn" follow them, the rest are proposals.
  E1–E12 are that plan's extraction rules. Nothing is built yet.

**Context.** The README names M6 "Document extraction pipeline". Ananya's targets: an OCR
fallback, Pydantic extraction schemas, validation rules, a confidence for each field, and routing
low-confidence fields to human review. Business-context §8: "M6: extraction schemas per business
type". Rule 9: nothing business-specific in code; every data path scoped by `business_id`.
Rule 5: uploaded documents are untrusted data, never instructions.

Eight constraints shape the choice:
1. **M3's measurement window** freezes `gateway/models.toml`, `PROMPT_VERSION`, `answer/prompt.py`
   and the gateway's code and output path. Messages are text only (`Message.content: str`,
   `gateway/types.py:319`), so page images need a gateway change. The `small` alias's 1024 output
   tokens are "not sized for M6 document extraction" (`models.toml:63`), and cannot change yet.
2. **Paid calls are shesha's alone** (ADR 0007), and M1–M5's live runs are all pending, so M6 is
   built and tested at $0 with the fake provider.
3. **Health data.** Both fictional businesses are health types (`intents/policy.py:65`). Their
   documents hold personal and health data. §4.1 forbids interpreting reports. Q4c (may Indian
   health data go to the model provider, DPDP) and Q7 (reason for visit) are open; M7 masks.
4. **No OCR engine exists:** no Tesseract binary here and no imaging package in `uv.lock`. Every
   engine is a new dependency (rule 11).
5. **Disk:** the data volume is 96 % used (9.1 GiB free, 2026-11-11).
6. **Isolation** (ADR 0004, 0009): new tables get `FORCE` RLS (which binds owner sessions too),
   composite keys with `business_id` first, and the canary suite on both connections.
7. **Existing parts to reuse:** the KB upload pattern (`kb/router.py:137`, the raise outside
   `except` at `:187`), pypdf, `complete_structured` with one repair (`gateway/gateway.py:323`),
   `choose_boundary`, Wilson and the bootstrap (`evalrun/`), the spend counter and the ledger.
8. **No consumer yet.** Nothing reads an extracted field until M8's review queue and dashboard.

**Options.**
- OCR: none in M6, with a seam and a test fake; Tesseract; Claude vision through the gateway
  (decided D1 (a)).
- Schemas: JSON per business type compiled into Pydantic models; Pydantic classes in code per
  type; JSON Schema with a validator package (decided D2 (a)).
- Extraction: `complete_structured` with `small` and a verified quote per field; the same with
  `large`; rules and regular expressions only (decided D3 (a)).
- Confidence: levels from checks in code; the model's own score; a calibrated probability
  (decided D4 (a)).
- Review: per field; per document; everything reviewed (decided D5 (a)).
- Privacy: minimal (no file, short quote spans, no health-implying field); full quotes and a
  `health_adjacent` category; also store the file; store nothing (decided D6 (a)).

**Decision.** One extraction core: text per page, one structured model call, then checks in code
that decide each field's confidence and whether a human must look at it. The model proposes
values; code decides what is trusted. Each part:
1. **Text and the OCR seam** (decided D1 (a)). PDF only, page by page with pypdf, in its own
   reader under `extraction/` (the ingestion loaders stay untouched while M2/M3's runs are
   pending). Limits ≤ 5 MB, ≤ 5 pages, ≤ 20 000 characters [proposal]; over any limit the upload
   is refused (413) before the model call, never cut (E12). Each page records whether it has text
   and whether it has images (image XObjects or inline images, pypdf only). An `OcrEngine`
   protocol has one production implementation, `NoOcr`, and a fake in tests. A page with an image
   caps its values at `medium` and sends every missing field to review (E4). Images are refused
   (415) until an engine exists.
2. **Schemas from configuration** (decided D2 (a)). A template per business type and document kind,
   `data/extraction/templates/<business_type>/<kind>.json`, and one file per business,
   `data/extraction/businesses/<business_id>.json` (`synthetic: true`, enabled kinds, enum values,
   date formats chosen from the template's allowed ones, extra always-review fields, retention
   days). Read from `VECTAPILOT_EXTRACTION_CONFIG_DIR` (a read-only bind mount). Code holds closed
   vocabularies only: field types `text`, `date`, `enum`, `identifier`; rules `required`, `after`
   and `name` (a closed character set for names); categories `identity`, `contact`,
   `administrative` (no clinical category, §4.1); a closed list of date formats. Every field has
   1–5 `labels` (its printed names, e.g. "Date of birth", "DOB"), in the template only. The loader
   compiles each template with `create_model` into an output model that checks **shape only**
   (`extra="forbid"`, per field `value`, `quote`, `unsure`); types, patterns, rules and lengths
   are checked per field afterwards, so one bad field never costs the document. Startup refuses
   an unknown type, rule or category, a business file that loosens the template (E5), a format
   set mixing day-first and month-first dates, and a kind enabled without `synthetic: true`.
3. **The model call** (decided D3 (a)). `complete_structured(alias="small",
   operation="extract-fields")` on the extraction's own `LLMGateway` (its own timeouts,
   `cache=None`: extraction output is personal data and never enters the response cache). Prompt
   version `extract-fields.1` with its own fingerprint map; `answer/prompt.py` imported, never
   edited. The document sits in a boundary block. Each field returns a value, a verbatim quote and
   an `unsure` flag; ≤ 8 fields so the output fits `small`'s cap. In M6 the route always uses the
   fake provider; paid extraction runs only through `extracteval --live` with its cap (E11).
4. **Checks and confidence** (decided D4 (a)). Team code computes per field (E2): the quote is in
   one page's text (page recorded); the value is in the quote as whole tokens, as the quote's text
   after its label, or as a date token parsed under the business's formats; a field label is in
   the quote; no other field shares the value or quote; the label does not start two lines with
   different values; the date is unambiguous; the page has no image; types and rules pass. Parsers
   return reason codes and never raise. The level is ordinal, with named reasons: `low` if the
   quote, value, type or rule checks fail; `medium` if only `unsure`, OCR or one of the label,
   shared, conflicting, ambiguous or image signals is set; else `high`. No percentage is shown,
   because none is calibrated (rule 3); the eval checks the levels by correctness per level.
5. **Routing** (decided D5 (a); H1, `[HAND-WRITE]` by shesha, decided D7 (b)).
   `route_fields(doc, policy)` is pure: a value is `accepted` only at `high` and outside the
   always-review list; any other value goes to `review`; a missing field goes to `review` if
   required, if its label is in the text, or if any page has an image, else `absent`. The
   document is `complete` only if no field is in review. Two team-code guards surround H1 (E3):
   an invariant check after it forces any `accepted` or `absent` field its signals do not justify
   to `review` (`routing_invariant`, counted), and any model failure, truncation or unexpected
   `Exception` stores an all-review extraction built without H1. A guardrail does not trust the
   check it guards.
6. **Storage and the route** (decided D6 (a)). Migration 0007 adds `extractions` and
   `extraction_fields` under `FORCE` RLS (`business_id` first in every key, composite FK,
   `ON DELETE CASCADE`). The app role gets `SELECT, INSERT` only.
   - File bytes are never stored, only their SHA-256 (as for KB documents). Each field stores its
     value, its page and only the quote's label-to-value span (≤ 40 characters), so clinical text
     next to a value is not kept; `requested_service` is left out until Q7 is answered.
   - `expires_at` is set by the database from the business's retention days (1–90, default 30
     [proposal]). A restrictive policy hides expired rows from the app role; a manual purge, as
     the owner, deletes them one business at a time. Nothing schedules it until M9.
   - `POST /v1/extractions?kind=<kind>` takes the PDF as the raw body. It needs `X-Business-Id`,
     so it exists only in local, test and ci until M8 (`business.py:24`). Uploads are by staff
     and operators only, synthetic documents only until Q4c is answered and M7's controls exist.
   - Logs and records are metadata only; errors are raised outside `except`, because the JSON
     formatter writes a chained exception's text (`log.py:26`); a database error is a 503.
   - Review decisions (accept, correct) are M8's; M6 stores the queue, it does not work it.
7. **Measurement** (decided D8 (b); H2 by Claude at shesha's request, decided D7 (b)). A
   synthetic `doc_extraction` set of at least 24 text-layer PDFs with canonical gold values,
   written by shesha into slots stratified by business and tag and split 50/50 before any is
   written. `extracteval` runs the core in-process
   against each business's configuration and scores with its own comparison, never the
   extraction's code (tested). Field accuracy (also per level and over accepted fields), silent
   errors (an accepted wrong value, or an `absent` field the gold has), review recall, review rate
   and review precision are counted per field and per business; the document is the independent
   unit (Wilson on documents with a silent error; a seeded bootstrap over documents for field
   rates). Live folders end in `-live-extract-<split>` so no other milestone's spend pattern
   matches them (tested both ways). Dry runs use a scripted fake model; live runs are shesha's,
   each ≤ $1.00 and $1.00 in total for M6, counted with M6's own `SpendRules`; test runs once per
   fingerprint, which holds
   `EXTRACT_PROMPT_VERSION` and `EXTRACTION_RULES_VERSION`.
8. **Not in M6.** A real OCR engine, image uploads, types without a business, Hindi and Telugu
   documents (the fixture PDF writer is ASCII only), the review UI and its decisions (M8), masking
   and an emergency screen on document text (M7), a worker queue, a resized `small` (after M3).

**Consequences.**
- **A value is trusted only if the document says it, next to the field's label.** The checks run
  in code, so an injected instruction cannot raise a level; swaps (a date of birth offered as the
  referral date) and omissions ("leave the insurer empty") go to review. Planted text that *is*
  in the document under the right label can still be quoted: rule 5 limits what it can do (a
  value in review or, at `high`, an accepted value), not whether the model reads it.
- **Known gap: hidden text layers.** pypdf reads invisible, white and off-page text. A hidden
  copy that conflicts with a visible one goes to review; a single hidden copy is not detected in
  M6.
- **Extracted values and quotes are untrusted data for every consumer:** escaped in any UI,
  inside a boundary block in any prompt, never an instruction, never an action argument without
  M5's confirmation.
- **Scanned pages, and pages with any image, go to humans** under D1 (a). "OCR fallback" is a
  seam and an honest refusal, not a recognizer; a letterhead logo costs review.
- **Confidence is a level with reasons, not a probability.** Easier to explain and to audit;
  checked by correctness per level, but it cannot rank fields within a level. Calibration needs
  labelled real data (M11).
- **Health data is processed.** Synthetic only in M6. Live runs send synthetic document text to
  the provider; real documents wait for Q4c and M7. Stored rows are hidden at expiry and deleted
  by a manual purge.
- **Accepted fields are unused until M8**, so a routing mistake in M6 harms nothing, and the eval
  measures silent errors before anything relies on them.
- **New isolation surface:** two tables, one write path and the purge join the canary suite.

**Revisit when:**
- M3's window closes: resize `small` or add an `extract` alias; consider vision input (D1 (c)).
- A real business sends scans or photos: choose an OCR engine (D1 (b) or (c)) with a measured
  comparison on scanned variants of the same gold set.
- M7 builds guardrails: hidden-text detection, masking before the provider, and an emergency and
  red-flag screen on page text before any customer-facing upload (M8 widget), under §6's recall
  gate.
- M8 builds the review queue: who sees values, review decisions, corrections as eval cases.
- M9 adds a response-cache store or a scheduler: extraction stays out of the cache; schedule the
  purge.
- Q4c and Q7 are answered: real documents, retention, the provider question, `requested_service`.
- Any silent error appears in a run: stop and fix the checks before any other M6 work.
