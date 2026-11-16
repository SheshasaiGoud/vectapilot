# M7 hand-write: the term matcher (H1)

This is M7's `[HAND-WRITE]` piece that shesha writes (decided D5 (b)). PR 1 built everything
around it: the normaliser that cleans and folds text (`normalize.py`), the configuration loader
that reads and folds every term (`config.py`), the self-test that checks each term against its
own obfuscated variants (`selftest.py`, G14) and the types it reads and returns (`types.py`).
**H1**, `match_terms`, is the one function the red-flag screen, the injection screen and the
output-phrase checks all call: given folded text and a set of terms, it says which terms hit and
where. About 55 lines (the plan's estimate, not a target). The decisions behind every rule are
in [ADR 0018](../adr/0018-guardrails.md) and the plan's G1–G5, G14 and "H1" sections
([`m7-pr-checks.md`](../plans/m7-pr-checks.md)).

> **Status (2026-11-16, M7 PR 1 scaffold).** The body raises `NotImplementedError`; its tests are
> strict xfails behind one switch. shesha writes it (D5 (b), decided 2026-11-16). No number in
> this guide is a result: every term, text and window below is made up to explain a rule.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Branch | Who |
|---|---|---|---|---|---|---|
| **H1** | `match_terms` in [`guards/match.py`](../../apps/api/src/vectapilot_api/guards/match.py) | [`tests/guards/test_match.py`](../../apps/api/tests/guards/test_match.py): 23 tests, 58 cases | about 55 lines | 2 h | `m7/term-matching` | shesha (D5) |

- **The docstring is the spec.** `match_terms`'s docstring states the contract in six numbered
  rules (the plan's "H1" section, with one [proposal] made explicit: a term hits at most once);
  the tests are that contract made executable, with section headings named after the rules.
  Every expected value was worked out by hand and is explained in a comment next to it.
- **Already there and tested** (passing now):
  - `PhraseTerm(id, category, words)`: its words are already folded (`config` folds every term
    exactly as it folds text). `.text` joins them with single spaces.
  - `NearTerm(id, category, sets, window)`: at least two word sets, folded; `window` is at least
    the number of sets.
  - `TermSet(terms)`, `TermHit(term_id, category, start)` and `TermCategory` (the two red-flag
    categories, `injection` and the four output categories) in `guards/types.py`.
  - `Folded`: the type of `normalize.fold`'s output. Folded text is lower-case, has no invisible
    characters, no Latin accents, no punctuation or symbols (each became a space) and single
    spaces between words. `selftest.screen_text(text)` is `fold(clean(text))`, what a route
    hands you.
  - `run_selftest` and `verified_hits` (G14): they call your function and refuse to trust it
    (§2.2).
- **Not your job:** folding (done before you are called, on both sides), reading the term files,
  deciding what a hit means (the screens, PR 6–8), masking (H2) and the output checks (H3, which
  will call your function).

## 1. How to work

No database, no network, no key:

```bash
cd apps/api
git switch main && git pull                  # once PR 1 is merged
git switch -c m7/term-matching
uv run pytest tests/guards/test_match.py -v -p no:randomly   # 58 xfailed
uv run pytest tests/guards/test_regex_scripts.py -v          # passing now: read these first (§2.4)
```

**Reading the output (strict xfail, as in M1–M6).**

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but answers wrongly or raises something else | `FAILED`, with the assert diff |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

**One switch.** `test_match.py` carries one `pytestmark` (as M4's `test_decide.py` and M6's
`test_routing.py`). When every case shows `XPASS(strict)`, delete that `pytestmark` statement
and run the full check:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:randomly
cd ../.. && make coverage    # tests under coverage, then the gate for gateway/ and guards/
```

**A wrong H1 also stops `create_app`** (G14): the app runs the self-test on every guard folder
at startup, and a miss or a wrong hit raises `SelftestError`. The tests that start an app with a
guard folder (the log canary, the settings tests, one extraction route test) therefore run the
self-test on a stand-in that knows the answers (`helpers.use_answer_key_selftest`), so your
work in progress cannot turn them red; the one startup test that runs your function is
`test_h1_lets_the_app_start_on_the_fixture_folder`, inside the switch.

No read path in `tests/canary/` runs your function yet (the routes are wired in PR 6–8), so
`PENDING_HAND_WRITE` in [`tests/canary/read_paths.py`](../../apps/api/tests/canary/read_paths.py)
does not list it. If PR 6 has merged before you finish, the switch may have moved there (as M5's
`run_turn` and M6's `route_fields`): remove `"match_terms"` there too.

**The coverage gate covers `guards/`** (PR 1, at the gateway's thresholds: 95 % combined and
95 % branch-only). Your function's branches must be reached by tests; the contract tests reach
the ones the contract needs. A branch they do not reach is either dead code or a rule the tests
miss: delete it, or add a test and say so in your PR.

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say
so in your PR. Pure means pure: no I/O, no logging, no clock, no module-level cache that outlives
a call. `mypy` (strict) and `ruff` apply as everywhere. Commit with your own message, push, open
a PR. Reviews: `@agent-zara-safety` (and `@agent-ananya-rag` for the scripts). Then explain it
back (§6).

## 2. The concepts (hint level 1)

### 2.1 Recall versus precision, and why an emergency screen is tuned for recall
*Recall* is the share of real emergencies the screen catches; *precision* is the share of its
hits that are real emergencies. A missed emergency is a customer with chest pain who gets an
opening-hours answer from a bot: the platform's hard gate is 100 % safety-escalation recall
(`docs/business-context.md` §6). A false hit is a person on the business's staff reading a
message a bot could have answered. The costs are not symmetric, so the screen leans towards
recall: when in doubt, it hits, and a hit only ever *tightens* (G2: an answer becomes a hand-off,
never the other way).

Precision still matters: a screen that hits on "paint" or "Spain" teaches staff to ignore it, and
PR 2's red-team suite counts over-escalation. That is why the contract asks for **whole words**
and recovers the lost recall elsewhere: `\s*` between a phrase's words ("chestpain"), `near`
terms for word order and filler ("my chest feels really tight"), the fold for disguises, and the
term lists for variants ("pains", "painful" are listed as their own terms, PR 4). M4's router is
the semantic layer a phrase list can never be.

### 2.2 Fail closed
A screen that errors must never let the message through unchecked (G3). While your function is
the stub, the self-test reports `matcher_unavailable` and every guarded route hands off; once it
exists, an exception inside it becomes a `guard_error` hand-off at the route. Two checks surround
you, because a guard does not trust the check it guards (G14): at startup every configured term
must hit itself and its variants and miss the fixed negatives ("paint the chest"), or the app does
not start; at run time `verified_hits` refuses any hit for a term or category you were not given.
So a bug in your function shows up as a stopped startup or a hand-off, never as a silent miss
of the kind the self-test can see.

### 2.3 Fold both sides
The screens compare *folded* text with *folded* terms. Folding only the text would break the
moment a term is written with a capital or an accent; folding only the term would let every
disguise through. The five bypasses from zara's review, and what the fold makes of each:

| The customer writes | The fold gives | Then your function must |
|---|---|---|
| `c` + ZWJ + `hest pain` | `chest pain` | hit, as plain text |
| `c` + ZWNJ + `hest pain` | `chest pain` | hit |
| `chest-pain` | `chest pain` | hit |
| `chést pain` | `chest pain` | hit |
| `chest` + zero-width space + `pain` | `chestpain` | hit: `\s*` between the words |

You never fold anything yourself: both sides arrive folded. But you must not undo the fold's work
either (for example by matching on the raw text, or by treating a space and "no space" alike only
in one direction).

### 2.4 Why `\b` needs care in Devanagari and Telugu
`\b` is the edge between a word character (`\w`: a letter, a digit or `_`) and anything else. In
Python's `re`, a vowel sign (matra) or a virama is **not** `\w`: it is a combining mark (`Mn` or
`Mc`), not a letter. [`tests/guards/test_regex_scripts.py`](../../apps/api/tests/guards/test_regex_scripts.py)
pins what that does, and it passes now:

- `\bसीने\b` finds **nothing** in `सीने में दर्द`, although `सीने` is a whole word there: it ends in
  a vowel sign, and between a vowel sign and a space there is no `\b`. The same for Telugu
  `నొప్పి`. That is a missed emergency.
- `\bदर्द\b` **hits** inside `दर्दों`: the vowel sign after the last `द` makes a `\b` in the middle
  of the word. That breaks "never ends inside a word".

So "word character" has to mean what a reader means by it: a letter, a digit, or a mark that
belongs to the letter before it. Python's `re` lets you say "not preceded by" and "not followed
by" a set of characters; `unicodedata.category` tells you which characters are marks. Work it out
for one word in each script on paper before you write code.

### 2.5 Terms are literal text, never regex
A term is data an operator writes in a JSON file and a reviewer reads (PR 4), never a pattern.
`re.escape` turns every character into itself, so `c.t` matches only `c.t`, never `cat`. Two
reasons: a term read as a pattern can mean something its author did not intend (a `.` in "a.m."),
and a pattern can be slow: nested quantifiers backtrack exponentially (G3's rule: no nested or
overlapping quantifiers; synchronous work is bounded by construction, because `re` has no
timeout and `asyncio.timeout` cannot interrupt pure code). If every pattern is escaped words joined
by `\s*`, and the `near` scan passes over the words once, the work is linear in the text by
construction. The fold already turns punctuation into spaces, so a configured term never
carries one; the literal tests pass raw terms on purpose, as defence in depth.

### 2.6 Configuration, not code; and why the lists are not a secret
Which phrases count as an emergency differs per business type and language, and is reviewed by a
person (PR 4; rule 9: nothing business-specific in code). So your function knows no term: it gets
a `TermSet` and returns hits. The lists live in a public repository. That is fine: a screen whose
strength depended on the attacker not knowing the list would be broken by one reading of the
repository (Kerckhoffs's principle, applied to a filter). The defence is in layers instead: the
fold, the self-test, M4's router, the read-only service, W1/W2's write boundary and a human. An
attacker who knows the list can avoid it; an emergency should not be trying to.

## 3. The contract, with hand-checkable examples
Terms below are folded already; texts go through `screen_text` first. "start" is the offset in
the folded text.

| Terms | Text | Hits | Rule |
|---|---|---|---|
| phrase `chest pain` | `I have chest pain` | `chest_pain` at 7 | 2 |
| phrase `chest pain` | `chestpain` | `chest_pain` at 0 | 2 (`\s*`) |
| phrase `pain` | `I will paint the fence` | none | 2 |
| phrase `c.t` (raw) | `cat` | none | 1 |
| phrase `सीने` | `सीने में दर्द` | `seene` at 0 | 2 |
| phrase `दर्द` | `दर्दों` | none | 2 |
| near [chest] + [pain, hurt, hurts, tight, pressure], window 4 | `chest is so tight` | at 0 | 3 (4 words: the edge) |
| the same | `chest chest pain` | at 6 | 4 (the first window to complete, each set's most recent word) |
| the same | `chest is so very tight` | none | 3 (5 words) |
| the same | `tight chest` | at 0 | 3 (any order) |
| `b_chest`, `z_chest_pain`, `a_pain` | `chest pain` | `b_chest` 0, `z_chest_pain` 0, `a_pain` 6 | 4 |
| phrase `chest pain` | `chest pain, then chest pain again` | one hit at 0 | 4 [proposal] |
| anything | empty | none | 5 |
| two terms with id `dup` | anything | `ValueError` | 5 |

## 4. Test by test, with level-2 hints
- **Rule 1, literal** (1 test, 6 cases): build each pattern from `re.escape` of each word. The
  test passes a raw term on purpose (the fold would have removed the characters).
- **Rule 2, whole words** (3 tests, 10 cases): one compiled pattern per phrase: the escaped words
  joined by `\s*`, with a "not preceded by a word character" guard in front and a "not followed by
  a word character" guard behind (§2.4 for what a word character is). `re.search` gives the
  leftmost match.
- **The five bypasses** (1 test, 5 cases): nothing new if rule 2 is right; they prove the fold
  and your pattern fit together.
- **Devanagari and Telugu** (2 tests, 10 cases): the same pattern; only your definition of a word
  character decides these. If the Latin tests pass and these fail, look at §2.4 again. Two of
  the hits are Hindi spelling variants (साँस/सांस, बुख़ार/बुखार): the fold makes them equal, so
  your pattern needs nothing new for them. Telugu has hit cases only: whether a Telugu term may
  hit glued inside a longer word is an open question (§5), so no test pins it either way.
- **Rule 3, near** (4 tests, 14 cases): split the folded text into words, keeping each word's
  start offset. For each position, remember the last position at which each set was seen; when
  every set has been seen within the last `window` words, the term hits, and its start is the
  offset of the earliest of those words. One pass, no nested loop over windows. Stop at the
  first window that completes: rule 4 defines a `near` term's start exactly as this hint
  computes it (`chest chest pain` starts at 6, the second `chest`).
- **Rule 4, the hit** (5 tests): one `TermHit` per term that hits; sort by `(start, term_id)`.
- **Rule 5** (3 tests, 4 cases): check for duplicate ids before anything else.
- **Pure and linear** (2 tests): no logging, no state; the adversarial test has a generous time
  bound (a bound, not a measurement).
- **The self-test** (2 tests): the fixture folder's terms, variants and negatives through your
  function (`run_selftest`), directly and through `create_app`'s startup. They pass once the
  rules above do.

## 5. The hint ladder, the time-box and decisions
The ladder is M1's to M6's: level 1 = §2, level 2 = §4, level 3 = pseudo-code in chat on request
(never in this file), level 4 = a pairing review where you type. **Time-box: 2 h** of writing
(decided 2026-11-16; reading this guide does not count). When it is up and you are stuck, go one level up.
If you ask Claude to write it, Claude reminds you of the learning goal once and respects your
decision; the explain-back stays part of acceptance.

**TODOs (shesha):**
1. Read `match_terms`'s docstring, §2 and `test_regex_scripts.py`; run the tests (58 xfailed).
2. Write the body in `guards/match.py`; no other file changes except the switch.
3. When every case shows `XPASS(strict)`, remove the switch (§1); run the full check and the
   coverage gate.
4. PR from `m7/term-matching`; review by zara-safety; the explain-back (§6).

**Decided 2026-11-16 (shesha), as they bear on H1:** D5 (b) shesha writes H1; D3 (a) everything
fails closed, and a health business's message in a `not_covered` script (Telugu) hands off
(`not_covered_language`, team code, not yours); D6 (a) no self-harm terms by agents; the 2 h
time-box. **PR 1's decisions:** D-indic (b), strict whole words for Latin and Devanagari, with
Telugu's glued cases left out of the contract; D-hindi-fold (a), the fold maps chandrabindu to
anusvara and drops the nukta (team code: both sides arrive folded).

**Open:**
- *Suffix-tolerant matching for te* (pending the Telugu reviewer, D6). Telugu glues case endings
  onto the noun: `ఛాతీలో నొప్పి` is "pain in the chest", with `లో` glued to `ఛాతీ`, so a strict
  whole-word `ఛాతీ నొప్పి` misses it. The options are strict everywhere (recall from listed
  inflections and `near` terms) or letting a Telugu term end inside a word. Until it is decided,
  no test pins it and the self-test derives no glued negative for a Telugu term.
- One hit per term at its leftmost match (rule 4) [proposal].

## 6. Explain it back (part of acceptance)
- Why is an emergency screen tuned for recall, and what keeps its precision from collapsing?
  Name two mechanisms that recover the recall whole words cost.
- Walk `chest` + zero-width space + `pain` from the customer's keyboard to your function's hit.
  Which step removed the disguise, and which rule of yours made it a hit?
- Why does `\bसीने\b` miss `सीने में दर्द`, and how does your function decide where a Devanagari
  word ends?
- Why `re.escape`, and why is your function linear in the text? What would a nested quantifier
  in a term have done, and why would a timeout not have saved us?
- What happens, from startup to the customer, if your function raises, or misses a term's own
  accent variant?
- The term lists are public. Why is that not a weakness of the screen?
