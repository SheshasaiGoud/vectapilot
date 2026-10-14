# M1 hand-write: cost per call (H1) and retry with backoff (H2)

These are the two `[HAND-WRITE]` pieces of the LLM gateway ([ADR 0006](../adr/0006-llm-gateway.md)).
Everything around them (types, errors, settings, the provider adapter, the gateway itself) is built
by the team; your two pieces are the ones that decide **money** and **time**. H3 (`latency_summary`)
comes later, with step 8 (`m1/measure`); §2.13 only previews its concept.

## Your tasks

| | Write the body of | So that these pass | Size | Time-box | Your branch |
|---|---|---|---|---|---|
| **H1** | `cost_usd` in [`gateway/pricing.py`](../../apps/api/src/vectapilot_api/gateway/pricing.py) | [`tests/gateway/test_pricing.py`](../../apps/api/tests/gateway/test_pricing.py): 15 tests, 22 cases | about 12–18 lines | 2 h | `m1/cost` |
| **H2** | `backoff_delay` and `call_with_retries` in [`gateway/retry.py`](../../apps/api/src/vectapilot_api/gateway/retry.py) | [`tests/gateway/test_retry.py`](../../apps/api/tests/gateway/test_retry.py): 25 tests, 40 cases | about 15–25 lines (both) | 3 h | `m1/retry` |

- **The docstring is the spec.** Each function's docstring states its contract; the tests are that
  contract made executable. Read the docstring first, then the tests.
- **Already there and tested:** `Usage`, `TokenPrices`, `LongPromptTier`, `ModelPricing` and
  `RetryPolicy` in `gateway/types.py`, and every error class in `gateway/errors.py`.
- **Not your job** (the gateway, PR 6, or the adapter, PR 5, do it): summing cost over retries and
  repairs; counting attempts and attempt timeouts and collecting their usage and request ids (that
  happens inside the `call` the gateway hands to H2); the per-attempt timeout and the deadline
  timer; deciding from status codes and headers which error is retryable. You read `retryable`;
  you never look at an HTTP status.
- H1 and H2 are independent: do them in either order, as soon as PR 2 is merged.
- **Early warning:** if H1 is not merged by day 4 of M1, or H2 by day 6, the schedule is at risk.
  Say so early, so we can adjust scope instead of rushing.

## 1. How to work

```bash
cd apps/api
git switch main && git pull          # once PR 2 is merged
git switch -c m1/cost                # H1 (m1/retry for H2)
uv run pytest tests/gateway/test_pricing.py -v     # see what is expected: all "xfailed"
# write the body …
uv run pytest tests/gateway/test_pricing.py -v
uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .
```

(`make check`, run from the repository root, does the same checks as CI.)

**Reading the output (strict xfail, the same mechanism as M0's `test_readyz.py`).** Each test
module starts with a `pytestmark = pytest.mark.xfail(strict=True, raises=NotImplementedError, …)`
statement (five lines). While it is there:

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but gives a wrong answer or raises something else | `FAILED`, with the assert diff (only `NotImplementedError` counts as expected) |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

So the moment you replace `raise NotImplementedError`, the whole module turns red:
`XPASS(strict)` for the tests you got right, `FAILED` for the ones you got wrong. Both are red, so
read which one each test shows. When every test in the module shows `XPASS(strict)`, **delete the
whole `pytestmark = …` statement** and run again: every test must now simply pass.

H2 has two functions in one module. If you write `backoff_delay` first, its 6 tests turn
`XPASS(strict)` while the 19 `call_with_retries` tests stay `xfailed`. That is expected; delete the
marker when both are done.

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say
so in your PR: that is a review conversation. Your code must pass `mypy` (strict) and `ruff` like
everything else. Commit with your own message (for example `feat(gateway): implement cost_usd`),
push, open a PR, and ask `@agent-diego-backend` to review it (for H2 `@agent-zara-safety` also adds
hostile cases). Reviewers must not rewrite it for you. Then explain it back (§5).

## 2. The concepts (hint level 1)

For H1 read 2.1 and 2.3–2.5; for H2 read 2.6–2.10. The rest (2.2, 2.11–2.13) is background for
the gateway and for H3: worth knowing, not needed to write H1 or H2.

### 2.1 The five token classes

A response's `Usage` has five counts, because each one has its own price:

| Field | What it counts |
|---|---|
| `input_tokens` | the **uncached** part of the prompt only |
| `cache_read_tokens` | prompt tokens served from the provider's prompt cache (the cheap one: that is the point of a cache) |
| `cache_write_5m_tokens` | prompt tokens written into the cache, kept for 5 minutes |
| `cache_write_1h_tokens` | prompt tokens written into the cache, kept for 1 hour |
| `output_tokens` | everything the model generated: the visible answer **and** any thinking (§2.12) |

The prompt's total is input + cache reads + both cache writes. The classic mistake is to treat
`input_tokens` as the whole prompt. The exact prices are on the pricing page and go into
`models.toml` in PR 3. Prompt caching itself arrives in M3, so in M1 the cache counts are usually
0. Your function must still be right for all five from day one; the tests use every class.

Do not confuse this with the gateway's **response cache** (ADR 0006, off by default). A
response-cache hit means no model call at all and costs $0. A prompt-cache read is still a paid
call, just cheaper for the cached part.

### 2.2 Context window vs `max_tokens`, and `count_tokens`

- **Context window:** the most tokens a model can handle in one request, prompt and output
  together. It differs per model: look it up on the provider's models page, don't memorise it.
- **`max_tokens`:** the cap on the **output** of one response, which you choose (per alias:
  `max_output_tokens` in the catalog; per call: `LLMRequest.max_tokens`). A model that hits it stops
  mid-answer with stop reason `max_tokens`, which becomes our `OutputTruncated`. That error is
  never repaired, because a repair would hit the same cap. Thinking is generated output too, so it
  uses up the same cap.
- **`count_tokens`:** the provider's token-counting endpoint counts a prompt's input tokens
  *before* you send it. It is useful for pre-flight checks (M7: will it fit, roughly what will it
  cost). Read its documentation before relying on it (rule 7): whether its count matches billed
  usage exactly is something to check, not assume.
- Tokens per character vary by language and script. "Hindi and Telugu use more tokens per
  character than English" is a hypothesis to measure, not a fact.

For H1 this means cost depends on tokens, not on characters or message counts, and the long-prompt
tier (§2.5) is about **prompt** tokens.

### 2.3 Prices are versioned data, not code

- Prices change, and models come and go. So prices live in `models.toml` (PR 3) with their source
  URL and checked date, and every `llm_call` record carries `catalog_version`. Any cost can then be
  recomputed later from the recorded usage and that catalog version.
- Cost is priced by the model that **answered** (`model_answered`), not by the alias.
- A configured model without a price **stops startup**. It never costs a silent $0, because a $0
  cost looks like success, which is the worst kind of wrong.
- So your function gets a `ModelPricing` and nothing else. It never sees a model name, reads no
  file and contains no price. All prices in the tests are made up (the long-prompt tier is exactly
  ten times the standard one, so a wrong tier is easy to spot), so the tests never go stale.
- Prices go stale: re-check them roughly monthly. A checked date in the catalog that is older than
  about a month is the warning sign.

### 2.4 `Decimal` vs `float`

Run on Python 3.12.15, 2026-10-14:

```pycon
>>> 0.1 + 0.2
0.30000000000000004
>>> from decimal import Decimal
>>> Decimal("0.1") + Decimal("0.2")
Decimal('0.3')
>>> Decimal(0.1)
Decimal('0.1000000000000000055511151231257827021181583404541015625')
>>> Decimal("0.1") + 0.1
Traceback (most recent call last):
  ...
TypeError: unsupported operand type(s) for +: 'decimal.Decimal' and 'float'
```

- A float is a **binary** fraction: 0.1 cannot be stored exactly (the first result). Tiny errors
  add up over millions of calls, and the total can depend on the order of the additions. Money is
  decimal, so use decimal arithmetic.
- Build a `Decimal` from a **string** or an int, never from a float (`Decimal(0.1)` above): the
  float's error is copied in. That is why the catalog is read with `tomllib`'s
  `parse_float=Decimal`, and why the `Usd` type in `types.py` refuses floats outright.
- Python refuses to mix `Decimal` and `float` (the last example). That is a feature: a float that
  sneaks into cost code fails loudly instead of quietly rounding.
- `Decimal("0") == 0` is `True`. So an equality check cannot tell an int from a `Decimal`, which is
  why one test checks the return **type**.
- We never round a cost: it is exact to the token. Rounding happens only for display.

### 2.5 The long-prompt tier

Quoted from <https://platform.claude.com/docs/en/about-claude/pricing>, section "Long context
pricing" (checked 2026-10-14; the same quote is in the `cost_usd` docstring):

> Claude Haiku 5.5 is priced by prompt length: a request whose prompt is over 100,000 tokens pays
> higher prices. A request's prompt length counts all of its input tokens, including cache reads
> and cache writes. Each request is priced on its own: a request over the threshold pays the higher
> prices even when part of its prompt is a cache hit, and earlier requests keep the prices they
> were billed at.

How our contract (the `cost_usd` docstring) reads it:

- **"over"** is strictly greater: a prompt of exactly the threshold stays on the standard tier.
- **"all of its input tokens"** means input, cache reads and both cache writes. Output is not
  prompt.
- The tier is chosen **once per request** and prices **every** class of that request, output
  included, from that tier's prices. The quote does not say which classes the higher prices
  raise; PR 3's price check confirms that on the page. If the page leaves a class at its standard
  price, the catalog's long-prompt tier simply repeats that price, so your code is the same
  either way.
- **"Each request is priced on its own"**: one call to `cost_usd` prices one response. A retry or a
  repair is a separate request with its own tier; the gateway adds them up (PR 6).
- Whether a model has this tier at all is data: `long_prompt` is `None` for a single-tier model.
  The threshold is data too (100,000 on the page, 100 in the tests). Your code never compares model
  names or contains the number 100,000.

### 2.6 Backoff, full jitter and the thundering herd

- Retrying at once hammers a server that is already struggling. **Exponential backoff** makes each
  retry wait longer than the last (the bound doubles), up to a **cap**.
- **The thundering herd.** When the provider has a hiccup, many clients fail at the same moment:
  other companies' clients, and our own concurrent calls (every business shares one workspace). If
  they all follow the same fixed schedule, they also retry at the same moments, in waves that hit
  the server just as it tries to recover.
- **Jitter** breaks the waves: each client waits a random amount. With **full jitter**, the wait is
  random anywhere from zero up to the current bound (AWS Architecture Blog, "Exponential Backoff And
  Jitter", 2015). This spreads retries the most evenly. Sometimes a retry comes almost at once, and
  that is fine.
- The cap stops one wait from growing absurd. The deadline (§2.10) is the hard stop for the whole
  call.
- **Testability.** Randomness and time are passed in as `rand`, `clock` and `sleep`. In production
  the gateway passes `random.random`, `time.monotonic` and `asyncio.sleep`; tests pass fakes, so
  nothing ever waits. It is the same dependency-injection idea as the fake checks for `/readyz` in
  M0.

### 2.7 Retry-After

- `Retry-After` is an HTTP response header (RFC 9110, section 10.2.3): the server says how long to
  wait. It is typical on 429 (too many requests) and 503. The adapter (PR 5) turns it into
  `Transient.retry_after`, in seconds, so you never parse headers.
- When the server says how long, believe it: wait exactly that long, no jitter. The server knows
  when its limit window resets, and retrying earlier only earns another 429.
- If it asks for **more than our cap**, don't retry at all: raise now. With a 30-second deadline, a
  long wait burns the user's time; failing fast records `transient_exhausted` and lets the caller
  (later the M4 router) decide. Decided by shesha on 2026-10-14; the alternative considered was
  waiting the full Retry-After whenever it still ends before the deadline. The tests expect this
  rule.
- The provider's `x-should-retry` header is also handled in the adapter: it sets `retryable` on
  `Transient` and `ProviderError` only. H2 just reads `retryable`.

### 2.8 Transient vs permanent, and why a spend limit is never retried

ADR 0006's error table splits errors in two:

- **Transient** errors may pass if you wait: connection errors, timeouts, 408, 409, 429, 5xx, 529.
  They become `Transient`, retryable by default.
- **Permanent** errors repeat for the same request: 400/404/413/422 (`InvalidRequest`), 401/403
  (`AuthenticationFailed`), 402 (`BillingError`), the spend limit (`SpendLimitReached`), plus
  `ModelRefused` and `OutputTruncated`. A retry wastes the deadline and adds load. A refusal or a
  truncation is a complete response, so retrying it pays again for the same outcome.

**The spend limit.** A 429 usually means "slow down", which is transient. The spend-limit response
(a 429 or a 400 with a documented shape) means "this workspace's monthly limit is reached". That
does not clear in seconds; it clears when the month resets or a human raises the limit. Retrying
burns the deadline for nothing, keeps hitting the API, and delays the one event a human must act
on, because one business can exhaust a limit every business shares. So the adapter checks the
spend-limit shapes **first**, before the generic "429 → Transient" rule. A `SpendLimitReached`
cannot be built as retryable, and like every field of a gateway error, `retryable` is read-only
afterwards.

In your code, decide only by `retryable`, an attribute every `GatewayError` has, never by class name
or status code. Anything that is not a `GatewayError` (a `ValueError`, a bug) is not retried either:
a retry would only hide the bug.

H2 never logs, prints or wraps an error: no `logger.exception`, no `raise … from …`, no new
exception around the old one. It re-raises the very object it got, and PR 6 writes the record. A
chained exception is exactly how prompt text could slip into a traceback (the PR 5 rule; see
`test_raising_inside_an_except_block_chains_the_caught_text` in `test_errors.py`).

### 2.9 Never catch `CancelledError`

- asyncio cancels a task by raising `asyncio.CancelledError` inside it, at the `await` where it is
  waiting. `asyncio.timeout`, `TaskGroup` and server shutdown all rely on this.
- Since Python 3.8 it is a `BaseException`, not an `Exception` (checked:
  `issubclass(asyncio.CancelledError, Exception)` is `False` on 3.12). So `except Exception` does
  not catch it, but `except BaseException` or a bare `except:` does, and that breaks timeouts: a
  swallowed cancel means the deadline no longer stops the call.
- To act on a cancel (PR 6 writes its `llm_call` record), use `finally`, or catch, record and
  re-raise. Never retry it and never turn it into something else.
- It can arrive in two places in H2: during the call and during the wait. Both are tested.
- This is the same rule as M0's `/readyz` checks, which catch `Exception` but never `BaseException`.

### 2.10 Nested `asyncio.timeout` and `.expired()`

The gateway (PR 6) wraps your function like this, in words:

- the whole logical call (all attempts, waits and the repair) runs inside one
  `asyncio.timeout(deadline)`;
- each single attempt runs inside its own `asyncio.timeout(attempt timeout)`. When that one fires,
  the gateway turns it into a `Transient`, so H2 may retry it.

What happens when one fires (measured on Python 3.12.15, 2026-10-14):

- Inside the scopes, the running code sees `CancelledError`, **whichever** timeout fired.
  `TimeoutError` appears only when the scope that fired exits.
- An inner scope lets an outer timeout's cancel pass through unchanged.
- After a scope exits, its `.expired()` says whether **that** timeout fired. In the experiment:
  inner fired → inner `True`, outer `False`; outer fired → inner `False`, outer `True`.

What this means for you:

- Your code never sees "the deadline" as a special exception, only `CancelledError`, which you let
  through (§2.9). PR 6 tells `deadline_exceeded` from `cancelled` after the scope exits, using
  `.expired()`.
- Why H2 still gets `deadline` and `clock`: so it does not **start** a wait it cannot finish. A
  sleep cut off by the deadline wastes the remaining time and loses the real error. Checking first
  lets you raise the last real error, with its request id.
- HTTP-client timeouts (connect, read, write, pool) limit each step, not the whole request: a
  server that trickles bytes slowly can keep a read alive. That is why the gateway has its own
  wall-clock limits.

### 2.11 Temperature and streaming (concepts only; approved playbook deviations)

**Temperature** is a sampling setting: low makes the model choose its most likely next token more
consistently, high makes the output more varied. The playbook has a hands-on exercise for it; we do
not. ADR 0006 says temperature is never sent: SDK 1.x removed it, and current models reject
non-default values (migration guide; approved 2026-10-14). `LLMRequest` has no `temperature` field,
so passing one is a validation error (see `test_types.py`). Consistency comes from clear prompts,
validated structured output and evals over many prompts, not from a setting.

**Streaming** sends the response as a sequence of events while it is being generated, instead of
all at once at the end. It improves the time until the first text appears, not the total time. Its
costs:

- a failure halfway through leaves an answer the user has already partly seen;
- validation can only run at the end;
- usage arrives last;
- a timeout must cover the gap between chunks as well as the total time.

The hands-on part moves to M8 (the customer widget), where users feel it. The M1 gateway does not
stream.

### 2.12 Omitted `thinking` = adaptive thinking, billed as output

We never send a `thinking` parameter. ADR 0006 records that leaving it out means **adaptive
thinking**: the model may think before it answers, steered by effort (we send `low` on every call).
Thinking tokens are **billed as output tokens** and are already inside `output_tokens`, so H1 never
adds them separately: that would count them twice. A short visible answer can therefore carry many
more output tokens than its text suggests; PR 9 measures this live. Thinking blocks stay in
`LLMResult.content` (with their signature, for replay later), and `LLMResult.text` leaves them out.

### 2.13 Preview for H3: the small-n percentile trap

For the 12 samples 1, 2, …, 12, Python's `statistics.quantiles` (run on 2026-10-14):

- the default (exclusive) method puts the 95th percentile at **12.35**, *above every sample*: it is
  extrapolated past the maximum;
- the inclusive method gives **11.45**: it stays inside the data, but it rests on the top two
  samples alone.

Larger n helps less than it seems. From n = 19 even the exclusive method stays inside the data, and
at n = 20 the inclusive p95 of 1, …, 20 is 19.05, essentially the 19th value. Inside the data is not
the same as precise: for n independent samples, the true p95 lies *above the sample maximum* with
probability 0.95ⁿ, about 54 % at n = 12 and still about 36 % at n = 20. Only from n = 59 does that
drop below 5 %.

ADR 0007's rules follow from this: use the inclusive method; report p95 only when n ≥ 20
**distinct** prompts (repeats of one prompt are not independent samples), and always next to n and
min/max, so a reader can see how little it rests on; count timed-out calls instead of dropping them
(dropping hides the slowest). H3 (`latency_summary`) comes with step 8, with its own stub, tests and
section here.

## 3. Test by test, with level-2 hints

Open a hint only for the test you are stuck on, and only after the concept section it points to.

### H1: `tests/gateway/test_pricing.py`

Made-up prices per MTok: standard $3 input, $7 output, $0.3 cache read, $11 5-minute write and $13
1-hour write. The long-prompt prices are ten times those. The tiered model switches tier when the
prompt is over **100** tokens. Every expected value has its working in a comment next to it.

- **`test_zero_usage_costs_exactly_zero`**: no tokens cost exactly `Decimal("0")`.
  <details><summary>Hint</summary>

  If the code is right for one token it is right for zero, with no special case. If you added one,
  check what type it returns (see the last H1 test).

  </details>
- **`test_each_class_is_charged_at_its_own_price`** (5 cases): 1,000,000 tokens of one class cost
  exactly that class's price; this catches a class priced from the wrong field, or forgotten.
  <details><summary>Hint</summary>

  The failing case's id names the class. Put `Usage` and `TokenPrices` side by side and pair each
  count with its price once, by name.

  </details>
- **`test_one_token_at_3_dollars_per_mtok_is_exact`**: the smallest amount, one input token at
  $3/MTok, comes out as exactly $0.000003. (On its own it cannot prove "no float": in float,
  3/1,000,000 happens to print as `3e-06`. The 18-digit test below does that.)
  <details><summary>Hint</summary>

  A `TypeError` means a float met a `Decimal` (§2.4): find it. Token counts are ints, and ints mix
  with `Decimal` exactly. Does your "one million" contain a decimal point or an exponent?

  </details>
- **`test_all_five_classes_are_added_up`**: all five classes at once (prompt 95, so standard) add
  up to $0.000814.
  <details><summary>Hint</summary>

  Compare your total with the working in the test's comment, term by term. The missing or doubled
  term is the bug.

  </details>
- **`test_realistic_counts_with_a_fractional_price_stay_exact`**: realistic counts with the $0.3
  cache-read price give exactly $0.0103413.
  <details><summary>Hint</summary>

  Exactness survives as long as nothing is converted or rounded: the prices already arrive as exact
  `Decimal`s.

  </details>
- **`test_a_price_with_more_digits_than_a_float_holds_stays_exact`**: a made-up price of
  $1.23456789123/MTok for 987,654 tokens gives exactly $1.21932591604487442, 18 significant digits,
  more than a float can hold. This is the test that catches a float anywhere on the way.
  <details><summary>Hint</summary>

  If the last two or three digits differ, a float was involved somewhere, even if you turned the
  result back into a `Decimal` at the end (§2.4): the digits were already lost. Look for `float(`,
  a literal with a decimal point, or `/` applied to something that is not a `Decimal`.

  </details>
- **`test_a_prompt_of_exactly_the_threshold_uses_standard_prices`**: a prompt of exactly 100
  tokens stays standard ($0.0003).
  <details><summary>Hint</summary>

  Reread the word the pricing page uses for the boundary (§2.5). Exactly 100 is not over 100.

  </details>
- **`test_a_prompt_one_token_over_the_threshold_uses_long_prompt_prices`**: 101 tokens switch to
  the long-prompt prices ($0.00303).
  <details><summary>Hint</summary>

  A result of $0.000303 means the long-prompt prices are never picked. Look at where they live in
  `LongPromptTier`: next to the threshold, one level down.

  </details>
- **`test_the_threshold_is_read_from_the_pricing`**: the same tiers with a threshold of 50, so 51
  tokens use the long-prompt prices ($0.00153).
  <details><summary>Hint</summary>

  If this one fails while the 100-token tests pass, the number 100 is in your code somewhere. The
  threshold is data (§2.5).

  </details>
- **`test_cache_tokens_count_toward_the_prompt_at_the_threshold`**: 40 input + 30 + 20 + 10 cache
  tokens make a prompt of exactly 100, so standard ($0.000479).
  <details><summary>Hint</summary>

  On its own this test proves little: it is the guard for the next one. If it fails, check the
  boundary rule (the exactly-100 test) first.

  </details>
- **`test_one_cache_token_pushes_the_prompt_over_the_threshold`** (3 cases): 100 input tokens plus
  one token of a single cache class make 101, so long-prompt prices for every class. This catches
  a prompt length that leaves out a cache class.
  <details><summary>Hint</summary>

  Reread the second sentence of the quote in §2.5. Which of the five `Usage` fields are "input
  tokens" in that sense? The failing case's id names the one you left out.

  </details>
- **`test_output_tokens_do_not_count_toward_the_prompt`**: 100 input + 1,000 output tokens stay
  standard ($0.0073).
  <details><summary>Hint</summary>

  The output is what the model wrote, not what you sent, so it is not part of the prompt.

  </details>
- **`test_the_long_prompt_tier_prices_output_too`**: over the threshold, output uses the
  long-prompt output price ($0.00373).
  <details><summary>Hint</summary>

  Choose the tier once for the whole request, then price every class from that one tier (§2.5).

  </details>
- **`test_a_single_tier_model_never_switches`**: 10 million prompt tokens on a model without
  `long_prompt` still pay standard prices ($30.8).
  <details><summary>Hint</summary>

  `long_prompt` can be `None`, and mypy will insist you handle that. With no long-prompt tier there
  is nothing to switch to, however long the prompt.

  </details>
- **`test_the_result_is_a_decimal`** (2 cases): the return value's type is exactly `Decimal`, for
  zero usage too.
  <details><summary>Hint</summary>

  Equality cannot tell `0` from `Decimal("0")` (§2.4), so this test checks the type. An int sneaks
  in when a branch returns a bare literal, or when nothing gets added at all, for example because
  zero-token classes are skipped.

  </details>

### H2: `backoff_delay` (first part of `tests/gateway/test_retry.py`)

The test policy is 2 retries, base 0.5 s and cap 8 s. So the bounds are 0.5, 1, 2 and 4 s for
retries 0–3, and 8 s from retry 4 on.

- **`test_backoff_with_rand_1_is_the_capped_exponential`** (7 cases): with `rand` = 1.0 the wait
  equals the bound exactly: 0.5, 1, 2, 4, 8, 8 and 8 s for attempts 0, 1, 2, 3, 4, 5 and 10.
  <details><summary>Hint</summary>

  With `rand` = 1.0 the jitter disappears, so this checks the bound alone. Attempt 0 must give the
  base itself: check where your doubling starts.

  </details>
- **`test_backoff_cap_applies_between_powers_of_two`**: with base 1 s and cap 3 s, attempt 2 waits
  3 s, not 4 s.
  <details><summary>Hint</summary>

  The cap need not be the base times a power of two. Compare the bound against the cap itself, not
  against the last power of two below it.

  </details>
- **`test_backoff_with_rand_0_is_zero`** (3 cases): with `rand` = 0.0 the wait is 0.0 at any
  attempt.
  <details><summary>Hint</summary>

  Full jitter's range starts at zero (§2.6), not at the base and not at half the bound.

  </details>
- **`test_backoff_grows_with_rand`**: a larger `rand` gives a longer wait at the same attempt.
  <details><summary>Hint</summary>

  If both waits are equal, `rand` is not used at all.

  </details>
- **`test_backoff_for_a_huge_attempt_is_capped_not_an_overflow`**: attempt 5000 returns the cap,
  not an `OverflowError`.
  <details><summary>Hint</summary>

  In the REPL, compute two to the power 5000 as a float and read the error. Once the bound has
  reached the cap, can a bigger exponent still change the answer?

  </details>
- **`test_backoff_returns_a_float`**: the result's type is exactly `float`.
  <details><summary>Hint</summary>

  The policy's delays are already floats (Pydantic converted them). Keep the arithmetic in floats
  and don't convert to an int.

  </details>

### H2: `call_with_retries` (rest of `tests/gateway/test_retry.py`)

The test helpers:

- `FakeTime`'s clock starts at 100.0. Its `sleep` records each wait and moves the clock forward;
  nothing really waits.
- `Script` plays its outcomes in order: it raises the exceptions and returns the text. Each call
  can take `takes_s` seconds on the fake clock, and a call beyond the script fails the test.
- `run` passes `rand` = 1.0, so every backoff wait equals its bound, and a deadline of 60 s unless
  the test sets one.

The tests, in file order:

- **`test_success_on_the_first_try_calls_once_and_never_sleeps`**: returns the answer after one
  call and no wait.
  <details><summary>Hint</summary>

  The happy path needs no clock, no sleep and no `rand`.

  </details>
- **`test_a_transient_error_then_success_retries_once`**: one `Transient`, a 0.5 s wait, then the
  answer.
  <details><summary>Hint</summary>

  The wait before the first retry uses retry number 0: `backoff_delay` counts the retries already
  made.

  </details>
- **`test_two_transient_errors_then_success_uses_both_retries`**: two `Transient`s, waits of 0.5 s
  and 1.0 s, then the answer.
  <details><summary>Hint</summary>

  Waits of 0.5 and 0.5 mean the retry number never moves on between waits.

  </details>
- **`test_when_retries_run_out_the_last_transient_error_is_raised`**: three `Transient`s with 2
  retries make 3 calls and waits of 0.5 s and 1.0 s, then the **third** error object itself is
  raised.
  <details><summary>Hint</summary>

  There is no wait after the last failure, because no retry is left to wait for. Raise the very
  object you caught: not a new one, and not the first one.

  </details>
- **`test_a_provider_error_marked_retryable_is_retried`**: a `ProviderError` with
  `retryable=True` (from `x-should-retry: true`) is retried like a `Transient`, after a 0.5 s
  backoff.
  <details><summary>Hint</summary>

  Decide by `retryable`, which every `GatewayError` has, not by class (§2.8). A `ProviderError`
  has no `retry_after`, so it gets the backoff.

  </details>
- **`test_a_non_retryable_error_is_raised_after_exactly_one_call`** (8 cases): a spend limit,
  billing, auth, invalid request, default provider error, refusal, truncation, and a `Transient`
  whose `retryable` is False each give 1 call, no wait, and the same object raised.
  <details><summary>Hint</summary>

  The last case is a `Transient` that must **not** be retried: checking the class instead of
  `retryable` fails exactly that case.

  </details>
- **`test_an_exception_that_is_not_a_gateway_error_propagates_after_one_call`**: a `ValueError` (a
  bug) is never retried.
  <details><summary>Hint</summary>

  Catch only what you can handle. A bug retried twice is still a bug, just slower and harder to
  see.

  </details>
- **`test_retry_after_is_waited_for_instead_of_the_backoff`**: a `Transient` with `retry_after`
  3.0 waits exactly 3.0 s, not 0.5 s.
  <details><summary>Hint</summary>

  Only a `Transient` has `retry_after`, and it may be `None` (§2.7). When it is set, it replaces
  the backoff, with no jitter.

  </details>
- **`test_a_retry_after_wait_still_counts_as_a_retry_for_the_backoff`**: a Retry-After wait of 3 s,
  then a backoff of 1.0 s (retry 1), not 0.5 s.
  <details><summary>Hint</summary>

  A retry is a retry, whichever rule chose its wait. If the second wait is 0.5 s, your retry number
  only moves on when a backoff was used.

  </details>
- **`test_retry_after_equal_to_the_cap_is_still_waited_for`**: a `retry_after` equal to the cap
  (8.0) is waited for.
  <details><summary>Hint</summary>

  "Above the cap" means strictly above; equal is allowed.

  </details>
- **`test_retry_after_above_the_cap_is_not_retried`**: a `retry_after` of 8.5 s (above the cap)
  raises at once: 1 call, no wait.
  <details><summary>Hint</summary>

  Don't shorten it to the cap: retrying before the server's time is up only earns another 429
  (§2.7). Give up instead.

  </details>
- **`test_the_first_attempt_is_made_even_when_the_deadline_has_passed`**: a deadline already in
  the past still gets one call, and its answer is returned.
  <details><summary>Hint</summary>

  The deadline rule in the contract is about *waits* (point 5), and the first attempt has none.
  The gateway's own `asyncio.timeout` stops work that runs too long (§2.10).

  </details>
- **`test_a_backoff_that_would_end_past_the_deadline_is_not_slept`**: with the deadline 0.3 s
  away, a 0.5 s wait is not started; the error is raised.
  <details><summary>Hint</summary>

  Before you sleep, compare when the wait would **end** with the deadline. Both are on `clock()`'s
  scale (§2.10).

  </details>
- **`test_a_retry_after_that_would_end_past_the_deadline_is_not_slept`**: the same rule for a
  Retry-After wait (3 s wanted, 2 s left).
  <details><summary>Hint</summary>

  One deadline check covers every wait, whichever rule chose its length.

  </details>
- **`test_a_wait_ending_exactly_at_the_deadline_is_not_slept`**: a wait that would end exactly at
  the deadline is not slept.
  <details><summary>Hint</summary>

  The contract says strictly *before* the deadline: a retry that starts with no time left cannot
  finish.

  </details>
- **`test_the_clock_is_read_after_the_failed_attempt`**: the attempt itself takes 0.4 s, so a
  0.5 s wait would end past a deadline 0.8 s from the start.
  <details><summary>Hint</summary>

  Read the clock when you need it, after the failure, not once at the start: attempts take time.

  </details>
- **`test_the_deadline_can_stop_a_later_retry`**: the first wait fits before the deadline, the
  second does not, so the **second** error is raised after one wait.
  <details><summary>Hint</summary>

  The deadline check runs before every wait, not just the first, and the error raised is always the
  latest one.

  </details>
- **`test_cancelled_error_from_the_call_propagates`**: a `CancelledError` from the call
  propagates after 1 call and no wait.
  <details><summary>Hint</summary>

  Which `except` clauses catch a `BaseException` (§2.9)? Make sure none of yours do.

  </details>
- **`test_cancelled_error_during_the_wait_propagates`**: a cancel while sleeping propagates, and no
  further attempt is made.
  <details><summary>Hint</summary>

  The sleep is part of your loop too. If it sits inside a `try`, the same rule applies there.

  </details>

## 4. The hint ladder and time-boxes

| Level | What | Where |
|---|---|---|
| 1 | The concepts | §2 of this file: read first |
| 2 | One short hint per test, no code | §3 of this file, one `<details>` at a time |
| 3 | Pseudo-code | On request only, in chat with Diego or Claude; never in this file |
| 4 | Pairing review: we go through your code together, and you type | On request only |

**Time-boxes** (agreed in M1 planning, 2026-10-14): H1 **2 h**, H2 **3 h**. The clock starts
when you start writing code; reading this guide is not counted (decided 2026-10-14). When the
time is up and you are still stuck, go one level up the ladder. Still stuck after level 4: extend
M1 or cut PR 8's scope, never quiet delegation. If you ask Claude to write a piece, Claude reminds
you of the learning goal once, respects your decision and records it in the hand-off; the
explain-back stays part of acceptance whoever typed the code. Proposal: note in your PR which
levels you used. It helps size H3.

## 5. Explain it back (part of acceptance)

**H1: cost per call**
- Name the five token classes. Which ones count toward the prompt length, and why doesn't output?
- Why `Decimal` and not `float`? Why is `Decimal(0.1)` wrong and `Decimal("0.1")` right, and what
  does that have to do with `parse_float=Decimal`?
- State the tier rule from the quote: what does "over" mean, what counts, and what does the tier
  change? Why is a retried request priced on its own?
- Why does a model without a price stop startup instead of costing $0?

**H2: retry with backoff**
- What is the thundering herd, and how does full jitter break it? Why is there a cap?
- `SpendLimitReached` can arrive as a 429, so why is it never retried? Why is a `ValueError` never
  retried?
- Why must `CancelledError` never be caught? What would break in the gateway's `asyncio.timeout` if
  it were?
- The gateway already has an `asyncio.timeout` around your function, so why does your function
  still check the deadline before sleeping?
- Why does a Retry-After above the cap mean "give up" rather than "wait the cap"?
