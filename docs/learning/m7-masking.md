# M7 hand-write: PII masking (H2)

This is M7's second `[HAND-WRITE]` piece (D5 (b), decided 2026-11-16: "shesha writes H1; Claude
writes H2 and H3 at shesha's request"). PR 3 built everything around it: the locale registry
with every pattern as data (`locales.py`), Verhoeff and Luhn over Unicode digits
(`checksums.py`), the input cap and the `Masked` type with its placeholders (`pii.py`), and the
red-team runner that drives category E through it (`redteam/masking.py`). **H2**, `mask_pii`, is
the one function that turns a customer's text into text a third party or a log may see: it finds
every personal-data span the patterns describe, joins overlapping ones, picks each span's label
and replaces it with a typed placeholder, then looks once more in case a replacement freed a
neighbour. Pure Python over one string: no model, no network, no
files. The decisions behind every rule are in [ADR 0018](../adr/0018-guardrails.md) (part 5) and
the plan's G3, G6, G10 and "H2" sections ([`m7-pr-checks.md`](../plans/m7-pr-checks.md)).

> **Status (2026-11-20): H2 is written, by Claude at shesha's request (D5 (b)), on
> `m7/pii-masking`, for shesha's review; the explain-back (§6) is shesha's, due, and part of
> acceptance.** All 120 contract cases passed with no expected value changed and the switch is
> deleted. Rules 2, 3 and 6 (every start position, overlapping candidates join, repeated passes)
> follow ananya's PR 3 review, confirmed by shesha on 2026-11-18. One addition, the PR 3 open
> item: rule 2 also runs every pattern over a **squeezed view** without ZWNJ, ZWJ, VS15, VS16 and
> the keycap mark (§2.6), with 18 new hand-derived cases, plus one for the cap's boundary (6,001
> characters, which a mutation check showed no test pinned). After ananya-rag's review and a
> second mutation check (2026-11-20): two hand-derived pass tests (`+` phones glued one after
> another, §2.8) and six probes, each shown to fail on the mutant it was written for (the joined
> end, the best's length, a tie across the two views both ways, the cap checked once, a fixed
> over-cap message): 147 in all, with no earlier expected value changed. Log masking is now on
> and the 18 category E red-team cases are hard passes. The review's open questions are in §5.
> No number in this guide is a result: every text and digit string below is made up to explain a
> rule, and every personal-data value comes from a reserved range.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Branch | Who |
|---|---|---|---|---|---|---|
| **H2** | `mask_pii` in [`guards/pii.py`](../../apps/api/src/vectapilot_api/guards/pii.py) | [`tests/guards/test_mask_pii.py`](../../apps/api/tests/guards/test_mask_pii.py): 36 tests, 120 cases | about 40 lines (estimate) | 1.5 h | `m7/pii-masking` | Claude at shesha's request (D5 (b)) |

- **The docstring is the spec.** `mask_pii`'s docstring states the contract in six numbered
  rules; the tests are that contract made executable, with section headings named after them.
  Every expected value was worked out by hand and is explained in a comment next to it.
- **Already there and tested** (passing now):
  - `locales.LOCALES`: `in` (phones with `+91` or `0091`, mobiles, a trunk `0` with 10 digits,
    Aadhaar, PAN, GSTIN, IFSC, passport, UPI) and `generic` (e-mail, `+` phones, 13–19 digit
    runs, longer runs), each a tuple of `PiiPattern`s with an `id`, a `kind`, an optional
    `checksum`, a compiled `regex` and `anywhere` (the same pattern in a lookahead: a match at
    every start position). `locales.locales()` gives the default pair, India first
    ([`test_locales.py`](../../apps/api/tests/guards/test_locales.py)).
  - `locales.ascii_view(text)`: every decimal digit of any script as its ASCII digit, every
    fullwidth form as its ASCII character and every dash or minus sign as a hyphen, **one
    character for one**, so a span found in the view is the same span in the text.
  - `checksums.decimal_digits`, `checksums.checksum_valid(checksum, digits)`
    ([`test_checksums.py`](../../apps/api/tests/guards/test_checksums.py)).
  - `pii.PLACEHOLDERS`, `pii.MASK_CAP` (6,000), `pii.MAX_PASSES` (3), `pii.MaskingIncomplete`
    and `pii.Masked(text, counts)`, which **checks itself when built**: counts keyed by
    `PiiKind`, each positive and backed by its placeholder in the text
    ([`test_pii_types.py`](../../apps/api/tests/guards/test_pii_types.py)).
  - `caps.OverCap`, the error G3 uses for any text over its cap.
- **Not your job:** writing or changing a pattern (team code, reviewed by ananya-rag; a missing
  shape is a pattern change with its own test), cleaning the text (done before you are called),
  deciding where masking runs (the routes, PR 6–8) and the log formatter (`guards/redact.py`,
  built in PR 3: it calls your function through `log_masker()` once you have written it).

## 1. How to work

No database, no network, no key:

```bash
cd apps/api
git switch main && git pull                  # once PR 3 is merged
git switch -c m7/pii-masking
uv run pytest tests/guards/test_mask_pii.py -v -p no:randomly   # 120 xfailed
uv run pytest tests/guards/test_locales.py -v                    # passing now: read these first
```

**Reading the output (strict xfail, as in M1–M6).**

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but answers wrongly or raises something else | `FAILED`, with the assert diff |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

**One switch.** `test_mask_pii.py` carries one `pytestmark` (as H1's `test_match.py`). When every
case shows `XPASS(strict)`, delete that `pytestmark` statement and run the full check:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -p no:randomly
cd ../.. && make coverage    # tests under coverage, then the gate for gateway/ and guards/
```

**More things change state when you are done**, and each is a test telling the truth (these
fail as plain `FAILED`, not `XPASS`, so expect them):
- `tests/redteam/test_plan.py` asserts H2's probe reports "unwritten"; update that line in your
  PR. The red-team suite's 18 category E cases then stop being `pending:H2` and run as `hard`
  cases through `redteam/masking.py`: each must pass, or `scripts/redteam_gate.py` fails.
- `tests/redteam/test_masking_runner.py::test_every_e_case_reaches_h2s_stub_and_is_pending_on_h2`
  pins today's state; delete it in your PR (the E cases themselves now prove the runner).
- The log formatter switches masking on by itself (`log_masker()` stops returning None), so four
  tests that pin the interim change to their H2 versions:
  `test_redact.py::test_log_masker_is_none_while_h2_is_the_stub` (the masker is not None),
  `test_redact.py::test_configure_logging_installs_exactly_what_it_is_asked_for`
  (`formatter.masker is not None`),
  `test_redact.py::test_without_h2_the_redacting_setup_says_so_once_with_a_fixed_code` (no
  `log_masking` line is written at all) and
  `test_log_canary.py::test_every_line_but_the_servers_own_is_todays_line` (`masker is not
  None`; a line whose free text holds a G6 shape is no longer today's line). Run the whole
  suite: another log test may need its H2 version too; say which in your PR.

**The coverage gate covers `guards/`** (95 % combined and 95 % branch-only). The contract tests
reach the branches the contract needs; a branch they do not reach is dead code or a rule the
tests miss: delete it, or add a test and say so in your PR.

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say
so in your PR. Pure means pure: no I/O, no logging, no clock, no module-level cache. `mypy`
(strict) and `ruff` apply as everywhere. Reviews: `@agent-zara-safety` (and `@agent-ananya-rag`
for offsets and scripts). Then the explain-back (§6).

## 2. The concepts (hint level 1)

### 2.1 Spans, not words
Masking works on **spans**: a start and an end offset in the text. A phone number is not one
word (`+91 00000 12345` is three), and a span includes the separators inside it, so the
placeholder replaces the whole thing and no stray digit group is left behind. Each pattern's
`anywhere.finditer` over the view gives you a candidate at **every start position** (group 1
is the span); you collect them all, join the overlapping ones (§2.3), and only then build the
new string, from left to right, copying the text between spans unchanged. Replacing while you
search would shift every later offset.

Why every start, not plain `regex.finditer`: `finditer` never starts a match inside an earlier
one. In `1111 1111 1111 1111 1111` the 16-digit pattern matches from the first group, and the
match from the second group (which would reach the last group) is never tried, so the last
`1111` would stay visible. `anywhere` is the same pattern inside a lookahead `(?=(...))`: it
matches no text, so `finditer` tries every position.

Why the **view**: customers type digits in their own script. `+९१ ००००० १२३४५` is the same
number in Devanagari; `ascii_view` turns it into `+91 00000 12345` *without changing its length*,
so the patterns can be written once over ASCII and every span still points into the original
text. You search the view and replace in the text.

### 2.2 Checksums only pick the label
Two identifiers carry their own check digit. Aadhaar uses **Verhoeff** (a dihedral-group check
that catches every single-digit error and every swap of two neighbours); payment cards use
**Luhn** (double every second digit from the right, the sum must end in 0; it misses a `09`/`90`
swap). Both are team code over Unicode digits (`checksums.py`), tested against published
vectors.

A pattern with a checksum labels its span with its kind when the check holds and `ID_NUMBER`
when it fails. That is all a checksum does here: **it never decides whether a span is masked**.
`checksums.decimal_digits(span text)` gives you the digits (separators skipped, any script).

### 2.3 Overlaps: candidates join; the best one names the span
Patterns overlap on purpose. Inside a card `4111 1111 1111 1111` the Aadhaar pattern finds the
first twelve digits; inside an e-mail address `customer12@mail.example` the UPI pattern finds
`customer12@mail`; inside a per-digit spaced phone the 8- to 11-digit `+` phones find prefixes.
The rule (rule 3 of the docstring; the plan's "H2": longest first, then leftmost):
1. candidates that **share a character join** into one span, and so on through every candidate
   they join; candidates that only touch (`[0, 2)` and `[2, 4)`) stay two spans;
2. the joined span takes the label of its **best** candidate: the **longest**, then the
   **leftmost**, then the **earlier pattern** (locale order, then pattern order). So the card
   is `[CARD]`, not `[AADHAAR]`, and `0091 00000 00000` is `[PHONE]` with India first and
   `[CARD]` with generic first (§3).

Why join instead of "the longest wins and a loser is dropped whole" (the first draft): two
numbers one space apart. `card 4111 1111 1111 1111 1225 123` (a card, its expiry, its CVV)
has a 19-digit window from the second group to the end; it is the longest, and dropping the
card's own candidate whole left `4111` visible. Joining masks it all (`card [ID_NUMBER]`).
The label is less precise there, which only tightens; a visible digit cannot be taken back.

Sort the candidates by start, sweep once to build the joined spans (a candidate that starts
before the current span's end extends it), and keep the best candidate of each by the key
above.

### 2.4 Why shape decides
"Is this an Aadhaar number?" has two possible tests: the **shape** (12 digits grouped 4-4-4) and
the **checksum**. If the checksum decided, a customer who mistypes one digit of their Aadhaar
would send the other eleven to a model provider and into the logs: the number is still theirs,
still personal data, and now unmasked. So the shape decides that a span is masked, and the
checksum only picks between `[AADHAAR]` and `[ID_NUMBER]` (G6, D1 (a)). The cost is false masks,
pinned in `test_locales.py`'s `KNOWN_OVER_MASKS`: two ISO dates in a row
(`2026-11-17 2026-11-18`, 16 digits), an ISBN-13, a 13-digit courier AWB (the agent then cannot
look it up), the tail of a UUID, `Order B2026111` (a passport's shape), `order1234a` (a PAN's),
ten single digits starting with 6–9 (a mobile's) and a pair of GPS coordinates. A 13–19 digit
order id would count against the red-team suite's over-escalation ceiling if a look-alike held
one; none does in v0.1. A false mask only tightens (G2); a missed mask cannot be taken back.

The same thinking runs the other way: a Luhn-valid number of a shape G6 does not list (an
11-digit `79927398713`) is **not** masked. Shape decides, both ways.

### 2.5 Why non-reversible
A placeholder carries the type and nothing else: no index (`[PHONE_1]`), no hash, no lookup
table kept somewhere. Two different numbers mask to the same text (a test pins it). Reversible
schemes (tokenisation with a vault, format-preserving encryption) exist for systems that must
give the value back; this platform never needs to, because staff read the original in its own
database (M8) and the model never needs the number to answer. Anything reversible would be a new
secret to protect, and a hash of a phone number is reversible by hashing every possible
number until one matches: there are only ten billion 10-digit strings.

### 2.6 What pattern masking cannot do
Patterns find what has a **shape**: digits in known groupings, an `@`, a code like a PAN. They do
not find **names** ("Anaya Testwala"), **street addresses**, dates of birth written in words, or
a health condition ("my diabetes"). Those need named-entity recognition (Presidio, D1 (c)), an ML
model the team cannot explain line by line, and are a documented gap (G6; the note will say so).
Masking is also not the answer to Q4c: health text without identifiers is still health data.

Known limits of the shapes themselves, each a pattern question rather than yours (ananya's PR 3
review ran each one):
- **Bank account numbers** of 9–12 digits (`acct 12345678901`) are not masked: not a G6 type,
  and a keyword-anchored pattern ("a/c", "account") is per language and outside the grammar. The
  IFSC next to it is masked.
- **Dotted phones** (`99999.99999`) and **parentheses** (`(020) 7946 0123`) are not matched; a
  full stop between groups counts only for Aadhaar and 13–19 digit runs
  (`4111.1111.1111.1111` is masked).
- **Other separators** between digit groups split the number, so each group stays visible when
  it is no shape alone: a line break (`99999` newline `99999`, or a card broken across lines,
  `4111 1111` newline `1111 1111`; `clean` turns CRLF, U+2028, U+2029, NEL and VT into a
  newline too), a run of separators (`99999 - 99999`, `99999--99999`), and a slash, underscore,
  comma or middle dot (`99999/99999`, `99999_99999`, `99999,99999`, `99999·99999`). Tightening
  any of them is a locale choice (§5, open).
- **Digits that are not decimal digits** (Unicode category No, so not digits in the view):
  superscripts (`⁹⁹⁹⁹⁹⁹⁹⁹⁹⁹`) and circled digits (`⑨⑨⑨⑨⑨⑨⑨⑨⑨⑨`) are not matched.
- **Spaced or written-out e-mail addresses** (`customer01 @ mail.example`,
  `customer01[at]mail.example`) are not matched, and an e-mail local part longer than 64
  characters keeps its start.
- **A Devanagari local part** is matched only from the `\w` characters right before the `@`
  (Python's `\w` does not match a combining vowel sign, virama or anusvara): `नाम@mail.example`
  becomes `ना[EMAIL]` and `राम@okaxis` becomes `रा[UPI]`, because a letter sits right before the
  `@`. A local part that ends in a sign has no candidate at all: `नमस्ते@gmail.com` and
  `सीता@okaxis` stay as written (ananya's H2 review; widening the squeezed view to every
  combining mark would mask them, §5, open).
- **Digits joined by ZWJ or ZWNJ** (which `clean` keeps for Indic conjuncts and emoji), VS15 or
  VS16, and **keycap digits** (`9`, VS16, U+20E3) were the PR 3 open item; H2 handles them with
  a **squeezed view** (rule 2, `pii.SQUEEZED_OUT`): when the ASCII view holds one of those five
  characters, every pattern also runs over the view without them, and an offset map turns each
  span back into the text's offsets, from its first character to just before the next character
  kept, so a keycap's marks after the last digit go with it. Both views' candidates join under
  rule 3, so the squeezed view only adds spans: `99999` + ZWJ + `99999` and ten keycap nines are
  each `[PHONE]`, and a PAN after `x` + ZWJ is still masked although `x` sits right before it in
  the squeezed view. Still not matched: digits carrying any other combining mark (a strike-through
  U+0336, an enclosing circle U+20DD, a combining grave U+0300), which `clean` keeps (§5, open).
  And the log formatter's cut (`redact.safe_cut`, team code) does not look through the five
  characters yet, so a field over `FIELD_CAP` cut at the space in `99999` + ZWJ + space + ZWJ +
  `99999` keeps its first five digits (§5, open; before H2 all ten stayed).
- **Two codes glued together** (`ABCZZ9999ZABCZZ9999Z`) are not masked: each blocks the other's
  whole-word boundary. A code glued to a number is masked in a second pass (rule 6) when the
  number is itself masked: `ABCZZ9999Z1` and `ABCZZ9999Z99999999999` stay whole (neither number
  is a shape, so the code stays glued), as does a code glued after letters, `myPANisABCZZ9999Z`.
- **A run longer than 19 digits** is masked whole when contiguous (`generic.long_digits`) and,
  with candidates at every start, when spaced per digit or grouped in fours, threes or fives (a
  13–19 digit window ends on a group's end); its label is then usually `ID_NUMBER`. Two groups of
  ten (`0123456789 0123456789`) match nothing: no window ends inside them, and ten digits
  starting with 0–5 are no mobile.

### 2.7 Bounded by construction (G3)
`re` has no timeout and `asyncio.timeout` cannot interrupt pure code, so a slow pattern would
hold a request with nothing to stop it. Three things keep masking bounded: every text is capped
first (`MASK_CAP`, 6,000; over it you raise `OverCap`, never truncate); every pattern follows
G3's written rule, checked when the pattern is built (no quantifier inside another, no two
neighbouring quantifiers that can match the same character, every repeat bounded), so each start
position costs a bounded number of steps, `anywhere`'s too; and at most `MAX_PASSES` (3) passes
replace anything. One exception to the second: a pattern of one item may repeat up to the cap
(rule 3 of G3), and `generic.long_digits` reads from every start of a run of digits to the run's
end, so it is quadratic within one run: at most about `MASK_CAP` squared over 2 characters (18
million) per view, and H2 searches a second, squeezed view when the text holds a joiner (§2.6).
The adversarial tests at the cap assert under `GENEROUS_SECONDS` (2.0 s): a bound, not a
measurement.

Why 6,000 and not the message's 2,000: G1 counts 2,000 on the raw text, and `clean`'s NFC can
make a text longer. A precomposed nukta letter (U+095E, which Hindi keyboards type) becomes two
code points, so a valid Hindi message near 2,000 characters would raise `OverCap` and hand off.
NFC makes a text at most three times longer (a Unicode stability guarantee), so three times the
message cap fits every valid message.

### 2.8 Passes: a replacement can free a neighbour
Patterns that need a whole word look one character beyond the span (`(?<![A-Za-z0-9])`). In
`pan ABCZZ9999Z9999999999` the PAN is followed by a digit, so it is no whole word, and only the
mobile is found: one pass gives `pan ABCZZ9999Z[PHONE]`, with the PAN visible. Now a `[`
follows it, and a second pass masks it. So masking repeats until a pass finds nothing, and the
counts add up. A placeholder never holds a match (`test_pii_types.py`), but a replacement can
free a neighbour that frees the next one: a phone never starts right after a digit, so in
`9999999999+99999999999+99999999999` each `+` phone waits until the digit before it is inside a
placeholder, one pass each. Such chains can be any length, with the registered patterns as with
the tests' letter patterns, so at most `MAX_PASSES` passes replace anything and a text that
still holds a span then raises `MaskingIncomplete`: fail closed, a `guard_error` hand-off at a
route and `[redaction_failed]` in a log field. A crafted text can force that hand-off; it can
never make masking return a visible value. The same refusal leaves `1+99999999999` as it is:
the `+` phone has a digit before it, and eleven digits alone are no other shape. The result is
idempotent by construction: masking it again finds nothing.

## 3. The contract, with hand-checkable examples
Texts go through `clean` first; `DEFAULT` is `locales()` (India, then generic). "nines" and "ones"
are digit strings that belong to nobody.

| Text | Masked text | Counts | Rule |
|---|---|---|---|
| `call +91 00000 12345 now` | `call [PHONE] now` | PHONE 1 | 2, 5 |
| the same in Devanagari digits | `call [PHONE] now` | PHONE 1 | 2 (the view) |
| `call 99999<ZWJ>99999 now`, or ten keycap nines | `call [PHONE] now` | PHONE 1 | 2 (the squeezed view) |
| `ID <Aadhaar, valid check>` | `ID [AADHAAR]` | AADHAAR 1 | 4 |
| `ID <Aadhaar, wrong check digit>` | `ID [ID_NUMBER]` | ID_NUMBER 1 | 4 (shape decides) |
| `ref 1111111111111` (13 ones) | `ref [ID_NUMBER]` | ID_NUMBER 1 | 4 (Luhn: 7 + 12 = 19) |
| `card 4111 1111 1111 1111 ok` | `card [CARD] ok` | CARD 1 | 3 (longest), 4 |
| `code 79927398713 ok` | unchanged | none | 4 (no G6 shape) |
| `no. 0091 00000 00000`, India first | `no. [PHONE]` | PHONE 1 | 3 (earlier pattern) |
| the same, generic first | `no. [CARD]` | CARD 1 | 3, 4 (Luhn: 1 + 9 = 10) |
| `abc` with test patterns `bc`, `ab` | `[PHONE]` | PHONE 1 | 3 (joined; leftmost names it) |
| `card 4111 1111 1111 1111 1225 123 ok` | `card [ID_NUMBER] ok` | ID_NUMBER 1 | 2, 3 (joined; Luhn: 34) |
| `pan <PAN>9999999999 ok` | `pan [PAN][PHONE] ok` | PAN 1, PHONE 1 | 6 (second pass) |
| `"9" * 6000` | `[ID_NUMBER]` | ID_NUMBER 1 | 2 (one span up to the cap) |
| 6,001 characters | `OverCap` | | 1 |
| no locales | `ValueError` | | 1 |
| a masked text, again | the same text | none | 6 |

## 4. Test by test, with level-2 hints
- **Rule 1, errors** (§6 of the tests, 7 tests): check the locales and the cap before anything
  else, once; raise `caps.OverCap` with a fixed message that names `MASK_CAP`, never the text.
  (`caps.require_within_cap` checks a surface's own cap, which is not this one.)
- **Rule 2, candidates** (§1, 3 tests, 33 cases): `view = ascii_view(text)`, then
  `for locale in locales: for pattern in locale.patterns: for match in
  pattern.anywhere.finditer(view)`, with `match.span(1)` as the span. Remember each
  candidate's start, end, pattern and order (a running counter over all patterns).
- **Rule 3, joining** (§3, 7 tests, 20 cases): sort by start; sweep, extending the current
  span while the next candidate starts before its end (`start < end`, so touching spans stay
  apart); for each joined span keep the candidate with the smallest
  `(-(end - start), start, order)`.
- **Rule 4, labels** (§2, 9 tests, 46 cases): for the best candidate, with a checksum,
  `checksum_valid(pattern.checksum, decimal_digits(text[start:end]))` (its own span, not the
  joined one) picks between `pattern.kind` and `PiiKind.ID_NUMBER`.
- **Rule 5, replace and count** (§4, 2 tests, 5 cases): walk the joined spans in text order,
  copy the text before each, append `PLACEHOLDERS[label]`, count the label; copy the rest.
- **Rule 6, passes, and purity** (§5, 10 tests, 13 cases): rules 2–5 are one pass over the
  current text; loop until a pass finds nothing, adding the counts; after `MAX_PASSES` passes
  that replaced something, one more pass that still finds a span means
  `raise MaskingIncomplete`. Return `Masked(Clean(result), counts)`. No logging.
- **The cap** (§7, 3 tests): nothing to add; they show the work stays within a generous bound.
- **Rule 2's squeezed view** (§8, 8 tests, 20 cases; added with H2, §2.6): only when the view
  holds a character of `SQUEEZED_OUT`, build the view without them and a list of where each of
  its characters sits in the view (one more entry: the view's length), run every pattern over it
  too, and map each span `[a, b)` to `[kept[a], kept[b])`.

**As written (2026-11-20).** Rules 2 and 3 meet in one array instead of a sort: for each start
offset H2 keeps only the longest candidate that starts there (patterns run in order, each over
both views, so a tie keeps the earlier pattern whichever view found it). Every other candidate
starting there lies inside it, so it would join the same span and never be its best. Then one
sweep from left to right joins (a start before the current span's end; the joined span ends at
the furthest end so far, not at its best candidate's) and keeps the best (only a strictly longer
candidate than the best so far replaces it, so the leftmost wins a tie). Work: the regexes over
each view, then linear in the text; the one quadratic part is `generic.long_digits` at every
start of a long digit run (a run of 6,000 nines reads about 18 million characters per view
inside the regex engine, twice that with a squeezed character; the cap tests assert under
`GENEROUS_SECONDS` = 2.0 s, a bound, not a measurement).

## 5. The hint ladder, the time-box and decisions
The ladder is M1's to M6's: level 1 = §2, level 2 = §4, level 3 = pseudo-code in chat on request
(never in this file), level 4 = a pairing review. **Time-box: 1.5 h** of writing (the plan's
"H2"; reading this guide does not count). Under D5 (b) Claude writes it at shesha's request; the
reminder of the learning goal was given with D5, and the explain-back stays part of acceptance.

**TODOs:**
1. Read `mask_pii`'s docstring, §2 and `test_locales.py`; run the tests (120 xfailed).
2. Write the body in `guards/pii.py`; no other source file changes (the tests named in §1 do).
3. When every case shows `XPASS(strict)`, remove the switch; update the red-team and log tests
   (§1); run the full check, the coverage gate and the red-team gate.
4. PR from `m7/pii-masking`; review by zara-safety and ananya-rag; shesha's explain-back (§6).

**Decided 2026-11-16 (shesha), as they bear on H2:** D1 (a) standard-library masking, no
package, shape decides and the checksum picks the label, names and addresses a documented gap;
D5 (b) Claude writes H2 at shesha's request; D3 (a) everything fails closed (a raising masker
is a `guard_error` hand-off at the route). The `RedactingJsonFormatter` (G10) is built in PR 3
and masks every free-text log field with your function as soon as it stops raising
`NotImplementedError` (its startup probe, `guards.redact.log_masker`); until then the app's
free text is written as before and `log_masking status=masker_unavailable` says so (decided 2026-11-18, shesha;
the plan's PR 6 bullet and ADR 0018 part 5 are amended to match). Your PR
then also turns the four interim log tests listed in §1 into their H2 versions. Done in H2's PR
(2026-11-20): the four log tests, `test_plan.py`'s two H2 lines, the deleted stub-pinning test
in `test_masking_runner.py`, and one more, `test_runs.py`'s dry run (a dry run now scores the
18 E cases in process); plus `test_redact.py::test_a_stub_h2_would_still_mean_no_masker`, which
keeps the probe's stub branch tested now that the real stub is gone.

**Open (proposals in PR 3, for review):**
- Every business masks with `in` and `generic` until Q4b names the other countries and a
  business's configuration names its locales.
- Rules 2, 3 and 6 as now written (ananya's PR 3 review, applied as a proposal): candidates at
  every start position, overlapping candidates join, passes repeat (at most three). The first
  draft ("the longest wins, a loser is dropped whole", `finditer` only, one pass) left digits
  visible: the first group of a card pasted with its expiry and CVV, 4 digits of an Aadhaar
  pasted after a phone or a card, up to 12 digits of a long spaced run, and a PAN glued to a
  number. shesha confirms the new rules, or keeps the draft with those exposures written down,
  before H2 is written.
- Bare mobiles need 6–9 first, then nine digits with any single space or hyphen (5-5, 3-3-4,
  4-3-3, 4-6, per digit); a trunk `0` takes any ten digits after it (a mobile or an STD
  landline). Ten single digits starting with 6–9 are masked as a phone (`KNOWN_OVER_MASKS`);
  `call 9999 999 999` is masked now, where the first draft missed it.
- GSTIN as a `[PAN]`-labelled shape (ananya's review, decided 2026-11-18, shesha: a scope choice
  inside G6's nine types); bank account numbers stay a gap (§2.6). The M7 note records both.

**After ananya-rag's H2 review (2026-11-20). Decided 2026-11-20 (shesha): all four stay recorded
gaps; the first becomes a small follow-up commit in PR 7, the rest are revisited with real data:**
- **The log cut and the squeezed view: done in M7 PR 7 (2026-11-21).** `redact.safe_cut` (team
  code, outside H2's file) used to treat a space between two squeezed characters as safe, so a
  log field over `FIELD_CAP` could keep `99999` + ZWJ of `99999` + ZWJ + space + ZWJ + `99999`
  (no regression: before H2 all ten stayed; measured before the fix: 40 of 70 test lines, five
  squeezed characters by 14 cut positions, kept five digits). The cut now compares the nearest
  character on each side of the space that is not squeezed out, through `redact.squeezed_out`,
  which reads `pii.SQUEEZED_OUT` itself (so widening the set below moves both; `guards/pii.py`
  unchanged). Tests: `test_redact.py::test_a_field_over_the_cap_keeps_no_digit_joined_across_a_squeezed_space`
  (with H2) and the two tests after it.
- **Widen the squeezed view** from the five characters to them plus every combining mark
  (Unicode Mn, Mc and Me). It would mask digits carrying a strike-through, enclosing circle or
  grave mark, and Devanagari local parts that end in a sign (§2.6). ananya's scratch run changed
  0 of the 121 Hindi, Telugu and English lines of `data/sample` (not a result: a check). Cost:
  the second view is then built for almost every Hindi or Telugu text.
- **Locale tightenings** (§2.6), each trading recall for false positives in tables and
  arithmetic: (a) a newline in `SEPARATOR` and `GROUP_SEPARATOR`; (b) a separator run `[ -]{1,3}`
  (G3's rule 2 still holds: it never overlaps `[0-9]`); (c) PAN's right boundary as "not before
  a letter" (a PAN ends in a letter, so a digit after it cannot lengthen it; GSTIN unchanged).
- **The `+` phone after a digit** (§2.8): dropping the digit-before-`+` refusal from
  `generic.phone_N` only would find every glued `+` phone in pass 1 and mask `1+99999999999`;
  the cost is that arithmetic such as `100+20000000` becomes `100[PHONE]`. This changes a rule
  decided on 2026-11-18 (zara's PR 3 mutation check), so it is shesha's call.

## 6. Explain it back (part of acceptance)
- Why does shape decide and not the checksum? Give the customer story for each way round.
- Walk `+९१ ००००० १२३४५` from the customer's keyboard to `[PHONE]`. Where do the Devanagari digits
  become ASCII, and why does the replacement still land on the right characters?
- Inside `4111 1111 1111 1111` two patterns match. Which wins, by which rule, and what would go
  wrong if the shorter one won?
- Why is `[PHONE]` safer than `[PHONE_1]` or a hash of the number? What would a hash cost an
  attacker to reverse?
- Name three kinds of personal data this masking cannot see, and what the platform does about
  them today.
- Why can the masking of a 2,000-character message not hang a request, although `re` has no
  timeout?
