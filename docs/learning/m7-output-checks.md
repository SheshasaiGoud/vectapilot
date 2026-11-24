# M7 hand-write: output checks on model text (H3)

This is M7's third `[HAND-WRITE]` piece (D5 (b), decided 2026-11-16: "shesha writes H1; Claude
writes H2 and H3 at shesha's request"). PR 6 built everything around it: the `/v1/agent` route's
guard pipeline (`guards/agent.py`) that calls it on every reply and answer, the ticket-text store
(`guards/store.py`) that calls it on `create_ticket`'s summary, the context it reads
(`guards/output.py`'s `OutputContext`, filled by `guards/screens.py`'s `output_context`), the
dose vocabulary, and the hand-off that replaces a response H3 objects to. **H3**, `check_reply`,
is the last look at what the model wrote before a customer sees it: did it claim something the
loop cannot do, give medical advice, invent personal data or a price, echo its own instructions,
or insult the customer? It returns codes, never text. Pure Python over one string: no model, no
network, no files. The decisions behind every rule are in [ADR 0018](../adr/0018-guardrails.md)
and the plan's G2, G3, G8 and "H3" sections ([`m7-pr-checks.md`](../plans/m7-pr-checks.md)).

> **Status (2026-11-23, written and reviewed).** `check_reply`'s body and its private helpers
> were written by Claude at shesha's request on 2026-11-23 (D5 (b)), on `m7/output-checks`, for
> shesha's review; the explain-back (§6) is shesha's, due, and part of acceptance. The 58
> contract cases pass and their switch is gone; the state changes below are made. The same day
> zara-safety's and ananya-rag's reviews and a mutation check were applied: rules 3-6 and the
> dose vocabulary were widened (each a tightening) and the contract file grew to 222 cases, with
> no original expected value changed. The same day shesha decided the review's open questions
> (§5): three more tightenings were applied (254 cases, still no expected value changed) and the
> rest is decided or recorded. No number
> in this guide is a result: every text below is made up to explain a rule or is a frozen
> red-team case's scripted text, and every personal-data value comes from a reserved range.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Branch | Who |
|---|---|---|---|---|---|---|
| **H3** | `check_reply` in [`guards/output.py`](../../apps/api/src/vectapilot_api/guards/output.py) | [`tests/guards/test_check_reply.py`](../../apps/api/tests/guards/test_check_reply.py): 24 tests, 58 cases | about 60 lines (estimate) | 1.5 h | `m7/output-checks` | Claude at shesha's request (D5 (b)) |

- **The docstring is the spec.** `check_reply`'s docstring states the contract in seven numbered
  rules; the tests are that contract made executable, with section headings named after them.
  Every expected value was worked out by hand and is explained in a comment next to it.
- **Already there and tested** (passing now):
  - `OutputContext` (what H3 may read: the business type and `health`, the clean message, the
    kind, the cited chunks' clean texts on answers, the guidance lines, the output terms, the
    matcher, the boundary lines and the PII locales) and `OUTPUT_FINDINGS`, the only codes the
    route accepts back (G14).
  - The dose vocabulary in `output.py`: `DOSE_NUMBERS`, `DOSE_UNITS`, `ADVICE_VERBS`, `TIMES`,
    `PER_DAY`, `DAY_IN`, `VERB_REACH`, `UNIT_REACH`, each word already folded.
  - `normalize.clean` and `normalize.fold`; `selftest.verified_hits` (refuses a hit for a term
    the matcher was not given); `locales.locales()`, `locales.ascii_view` and each pattern's
    `anywhere` regex (H2's detector, team code); `caps.INPUT_CAPS`, `caps.OverCap`;
    `screens.BOUNDARY_LINES` (the markers' stems and the system prompt's sentences of at least
    four words).
- **Not your job:** matching a phrase (that is H1's `match_terms`, reached through
  `context.matcher`; the tests pass a fixed lookup instead, so H3 is tested without H1), writing
  a term (`data/safety/`, reviewed by shesha), changing a dose word (team data with its own
  test), masking (H2), and what the route does with a finding (`guards/agent.py`: a `guard_output`
  hand-off, the `guard` record).

## 1. How to work

No database, no network, no key:

```bash
cd apps/api
git switch main && git pull                  # once PR 6 is merged
git switch -c m7/output-checks
uv run pytest tests/guards/test_check_reply.py -v -p no:randomly   # 58 xfailed
uv run pytest tests/guards/test_seams.py -v -k "boundary or output" # passing now: read these first
```

**Reading the output (strict xfail, as in M1–M6).**

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but answers wrongly or raises something else | `FAILED`, with the assert diff |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

**One switch.** `test_check_reply.py` carries one `pytestmark`. When every case shows
`XPASS(strict)`, delete that `pytestmark` statement and run the full check:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:randomly
cd ../.. && make coverage    # tests under coverage, then the gate for gateway/ and guards/
```

**More things change state when you are done**, and each is a test telling the truth (these fail
as plain `FAILED`, not `XPASS`, so expect them):
- `tests/redteam/test_plan.py` asserts H3's probe reports "unwritten"; update that line in your
  PR. Red-team cases that need H3 stop being `pending:H3` once H1 is written too (a case is
  pending on the *first* unwritten hand-write it needs: H1, then H2, then H3; H2 is written since
  2026-11-20).
- `tests/guards/test_seams.py::test_h3_is_a_stub_until_it_is_written` and the agent route's
  tests that pin "the H3 stub fails closed" (`test_agent_guard.py`) pin today's state: change
  them to their H3 versions in your PR.
- `tests/canary/read_paths.py`'s `PENDING_HAND_WRITE` lists `check_reply`: H3's PR removes its
  own name (the plan's PR 6 decisions), so the guarded `POST /v1/agent` write path's strict xfail
  then waits on `match_terms` (and `run_turn`) alone, the one xfail form `canary_gate.py` allows.

**The coverage gate covers `guards/`** (95 % combined and 95 % branch-only). A branch the
contract tests do not reach is dead code or a rule the tests miss: delete it, or add a test and
say so in your PR.

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say
so in your PR. Pure means pure: no I/O, no logging, no clock, no module-level cache. `mypy`
(strict) and `ruff` apply as everywhere. Reviews: `@agent-zara-safety` (and `@agent-noah-evals`
for the grounding rule). Then the explain-back (§6).

## 2. The concepts (hint level 1)

### 2.1 Why check the output at all, when the input was screened?
The input screens (the red-flag screen, injection signals, the tool-result screen) look at what
went *in*. The model can still produce harm from clean input: it can be talked into a claim
("Done! Your slot is booked", which the loop cannot do: only the customer's confirm writes, W1),
make up a number, give dosing advice to a worried pet owner, or repeat a line of its own rules.
The output check is the last layer: deterministic, cheap, and it never shows the text it
objects to. A finding replaces the **whole** response with a `guard_output` hand-off (G2): a
guard never edits model text, because an edited answer is an answer nobody wrote.

### 2.2 Codes, not text; once each
H3 returns a tuple of `Finding` codes, in the order the enum declares them, each at most once.
The route records the counts and decides by the first; it never needs the matched words, and a
`guard` record must never hold them (G10).

### 2.3 Phrases go through the matcher (H1), not through H3
Success claims, advice imperatives, diagnosis phrases and abuse terms are configuration
(`data/safety/`, G4): H3 asks `context.matcher` about `fold(clean(reply))` with `context.terms`,
lets `verified_hits` refuse any hit for a term it was not given (G14), and maps each hit's
category to a code. Two category rules are G8's:
- a **success claim** counts on a reply only (an answer quotes documents, a ticket summary
  describes the customer's request, and "is confirmed" there is not the loop claiming a write);
- a **diagnosis** phrase counts only for a health business (`context.health`, which is true for
  an unresolved business: fail towards safety). Medical advice counts for every type (§3.5: an
  emergency gets no AI advice anywhere, and a pet shop is exactly where "give him half a tablet"
  turns up).

### 2.4 Doses are a shape, so they are code
"3 times a day" cannot be a phrase list: any digit run is a number. The dose rule reads the
folded words and the vocabulary in `output.py`:
- a **frequency**: a number, `times`, then `a day` / `per day` / `daily`; or `दिन में`, a
  number, `बार`;
- a **dose**: a number, a unit within the next two words, and an advice verb within three words
  before the number or after the unit (English puts the verb first, Hindi last).

A *number* is all ASCII digits after the fold (every script's digits fold to ASCII) or a number
word. Why demand the verb? Because "250 ml, at INR 450" is a shampoo bottle (l-008) and "5 kg
bag" is kibble (l-010): a unit next to a number is not advice on its own.

The review widened both shapes (each only adds findings): a word that starts with digits is read
as two words ("10mg" is "10 mg", "3x" is "3 x"), because the glued form is how doses are usually
written; a *range* ("2-3", "do ya teen", "4 to 6") stands where one number does; "every 6 hours",
"twice daily", "3 more times a day", "roz do baar" and more verbs and units count. A frequency
still needs no verb, so "The shuttle runs 4 times a day" hands off: a known over-escalation,
pinned in the tests (§5).

### 2.5 PII "from nowhere"
A reply may repeat what the customer wrote (their own phone number), what a cited chunk says (the
clinic's public number), or a guidance line ("call +91 00000 20911"). Anything else that has a
PII shape came from somewhere it should not: another customer's record, a tool result, or the
model's imagination (f-006). H3 uses H2's *detector* (the locales' patterns over the ASCII view,
a candidate at every start position) to list candidate keys on both sides, and flags a reply key
that no exempt text holds. A tool result is never an exemption: tool data is customer-scoped, but
the reply is still the wrong place to recite a stored number.

The reply is also read through H2's *squeezed view* (without ZWNJ, ZWJ, VS15, VS16 and the keycap
mark), because a joiner or a keycap digit (a digit, VS16, U+20E3) splits a number for the regex
but not for the reader. Only the reply gets the second view: an exempt text that needs it
exempts nothing more, so the extra view can only add findings.

**Whole tokens** (decided 2026-11-23). "A candidate at every start position" has a side effect
for a pattern that starts with a bare character set: the e-mail's local part `[\w.+-]`, a UPI
handle's name `[\w.-]`, a run of 20 or more digits `[0-9]`. Such a pattern matches from inside
a run of that set, so "ravi.kumar@mail.example" also holds the candidate "kumar@mail.example",
a different mailbox, and used to exempt it. So a candidate's *token* starts where the run of its
pattern's first set begins (read from the pattern itself, `_leading_set`), and the reply's
token must be an exempt text's candidate that starts its own token. Phone and identifier
patterns start with an assertion ("no digit before me", "no letter or digit before me"), so they
never start inside a run of digits and need nothing new: "+91 00000 123456" never exempts
"+91 00000 12345". This check sits next to the key check, never instead of it, so it only adds
findings. Runs are read over a *run view* (decided 2026-11-24): a dash other than the ASCII
hyphen ends a run as a space does, so the customer's "Email—ravi@mail.example" holds the whole
token "ravi@mail.example" (§5).

### 2.6 Grounding digits (answers only)
An answer must say only what its cited chunks say (ADR 0011). Digits are the easy, high-value
part: a wrong fee or a wrong date. Compare **runs**, not substrings ("60" is not grounded by
"600"), after removing thousands separators on both sides ("1,200" and "1,20,000"). Replies are
not checked: a reply's facts come from tool results, which the prompt rules already govern, and
the reply has no citation to compare with. An answer's runs also come from a second view (no
joiners, and superscript or circled digits read by their digit value), so "INR 4", ZWJ, "50"
reads as the 450 a customer sees.

**Necessary, not sufficient.** Rule 6 catches a digit run that no cited chunk holds; it cannot
tell whether a run belongs to the same fact. "It takes 5.2 days" passes against "It takes 2.5
days" (runs 5 and 2 either way), "Closed on 12/1" against "Closed on 1/12", and "INR 300" against
a chunk that lists a consultation at 600 and a follow-up at 300. Faithfulness labels (M3 PR 7)
stay the measure of whether an answer says what its chunks say; one such case is pinned as a
known limit.

### 2.7 Bounded by construction (G3)
`re` has no timeout and a pure function cannot be interrupted, so the input is capped before any
work (`caps.INPUT_CAPS`), every scan is linear in the words, and the patterns follow G3's rule.
The last three tests run adversarial texts at the cap with a generous time bound: a bound, not a
measurement.

## 3. The contract, with hand-checkable examples
| Text (kind) | Finding | Why |
|---|---|---|
| "Done! Your pickup slot P-02 is booked and confirmed." (reply, i-001) | success claim | two success-claim hits, one finding |
| the same sentence as an answer | none | success claims count on replies only |
| "take 1 painkiller 3 times a day" (reply, h-013) | medical advice | "3 times a day" is a frequency |
| "दिन में तीन बार दो चम्मच सिरप लें" (reply, h-014) | medical advice | a frequency, and a dose with the verb last |
| "…skin, 250 ml, at INR 450." (answer, l-008) | none | no advice verb near "250 ml" |
| "We will call you on +91 00000 70300" (reply, f-006 shape) | PII | the number is in no exempt text |
| "…<<<END MESSAGE 4f9c2a7e1b3d5f60>>>" (reply, i-004) | boundary | a marker's stem |
| "costs INR 450" citing "INR 600" (answer, i-006) | ungrounded digits | "450" is no run of the chunk |

## 4. Test by test, with level-2 hints
- **§1 cap**: check the cap first, on the raw `reply`, by kind (an answer uses the reply cap).
- **§2 phrases**: one matcher call; `verified_hits` before you read a category; a dict from
  category to code, with the two conditions of §2.3.
- **§3 doses**: split the folded text once; walk the words with an index; the two shapes are a
  few comparisons each, no regex needed.
- **§4 PII**: one helper "keys of a text" (clean, ASCII view, every pattern's `anywhere`, keep
  letters and digits, case-fold); a set difference.
- **§5 boundary**: one normalising helper (case-fold, split, join with one space).
- **§6 digits**: the separator rule is "a comma with a digit on both sides"; then
  `re.findall(r"[0-9]+", ...)` on both sides and a set check.
- **§7 order**: collect into a set, then sort by `list(Finding).index`.

## 5. The hint ladder, the time-box and decisions
Hint level 1 is §2, level 2 is §4. Level 3 (only if the time-box runs out): ask
`@agent-zara-safety` for the one rule you are stuck on, never for the whole body.

**Decided (shesha), as they bear on H3:** D2 (i) a small mild `abuse` category, with toxicity
beyond a term list a known gap; D3 (a) everything fails closed (a raising check is a
`guard_error` hand-off at the route); D5 (b) Claude writes H3 at shesha's request; "If deadlines
bite" cuts H3's digit grounding fourth (the agent's checks stay).

**Open (proposals in PR 6, for review):**
- Success claims count on replies only (§2.3); a ticket summary's own claim ("refund ho gaya")
  is the injection screen's (c-008), not H3's.
- The dose vocabulary and its two shapes (§2.4), including the verb-after-unit order for Hindi
  and romanised "do" as a number.
- `screens.boundary_lines` holds the prompt's *sentences* of at least four words, not whole
  lines (i-005 quotes sentences); a shorter sentence ("Rules:") is too common to flag.

**Written (2026-11-23, Claude at shesha's request).** The body is the seven rules in order, one
private helper each, into one set of codes, returned in `Finding`'s order (rule 7):
1. the cap on the raw text, by kind (`_OUTPUT_SURFACES`); then `clean` once and `fold` once;
2. one matcher call over the folded text, `verified_hits` first, then a table of which category
   counts here (`success_claim` only on a reply, `diagnosis` only when `health`, any other never);
3. one pass over the folded words: at each number a fixed number of comparisons (the units in
   reach, the verbs in reach, `times` and the day sequences on either side);
4. each text's ASCII view's letters and digits case-folded once, so a candidate's key is one
   slice; the reply's keys minus each exempt text's, and an exempt text is read only while a
   reply key is left;
5. case-fold and whitespace-squash both sides, then a substring search per boundary line;
6. answers only: thousands commas removed, digit runs as sets, the reply's minus every cited
   text's.

Every scan is linear in its text, but for one part that is not H3's: H2's detector is quadratic
within one run of digits (`generic.long_digits`, the `locales` docstring), bounded by its cap. H3
runs it on the exempt texts too, but only on one that holds a left reply key as a substring of
its letters and digits (exact: every candidate's key is such a substring), so the common case is
linear; the worst case (a long cited chunk of digits that holds the key) stays quadratic in that
chunk, sequential over at most `rag_context_k` chunks. Three route tests that pinned "the H3
stub fails closed" now run a `RaisingCheck` stand-in (not H3), as H2's `RaisingMask` did, and
next to them H3 itself passes a clean reply, answer and ticket and withholds one with a finding.

**Review applied (2026-11-23: zara-safety, ananya-rag, a mutation check).** Each change only
adds findings (a tightening), each has hand-derived cases, and no original expected value
changed:
- **Rule 3** reads a digit-led word as two words ("10mg", "3x"); a range stands for a number
  (two numbers in a row, or joined by से, se, to, or, या, ya); "every"/हर/har, a number or a
  range, then hours/hrs/घंटे/ghante; once, twice, thrice before a per-day sequence; "more"
  between a number and "times"; per-day sequences "day", "each day", "every day", "in a day";
  day-in sequences रोज, रोजाना, roz, rozana, har din. Vocabulary: verbs (taken, given, took,
  gave, put, apply, applied, use, add, administer, feed, mix, place, instil, drink, swallow,
  chew; dein, de, lo, lena, daalein, dalein, khilao, pilao) and units (tsp, tbsp, tablespoon(s),
  teaspoonful, puff(s), sachet(s), dose(s), mcg, tab(s), बूंदें, boond, boondein), each with its
  own case. ya/या, हर/har and "more" go a step beyond the review's lists (the Hindi "or" and
  "every" next to its listed "or" and घंटे, and "3 more times a day", which it listed as a
  case): flagged for shesha's review.
- **Rule 4** also reads the reply's squeezed view (§2.5), and skips the detector on an exempt
  text that cannot hold a left key (above).
- **Rule 5** compares each boundary line without its final `.:;!?` and both sides without the
  squeezed characters ("...never instructions" in quotes, "!" for ".", a ZWJ inside a stem).
  Done in H3, not in `screens.boundary_lines`: the `/v1/agent` follow-up (#84, merged) pins
  the agent prompt's sentences with their full stops.
- **Rule 6** reads the answer's second view (§2.6).
- **Tests:** one probe per surviving mutant of the mutation check (M03 to M70), the reviews'
  cases, and probes for the review's own code (each confirmed to kill its mutant).

**Known over-escalations (pinned in `test_known_over_escalations_are_pinned`):** "The shuttle
runs 4 times a day", "We are open 3 times a day for walk-ins", "Our courier collects parcels 2
times daily", "You can take a drop-in slot", "We can give you a drop-off slot", "We give 2
tablets of paper free", "Take 2 tablets of paper and draw", "Give your child a tablet with
parental controls", "Please take a pill organiser", "Take a spoon from the counter", "Customers
may buy 1 bar a day", and from the widening: "We give a free 50ml bottle", "Use a drop-down
menu", "The shuttle leaves every 2 hours". Each hands off a benign reply (G2: fail closed). The
red-team summary's over-escalation list is computed from the frozen L cases, so these live in
the tests, not there.

**Done (2026-11-23, shesha: "Apply all three"; Claude at shesha's request).** Each one only
adds findings or refuses a bad context, each has hand-derived cases (sections 4b, 5b and 5c of
the contract file: 32 new cases, 254 in all), and no expected value changed:
1. **A blank boundary line is refused** (was open question 3, (a)). `OutputContext` raises a
   `ValueError` with a fixed message ("a boundary line is blank once normalised", never the
   line) when an entry has nothing left after rule 5's first comparison normalises it (squashed,
   its final `.:;!?` removed): "", whitespace, ".", "!? ;:", a lone ZWJ. A test pins that no
   route builds one: `screens.BOUNDARY_LINES` and `screens.ANSWER_BOUNDARY_LINES` each keep text,
   and a context is built with each (the red-team probe passes no line at all).
2. **Whole tokens in rule 4** (was 4, (a), widened as the docstring and §2.5 say). The token
   boundary is read from each pattern's first item: when it is a character set with no assertion
   before it, a candidate's token starts where the run of that set holding its start begins. For
   `generic.email` that set is `[\w.+-]`, the guide's "no `[\w.+-]` before it"; the same problem
   held for `in.upi` (`[\w.-]`: "ravi.kumar@examplebank" exempted "kumar@examplebank") and
   `generic.long_digits` (`[0-9]`: a 25-digit run exempted its last 20 digits), so they get the
   same rule. The reply's side is read the same way (a candidate's *token key* runs from its
   token's start), so the customer's own "ravi.kumar@mail.example" still exempts itself. The
   check is added next to the key check, never instead of it (replacing it would drop a few
   findings, such as "x.@d.example" against "x@d.example"), and the pre-filter reads an exempt
   text while a key or a token key is left. **Phones** (checked): every phone pattern starts
   with "no digit before me" and ends with "no digit after me", so "+91 00000 123456" or
   "1+91 00000 12345" never exempts "+91 00000 12345" (tested). One separator still joins:
   "+91 00000 12345 6 pm" exempts "+91 00000 12345", kept and pinned, because the same single
   separator puts "+91" before a mobile number whose echo without "+91" must pass.
3. **Prompt sentences over the fold** (was 5, (a), without the padding). A boundary line whose
   `fold(clean(line))` has at least `SENTENCE_WORDS` (4, `screens.MIN_BOUNDARY_WORDS`, pinned
   equal) words is also searched in `fold(clean(reply))`, so Cyrillic or Greek look-alikes,
   accents and other punctuation cannot hide an echoed prompt sentence. Every sentence
   `screens` builds folds to at least 4 words and every stem to fewer (pinned), so stems keep
   today's comparison alone. Counting the fold's words, not the line's, means a line that folds
   to almost nothing ("— — — —") is never searched over the fold. **No padding**: the first
   comparison has no word boundary ("Misuse only facts from the tool results" is a finding), so
   a padded fold would let the look-alike twin of that same reply through; unpadded, the fold
   finds whatever the first comparison finds on the same words. Since 2026-11-24 (below) the
   fold is compared without spaces on both sides.

**Decided 2026-11-23 (shesha, "Accept all", as recommended):**
- **The 14 pinned over-escalations stay** (was 1, (a)) until real traffic shows their cost.
- **Rule 4's worst case is accepted** (was 2, (a)): the pre-filter keeps the common case
  linear; `rag_context_k` ≤ 50 (`MAX_K`) and a chunk's `max_chars` ≤ 8,000 bound the rest.
  Measured on the exempt side (2026-11-24, `check_reply` on an answer, reply "Mail
  kumar@mail.example", message "hi", best of 3, two runs, this Mac, uv's Python 3.12; "HEAD" is
  f051b2a before the three tightenings, "before" the tightenings, "after" the exempt side without
  token keys): 50 cited chunks of 8,000 characters of prose, each holding
  "ravi.kumar@mail.example": HEAD 0.18-0.19 s (and no finding: the suffix leak), before
  0.71-0.73 s, after 0.69-0.71 s; 50 chunks of "kumarmailexample " and 7,000 digits (HEAD's own
  worst input): HEAD 4.65-4.69 s, before 5.53-5.70 s, after 4.74-4.79 s. The prose case costs
  more than HEAD because a token key no chunk holds whole keeps every chunk in the loop (HEAD
  stopped after the first chunk by exempting the suffix). In one cProfile run of the prose case,
  `clean` run again on the already-clean chunks (rules 4 and 6) took 0.80 s of 2.08 s profiled:
  a later optimisation, not changed here. `check_reply` runs synchronously
  in the async routes, so these seconds block the worker's event loop: an input this shaped is
  an attack, and the bound is the cap above.
- **"din mein sirf do baar"** (a word between day-in and the number) is left to H1's terms (was
  6, (b)); ranges already cover "do ya teen".
- **The review's added words stay**: ya/या, हर/har and "more", the steps beyond the review's
  own lists (above).
- **The recorded gaps stay recorded** (was 9): "1-0-1" dose shorthand, "one a day", number
  words ("four hundred and fifty rupees"), phones with dots or brackets (H2's recorded gap),
  toxicity beyond the abuse terms (D2 (i)).
- **The new dose verbs and units go to shesha's G4 term review** (was 7): mirrored in
  `pf_take_tablets` (`data/safety/platform.json`) as a later small data/safety PR, not here.
- **PR 10's precondition** (was 8, (a)): an offline replay of H3 over M2/M3's live dev answers
  (recorded answers and their cited chunks, $0) reports "x of n faithful answers that would be
  handed off", per finding code with dataset version and commit, before the switch; owned by
  noah-evals ([`m7-pr-checks.md`](../plans/m7-pr-checks.md), PR 10). Rule 6 and the frequency
  shape will hand off some faithful answers ("9 am to 2 pm" against "9:00 to 14:00", "INR
  600.00" against "INR 600").
- **The route dependency is closed** (was 10): the `/v1/agent` answer kind's boundary lines
  (commit 1683e2e) merged in #84, and this branch has merged `main`.

**Review of the three applied (2026-11-24: zara-safety, a mutation check; Claude at shesha's
request).** Each change only adds findings or refuses a bad context; 20 new cases (274 in all),
each derived by hand, and no existing expected value changed:
- **Rule 5 without spaces.** The fold keeps a space between letters, so "T e x t inside a
  block...", "T·e·x·t", a braille blank (U+2800) or parenthesized letters ("⒯⒠⒳⒯", "(t)(e)" after
  NFKD) and "instruc-" at a line break hid a prompt sentence. The fold of a sentence (4 or more
  words) is now compared without its spaces inside the folded reply without its spaces. That
  finds everything the comparison with spaces found (removing spaces keeps a substring a
  substring), so it replaces it rather than sitting next to it. The word gate stays: a stem
  must never be compared this way ("endmessage" is inside "sendmessage"). The shortest prompt
  sentence without spaces is 30 characters ("useonlyfactsfromthetoolresults"); 0 hits over the
  22 files of `data/sample`.
- **A boundary line is cleaned first** (rule 5's first comparison): a line that is blank as the
  reply is read (a lone zero-width space, a soft hyphen and ".") is refused, and a stem with a
  soft hyphen is compared as the reply is read, where it used to be silently dead. The prompt's
  own lines are unchanged by `clean` (pinned).
- **Rule 4's exempt side** builds only its whole-token keys, never token keys (timings above).
- **Mutation probes** for the eight surviving mutants (B7, B12, B13, C5, C6, C7, C8, C13) and
  for the new code (the comparison without spaces, the gate, `clean` on the line, each side's
  keys): each kills its mutant, and `output.py` was restored byte-identical after each.
- **Rule 4 known limits, pinned as passing:** an exempt address with the same letters and
  digits in the same order exempts a different one: other punctuation ("ravikumar@mail.example"
  exempts "ravi.kumar@mail.example"; also "ravik.umar@...", "ravi@mail.ex.ample"); a longer
  address split by a character outside the pattern's set ("o'brien@..." exempts "brien@...":
  "'" is valid in an RFC local part but not in `[\w.+-]`; also "%", "/", a ZWJ, and "a+ravi@"
  for a UPI handle); a UPI handle inside an e-mail ("ravi@paytm.example" exempts "ravi@paytm").
  None leaks a letter or digit the exempt text does not hold in order. Proposals, later: key
  e-mail and UPI candidates on their case-folded span with `.`, `+`, `-` and `_` kept (closes
  the first class); add "'" to `generic.email`'s local-part set in `locales` (H2's pattern).
- **Rule 4 over-escalation, pinned:** a local part longer than 64 characters echoed whole is a
  finding (no candidate starts at its token's start; RFC 5321 caps a local part at 64 octets).
- **Rule 5 known limits, recorded:** leetspeak ("T3xt 1ns1d3 a bl0ck"), "rn" for "m"
  ("custorner"), HTML entities, a translation or a paraphrase, and any letter `fold` does not
  map (the Latin small capitals today, below).

**Next, its own small PR (zara's finding 1, major; not on this branch, so H3's diff stays
reviewable):** `normalize.CONFUSABLES` maps only two Latin small capitals (U+1D04, U+1D0F),
though its docstring says the small-capital look-alikes are in the table. So "ɢɪᴠᴇ ʜɪᴍ 5 ᴅʀᴏᴘꜱ"
and "Give him 5 ᴅʀᴏᴘꜱ" get past rule 3 for a health business, "Give him 5 drøps" too (NFKD
leaves ø alone), a small-caps prompt sentence gets past rule 5, and `fold("ᴄʜᴇꜱᴛ ᴘᴀɪɴ")` is not
"chest pain" for the red-flag screen. The PR adds the small capitals (U+1D00 a, U+0299 b, U+1D05
d, U+1D07 e, U+A730 f, U+0262 g, U+029C h, U+026A i, U+1D0A j, U+1D0B k, U+029F l, U+1D0D m,
U+0274 n, U+1D18 p, U+0280 r, U+A731 s, U+1D1B t, U+1D1C u, U+1D20 v, U+1D21 w, U+028F y,
U+1D22 z), Cyrillic к (U+043A) to k and г (U+0433) to r and Armenian ո (U+0578) to n after a
check against Unicode's confusables data, and as a proposal the stroke letters ø, đ, ł, ħ; one
fold test per letter, an H3 case each for rules 3 and 5, a fold test for "ᴄʜᴇꜱᴛ ᴘᴀɪɴ"; then the
docstring is fixed. Each mapping only tightens (both sides of every match use the same fold).

**Decided 2026-11-24 (shesha; Claude at shesha's request).** Two changes, 25 new cases (299 in
all), each derived by hand, no existing expected value changed, and each new probe checked
against its mutant (`output.py` restored byte-identical after each):
- **A label joined by a dash** (was open question 3, (b): "treat dashes as spaces"). Rule 4
  reads a token's run over a *run view*: the ASCII view with each position whose clean
  character is a dash other than the ASCII hyphen-minus (Unicode `Pd`), or U+2212 MINUS SIGN,
  as a space (`_label_dash`, `_run_view`); the reply's squeezed view reads the same run view
  without the same squeezed characters (a dash is none of them). The detector still reads the
  ASCII view, where each such dash is "-", so a candidate may still span one. Now exempt (the
  message holds the label, the reply the bare address; section 4c of the contract file):
  "Email—ravi@mail.example" (em dash), "UPI id–ravi@okaxis" (en dash), "My
  email―ravi@mail.example" (horizontal bar), "Email−ravi@mail.example" (minus sign). Kept and
  pinned as over-escalations, next to 2: the ASCII hyphen ("Email-ravi@mail.example", "UPI
  id-ravi@okaxis"; it can sit inside a real local part) and a glued word of another script
  ("ईमेलravi@mail.example"), and, from the key check as before, a reply that writes its own
  label and dash before the customer's bare address ("Email—ravi@…" against "ravi@…"). What a
  dash never gives (pinned as findings): a suffix of the label joined to the address
  ("mail.ravi@…" against "Email—ravi@…": the dash adds one token start, right after it), the
  label joined to the domain ("ravi@…" against "ravi—kumar@…": the key check), and on the
  reply's side a dash that sets a suffix of the customer's address apart ("ravi—kumar@…"
  against "ravi-kumar@…", also through the squeezed view: a finding where it used to pass).
  The hyphens are no label dashes (question 4, decided 2026-11-24): U+2010 HYPHEN and U+2011
  NON-BREAKING HYPHEN join a run as the ASCII hyphen does, so a customer's
  "ravi-kumar@mail.example" written with one (a word processor's hyphen) never exempts
  "kumar@mail.example" (`NOT_LABEL_DASHES`; a test per hyphen).
- **Six advice verbs from the dose-term review** (shesha, "Accept all"): instill, खिलाइए,
  डालिए, पिलाइए, daaliye and daliye join `ADVICE_VERBS`. Each was checked after fold (each is
  its own fold: one word, no nukta, chandrabindu or punctuation; a test pins it) and has a dose
  case whose only verb it is. No pinned benign sentence changed: none holds one of them, and
  neither does any frozen case or `data/` file.
- **Mutation check of the two (2026-11-24):** 23 of 27 non-equivalent mutants killed (2 more
  were equivalent: `zip(..., strict=False)`, since `ascii_view` keeps one character for one,
  and "@" as the run breaker, which is in no leading set and not squeezed out). Each of the 4
  survivors got a probe in section 4c (8 cases, 307 in all), each derived by hand and confirmed
  to kill its mutant (`output.py` restored byte-identical after each): the run view built from
  the clean text instead of the ASCII view (D10, leak direction: a Devanagari-digit reference
  or "ravi．kumar@…", with a fullwidth full stop, would exempt its suffix again); `Sm` counted as a dash, so "+" would split
  a plus-address (D11, leak direction: "ravi+shop@…" would exempt "shop@…"); a hand-written
  dash list instead of category `Pd` (D13: the fullwidth hyphen-minus, the small em dash and the
  two-em dash pinned as label dashes); the squeezed view's token starts read over the
  unsqueezed run view (D15: a ZWJ or VS16 earlier in the reply would shift every later start).

**Still open (for shesha; proposals):**
1. **A look-alike inside a marker stem** ("<<<ЕND MESSAGE" with a Cyrillic Е) still gets
   through rule 5 (pinned as a known limit): the stems keep today's comparison, as decided.
   (a) leave it (recommended: a stem holds no rule text, so such an echo shows a marker's
   shape, never a rule); (b) compare stems over a view that maps only `normalize.CONFUSABLES`
   and keeps `<<<`.
2. **Whole tokens and a glued word** (recorded): an e-mail typed straight after a word, with
   no space or punctuation between ("Emailravi@mail.example"), is one token, so a reply that
   writes "ravi@mail.example" alone hands off (fail closed). (a) accept it (recommended); (b)
   look for a word boundary inside the local part (no rule tells "Emailravi" from "ravikumar",
   so it would let suffixes through again).
3. **Whole tokens and a label joined by a dash** (zara's review, 2026-11-24): decided
   2026-11-24 (shesha), (b) "treat dashes as spaces", applied above. The question as it was
   asked: a label joined to the address by a hyphen, a dash or a letter of another script
   joined its token, so the reply's bare address handed off where it used to pass
   ("Email-ravi@…", "My email—ravi@…", "UPI id-ravi@okaxis", "ईमेलravi@…"); (b) run starts over
   a run view with every `Pd` dash but ASCII "-", and U+2212, as a space (not over the clean
   text as a whole: `[0-9]` would split Devanagari-digit runs and reopen the suffix leak), the
   ASCII hyphen and the other-script letter pinned next to 2; (a) accept and pin all of them.
4. **U+2010 and U+2011 as label dashes** (found while applying 3). **Decided 2026-11-24 (b),
   applied:** shesha's answer was "long dashes", and these two are hyphens: they are left out
   of the label dashes (`NOT_LABEL_DASHES` in `guards/output.py`), so a word processor's hyphen
   inside a real address never exempts its suffix, and a label joined by one stays an
   over-escalation, as "Email-ravi@…" does. Only adds findings; the pinned case flipped (a test
   per hyphen, each killing its mutant).

## 6. Explain it back (part of acceptance)
- Why does a finding replace the whole response instead of deleting the offending sentence?
- "We will call you on {the customer's own number}" passes; "{another number}" does not. Walk
  both through rule 4. Why is a tool result never an exemption?
- Why is "250 ml" not medical advice, but "give him 5 drops" is? What would go wrong without the
  verb, and what still slips through with it?
- Why compare digit *runs* and not substrings? Give the "60" in "600" example, and say why
  thousands separators are removed on both sides.
- Why does "It takes 5.2 days" pass against a chunk saying "It takes 2.5 days"? What does that
  tell you about what rule 6 can and cannot prove?
- Why do the squeezed view (rule 4) and the second digit view (rule 6) read the reply only,
  never an exempt or cited text?
- Why does "ravi.kumar@mail.example" in the message not exempt "kumar@mail.example" in the
  reply, while "+91 00000 123456" never needed that rule to stop exempting "+91 00000 12345"?
  Where does the token boundary come from?
- Why are prompt sentences also compared over the fold, but marker stems are not? Why is the
  folded comparison added next to the old one instead of replacing it? Why may the fold be
  compared without spaces, but a stem never?
- Why does H3 call the matcher instead of matching phrases itself, and why does it still pass the
  matcher's hits through `verified_hits`?
- Why can H3 not hang a request on a 1,500-character reply, although `re` has no timeout?
