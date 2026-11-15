# M6 hand-write: the extraction metrics (H2)

This is M6's second `[HAND-WRITE]` piece (D7 (b), decided 2026-11-11: "shesha writes H1
`route_fields`; Claude writes H2 at shesha's request"). M6 PR 4 built the extraction harness
(`vectapilot_api/extracteval/`): it runs every document of a split through the extraction core
in-process, compares each returned value with the gold using the eval's own comparison
(`compare.py`), and writes one result line per document with every field's status, level, reasons
and `correct`. **H2** is the five small functions that turn those fields into the numbers M6 is
judged by: field accuracy, silent errors, review rate, review precision and straight-through
documents. Pure Python over a sequence of `ScoredField`s: no model, no network, no files.

The rules behind every function are the plan's "Scoring rules"
([`m6-pr-checks.md`](../plans/m6-pr-checks.md)) and the dataset's README
([`evals/datasets/doc_extraction/v0.1/README.md`](../../evals/datasets/doc_extraction/v0.1/README.md)).

> **Status (2026-11-15, M6 PR 4 scaffold).** The five bodies raise `NotImplementedError`; their
> 27 tests are strict xfails with hand-counted fixtures. Claude writes them later, on
> `m6/extraction-metrics`, at shesha's request (D7 (b)); the explain-back (§6) is shesha's. Every
> field below is made up to explain a rule; **no number in this guide is a result.**

## Your task

| | Write the bodies of | So that these pass | Size | Time-box | Branch | Who |
|---|---|---|---|---|---|---|
| **H2** | `field_accuracy`, `silent_errors`, `review_rate`, `review_precision`, `straight_through` in [`extracteval/metrics.py`](../../apps/api/src/vectapilot_api/extracteval/metrics.py) | [`tests/extracteval/test_metrics.py`](../../apps/api/tests/extracteval/test_metrics.py): 27 strict xfails | about 60 lines (estimate) | 1 h (decided) | `m6/extraction-metrics` | Claude at shesha's request (D7 (b)) |

- **The docstrings are the spec.** Each function states its contract; the tests are that
  contract made executable, with section headings named after this guide's sections.
- **Already there and tested** (passing now, [`test_metrics_types.py`](../../apps/api/tests/extracteval/test_metrics_types.py)):
  - `ScoredField`, H2's input, one per (document, field): `document`, `business_id`, `field`,
    `scored`, `document_status`, `status`, `level`, `reasons`, `has_value`, `gold_null`,
    `correct`, and `key` (`<document>/<field>`). A field of a document that was not scored (not
    run, or a harness error) has `scored` False and no status or `correct`.
  - The results: `Accuracy`, `FieldCount`, `DocumentCount` (with its Wilson interval),
    `SilentErrors`, `ReviewPrecision`. Each **checks itself when built**: ids sorted and unique,
    `k <= n`, the interval exactly `wilson_interval(x, n)` or None when n is 0. A slip raises
    `ValueError` the moment you build the result.
  - `ExtractMetrics` and `H2_EXTRACT_METRICS`, the bundle the harness calls.
- **Not your job:** the comparison (`compare.py`), choosing subsets (per business, per level,
  accepted precision, review recall: `summary.py` calls your functions on filtered fields), the
  bootstrap for field-level intervals (`summary.field_interval`), and the "also by id" lists.
- **When the tests pass:** delete `pytestmark` in `test_metrics.py`, run `make check`. Change the
  code, never a fixture: every expected value was counted by hand before any code existed.

## 1. Field accuracy

*Correct of all scored fields, whatever the status.* A field in review counts too: the system
returned a value (or none), and it either is the gold or is not. E3's all-review fields count (the
system's real output for a truncated or failed call: no value, so correct only where the gold is
null). Fields of a document that was not scored are outside n; their documents are listed once in
`not_scored`.

The harness calls it four more ways (team code): per business, **per level** (does `high` really
mean "usually right"? levels are ordinal checks in code, not calibrated probabilities, so the claim
is "high was right k of n times", never "high means 95 %"), over accepted fields (**accepted
precision**: of what nobody will look at, how much is right) and per field.

## 2. Silent errors (target 0; they judge the routing)

A **silent error** is a wrong field nobody will look at:
- an `accepted` field that is not correct, or
- an `absent` field whose gold has a value (the system said "not in the document", and it was).

An accepted value whose gold is null (invented, hidden-only or planted: the page does not visibly
give the field) is silent: "not correct" includes "a value where the gold has none", never only "a
different value". It is the most dangerous kind, because a single hidden copy is not detected in
M6 (ADR 0017), and the tests pin it (`test_an_accepted_value_with_a_null_gold_is_silent`).

A field in `review` is never silent, however wrong: a person sees it. Two counts:
- **fields**, `k of n` over every scored field (proposal: n is all scored fields, so it reads
  next to field accuracy; the plan says "fields `k of n`" without naming n. If shesha prefers n
  = accepted plus absent fields, that is one changed line and one recounted fixture);
- **documents** with at least one, `x of n` scored documents, with Wilson. This is the headline:
  "0 of 12 test documents with a silent error" still allows a rate up to about 24 % (the Wilson
  upper bound, arithmetic: z²/(n + z²) with z = 1.96), and 0 of 6 per business about 39 %. **The
  claim is the count**, never "no silent errors".

## 3. Review rate, and review recall

**Review rate:** fields in `review` of all scored fields: the staff work routing asks for. Low is
good only if silent errors stay at 0; 100 % review is perfectly safe and useless.

**Review recall** (computed by the harness: `review_rate` over the fields that are *not correct*):
of the wrong-or-missing fields, how many did routing send to a person. Its misses are, by
definition, the silent errors: a field that is not correct and not in review is either accepted
(wrong) or absent (gold has a value). The summary checks that identity: if your two functions
ever disagree, summary.md opens with "Inconsistent" and `run` and `summarize` exit 1, so a wrong H2
can never produce a clean headline.

## 4. Review precision

Of the reviewed fields **with a value**, how many were wrong: is review spent on real problems,
or on correct values with a missing label? A reviewed field **without a value** cannot be "wrong
in the value"; it is counted apart, per reason (a field with two reasons counts under both), with
how many had a null gold too: the document really did not give it, so that review was not needed
(a typical cost of E4's "unseen is not absent" on a page with a logo).

## 5. Straight-through documents

A document is straight-through when it is `complete` (no field in review) **and** every field is
correct: what M8 could one day file without a person. "Complete but wrong" is exactly the
dangerous case (it also shows up as a silent error); "needs review but all correct" is the safe,
expensive one. `x of n` scored documents, with Wilson.

## 6. Fixture A, counted by hand

Seven documents, two businesses; d0 and d6 are not scored. The tests' comment table lists every
field; the counts:

| function | result | how |
|---|---|---|
| `field_accuracy` | 9 of 15; not scored d0, d6 | d1 3, d2 1, d3 1, d4 2, d5 2 |
| `silent_errors` | fields 3 of 15; documents 2 of 5 | d2/name, d5/owner accepted wrong; d2/insurer absent with a gold value |
| `review_rate` | 6 of 15 | d2/born, d3 ×3, d4/pet, d4/owner |
| review recall (harness) | 3 of 6; misses = the 3 silent errors | the wrong fields in review: d3/born, d3/name, d4/pet |
| `review_precision` | 1 of 2 wrong; 4 without a value (image_page 1, label_present 1, truncated 3); 2 with a null gold | d2/born right, d4/pet wrong |
| `straight_through` | 1 of 5 (d1) | d5 is complete but has a wrong field |

## 7. Explain it back (part of acceptance)

1. Why is a field in `review` never a silent error, even when its value is wrong? What does
   review recall's miss list always equal, and why?
2. d5 in Fixture A is `complete`. Why is it not straight-through, and which other metric already
   shows its problem?
3. Why are intervals computed over *documents* (Wilson for documents, a bootstrap that resamples
   documents for field rates) and never over fields as if each field were independent?
4. "0 of 12 test documents had a silent error." Read the Wilson interval aloud and say what you
   can and cannot claim from it.
5. Why does the eval compare values with its own `compare.py` instead of reusing the core's
   `signals.canon`? Name one bug that reuse would hide.
