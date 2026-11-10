# M4 hand-write: the router's metrics — confusion matrix, macro-F1, calibration (H2)

This is M4's second `[HAND-WRITE]` piece. The intent harness (M4 PR 4, `intenteval`) routes every
eval row three ways (the classifier alone, the LLM alone, and the hybrid that is H1's
`route_message`) and writes one result line per row: its gold intent, what was decided, the top
probability. These four functions turn those lines into the numbers the router is judged by: a
**confusion matrix**, **macro-F1** over the eight intents, and a **reliability table** with its
**expected calibration error (ECE)**. They are pure Python over small tuples: no model, no
network, no files.

> **Status (2026-11-10).** Written by Claude at shesha's request (D4 (b), decided 2026-11-05) on
> `m4/metrics`, in review: the four bodies are in place, the strict xfail in `test_metrics.py` is
> deleted and its 21 cases pass; the harness's own tests still use stand-ins, plus one summary test
> with the real H2. The explain-back (§6) is shesha's and part of acceptance: read the code against
> §1–§3 first.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Branch |
|---|---|---|---|---|---|
| **H2** | `confusion_matrix`, `macro_f1`, `reliability_bins`, `expected_calibration_error` in [`intenteval/metrics.py`](../../apps/api/src/vectapilot_api/intenteval/metrics.py) | [`tests/intenteval/test_metrics.py`](../../apps/api/tests/intenteval/test_metrics.py): 13 tests, 21 cases | about 40–60 lines (all four, an estimate) | 2 h | `m4/metrics` |

- **The docstrings are the spec**; the tests are that spec made executable, every expected value
  worked out by hand in a comment (fixture A: ten rows, one fail-closed; fixture B: nine
  confidences). Read the docstrings, then the tests, then write.
- **Already there and tested** ([`tests/intenteval/test_metrics_types.py`](../../apps/api/tests/intenteval/test_metrics_types.py)):
  the result types `ConfusionMatrix` (checks its shape: every intent's row, every column, the
  `fail_closed` column last), `ClassScores`, `MacroF1` (eight intents in decision order) and
  `ReliabilityBin` (an empty bin has None, never 0; accuracy is exactly `correct / n`). Build
  them; do not change them.
- **Not your job** (PR 4, `scoring.py` and `summary.py`): choosing rows, safety-escalation recall
  and its misses by id, route-level accuracy, which slices are big enough to show macro-F1, the
  bootstrap over split units, Wilson intervals, the report. Your functions see gold and decided
  intents, or confidences and right/wrong, and nothing else.
- **When done:** delete the `pytestmark` in `test_metrics.py` and run again; every test must
  simply pass. Change code, never a fixture.

## 1. The confusion matrix, with a `fail_closed` column

Rows are the gold intent, columns what was decided; cell (g, d) counts rows of gold g decided d.
Eight rows (the intents, in decision order) and nine columns: the eight intents, then
`fail_closed`. A **fail-closed** row is one where the LLM raised a `GatewayError` and the router
handed the message off with no intent (H1, rule 4). It gets a column, never a row (no message's
true intent is "fail closed"), and it is **wrong for every intent metric**: it is a false negative
of its gold intent and a false positive of nothing. At route level it is scored by its actual
route (a hand-off), which `scoring.py` does, not you.

Why a column and not "drop those rows": dropping them would make a provider outage look like
perfect recall on the rows that remained. Keeping them in n and never in k is the honest count.

## 2. Per-intent scores and macro-F1

For each intent c, from the matrix:

- **TP** = cell(c, c) · **FP** = rows decided c whose gold is another intent · **FN** = rows of
  gold c decided anything else, the `fail_closed` column included.
- **precision** = TP / (TP + FP): of what we called c, how much was c.
- **recall** = TP / (TP + FN): of the real c's, how many we found.
- **F1** = 2·TP / (2·TP + FP + FN), the harmonic mean of the two (equal to 2PR / (P + R), and
  defined even where one of them is 0/0).
- **macro-F1** = the plain mean of the eight F1 values. Always eight, in decision order.

**Why macro-F1 and not accuracy.** Accuracy weights every *row* the same, so the intents with
many rows dominate it. Macro-F1 weights every *intent* the same: missing every `emergency` costs
one eighth of macro-F1 however few emergencies there are. On a support mix where `question` is
common and `emergency` rare, a router can be 90 % accurate and useless on the rows that matter.
In fixture A accuracy is 5 of 10 and macro-F1 is 5/16: three intents have no rows at all and score
0, which is why the report shows macro-F1 only when every intent has at least 5 gold rows and
at least 5 split units (decided 2026-11-09: translations of one message share a unit and are not
independent draws).

**The 0/0 rule.** Any of precision, recall or F1 whose denominator is 0 is `Fraction(0)`: never an
error, never left out of the mean. This is conservative (an intent nobody wrote rows for pulls the
macro down) and matches scikit-learn's `zero_division=0`. It matters in bootstrap resamples, where
a small intent can drop out of a resample; that is one reason the interval is "approximate at
small n". The harness counts those resamples and prints "k of B resamples lacked an intent" next
to each interval, so you can see how much the lower end leans on this rule.

**Exact `Fraction`s.** Counts make ratios; 1/3 stays 1/3. A float would turn the paired difference
of two macro-F1 values into rounding noise, and an exact value can be compared with a target
without a last bit deciding it (the same reason as M2's H2).

## 3. Calibration: the reliability table and ECE

A classifier is **calibrated** when its probabilities mean what they say: among rows where it says
0.8, about 80 % should be right. The router's threshold τ only means "this sure" if that holds,
which is why PR 3 fits a temperature on dev and the report shows raw (T = 1) and calibrated side
by side on test.

**Bins** (`reliability_bins`). Five equal-width bins of the top probability over [0, 1]: bin i
has the exact edges i/5 and (i+1)/5. A confidence p goes to bin `min(floor(Fraction(p) * 5), 4)`,
so 1.0 joins the last bin. **Exactly**, from the float's own value: the float 0.6 is
0.59999999999999997779…, just below 3/5, so it belongs to [2/5, 3/5); a float product,
`int(0.6 * 5)`, rounds up to 3 and would put it one bin higher. Each bin reports n, how many were
right, the mean confidence (`math.fsum` / n, a float: probabilities stay floats) and the accuracy
`Fraction(correct, n)`. An empty bin has None for both, never 0 (0 would read as "always wrong").

**ECE** (`expected_calibration_error`): Σ (n_b / N) · |accuracy_b − mean_confidence_b| over the
non-empty bins; equivalently (1/N) Σ |correct_b − Σ confidence_b|. One number: how far, on average
over rows, confidence is from accuracy. No rows, no ECE (ValueError).

**Why "biased upward at small n".** Each bin's accuracy is a noisy estimate, and the gap is taken
in absolute value, so noise in either direction adds to the ECE: a perfectly calibrated model on
80 rows still shows an ECE above 0. Compare raw against calibrated on the same rows; never read
an ECE as "x % miscalibrated".

## 4. Counts next to rates

Every rate in the report sits next to its count and an interval: `x of n` with a Wilson interval
for proportions (M2's rule), macro-F1 with a percentile bootstrap over split units, ECE with its
n. With about 80 test rows (an estimate), one business's 10 of 10 safety rows has a Wilson lower
bound near 72 %. "No difference detected at n = …" is a result; "as good as" never is.

## 5. TODOs (in this order)

1. `confusion_matrix`: check lengths and types (a plain string is not an `Intent`); start from all
   zeros for every row and column; count; build `ConfusionMatrix`.
2. `macro_f1`: TP, FP, FN per intent from the matrix; the three ratios with the 0/0 rule; the
   mean; build `ClassScores` and `MacroF1`.
3. `reliability_bins`: check inputs; place each confidence exactly; one `ReliabilityBin` per bin,
   empty ones with None.
4. `expected_calibration_error`: N from the bins; refuse 0; the weighted sum as a float.
5. Delete `pytestmark` in `test_metrics.py`; run `uv run pytest tests/intenteval -q`, then
   `make intent-dry-run` on a frozen dataset to see your numbers in a DRY RUN summary (plumbing,
   never a result).

## 6. Explain-back (part of acceptance)

1. A fail-closed row: which cells and which of TP, FP, FN does it change, and why is it not
   simply dropped?
2. Why does the report show macro-F1 only with 5+ gold rows and 5+ split units of every intent,
   and what does the 0/0 rule do to an intent with no rows?
3. Accuracy 90 %, macro-F1 0.55: what kind of router is that, and which number does a clinic care
   about?
4. Where does the float 0.6 go, and why does the exact rule matter for a threshold-sensitive
   report?
5. ECE 0.08 raw, 0.05 calibrated on 80 rows: what can you claim, and what not?
