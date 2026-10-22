# Sample corpus — synthetic and fictional

**Everything under `data/sample/` is synthetic.** The businesses are the fictional examples of
[`docs/business-context.md` §7](../../docs/business-context.md); every document was written by
Claude (noah-evals) for M2 PR 7 on 2026-10-22, to be ingested and asked about by the eval v0
dataset ([`evals/datasets/support_qa/`](../../evals/datasets/support_qa/)). Nothing here describes a
real clinic, shop, person, insurer, product or address, and nothing here is medical or veterinary
advice.

- **Names** of doctors, vets, insurers, brands and places are invented (surnames such as
  "Testwala", "Samplekar", "Mockford"); any resemblance to a real one is a coincidence.
- **Contact details** are fake on purpose: phone numbers start with `+91 00000`, e-mail addresses
  and websites use the reserved `.example` domain (RFC 2606). The one real number is the public
  emergency number **112**, as in business-context §7's own example configuration: a fake number
  in emergency guidance would be the more dangerous mistake.
- **Prices** are invented (INR) and change between the two businesses on purpose.
- **Health content** stays administrative (§4): services, fees, policies, preparation instructions
  "as your doctor tells you", and emergency guidance that only says *whom to call*. No diagnosis,
  no medicine, no dose. The red-flag lists in `emergency` (and `faq-hi`'s emergency section)
  follow business-context §4's examples, are written as examples with a catch-all line ("if in
  doubt, call ..."), and have **not** been reviewed by a professional (Q8). They are
  customer-facing text, never a control: M4's router and M7's guardrails decide escalation.
- Every document says in its own text that it is synthetic (a first line, a CSV `note` cell, or
  the PDF's first line), and every corpus and dataset manifest says `synthetic: true`. The label
  is per document and per manifest, **not per chunk**: only a document's first chunk carries the
  notice. Reports (PR 8 on) take "synthetic" from the manifests, never from chunk text.

## Layout

```
data/sample/<business_id>/
  manifest.json          what each file is: the upload metadata of POST /v1/kb/documents
  <file>.md|.html|.csv|.pdf
  <file>.pdf.txt         the text source a PDF is built from (not a knowledge source itself)
```

`manifest.json` holds the business (`business_id`, `name`, `business_type`, `languages`,
`default_language`, `synthetic: true`, `created`, `authored_by`) and one entry per document with
exactly the query parameters the upload endpoint takes (`source_id`, `version`, `title`,
`content_kind`, `language`, `status`, `audience`, `review_by`) plus `file` and `format`
(`markdown`, `html`, `csv`, `pdf`) and, where shesha must check a document Claude wrote in a
language other than English, `review` (`pending` until shesha has read it, then `done`; decided
2026-10-22, item 1). The eval dataset's `freeze` refuses while any document is `pending`. It is
checked by `vectapilot_api.evalset.corpus` (`tests/evalset/test_sample_corpus.py`): every listed file loads and chunks with the real
ingestion code, every file in the folder is listed, at most one version per source is approved
(ADR 0010), and contact details look fake.

## What the two businesses exercise

| | `sunrise-clinic` (human healthcare; en, hi) | `paws-and-co` (veterinary + pet store; en, te) |
|---|---|---|
| Formats | Markdown, HTML, CSV, **PDF** (`fees-2026-04.pdf`) | Markdown, HTML, CSV |
| Content kinds | faq, service_list, pricing, appointment_policy, other | faq, service_list, pricing, product_catalogue, appointment_policy, other |
| Never answers (status) | `fees` **2025-10 retired** (older prices) | `holiday-hours` **2026-12 draft** |
| Never quoted to customers | `staff-handbook` (staff_only) | `staff-notes` (staff_only) |
| Safety guidance | `emergency` (call 112; the assistant does not assess symptoms) | `emergency` (fictional clinic line and 24-hour animal hospital) |
| Not English | `faq-hi` (Hindi; its entry says `review: pending` until shesha checks it) | none (Telugu is out of scope for M2) |

**Same source ids, different facts (isolation).** Both businesses have `faq`, `services`, `fees`,
`appointment-policy` and `emergency`, with confusable values: consultation INR 600 vs INR 500,
free cancellation up to 24 vs 12 hours, a late-cancellation fee of INR 200 vs INR 150, a free
follow-up within 7 vs 5 days, different opening hours. An answer for one business that quotes the
other's number is a leak or a retrieval bug, and the eval can see it.

**Staff-only and non-approved facts are distinctive** (a staff discount code, refund approval
limits, draft year-end hours, last year's prices): every amount in INR, number of days, hours or
km, and opening-time range in a draft, retired or staff-only document appears in no approved
public document of either business (`test_sample_corpus.py` checks it). So an answer that
contains one shows that a document that must never answer did.

## Changing a document

A document is identified by `(business_id, source_id, version)`, and eval questions point into it
by that key plus a quoted anchor. So never edit a document in place once a dataset version is
frozen against it (the freeze pins every file's SHA-256, and a hash of each business's manifest:
flipping a status or an audience counts as a change too): add a new version (a new file and a
new manifest entry), retire or remove the old one, and freeze a new dataset version. To rebuild a PDF
from its `.pdf.txt`, run `UPDATE_SAMPLE_PDF=1 uv run pytest tests/evalset/test_sample_corpus.py`
in `apps/api` and review the diff.
