# ADR 0004 — Keeping businesses isolated in a multi-business platform

- **Status:** accepted · 2026-10-12 · chosen by shesha (business-context §9, Q2)

**Context.** VectaPilot serves many businesses (clinics, vet clinics, pet stores, online shops…).
Each business's configuration, knowledge sources, conversations and data must never leak to, or
influence answers for, another business. The playbook warns not to claim multi-business support
unless isolation is tested. Leaks can happen in the database, in vector search, in caches, in
prompts, in tools and in logs — not only in SQL queries.

**Options.**

| | Option | Isolation strength | Cost / complexity |
|---|---|---|---|
| (a) | Shared tables + `business_id`, filtered in application code only | Weakest — one forgotten filter leaks | Simplest |
| (b) | (a) **+ Postgres row-level security** (RLS) keyed on the current business | Strong — the database refuses cross-business rows even if code forgets | Low: policies + setting the business per connection |
| (c) | A separate schema per business | Strong | Migrations run once per business; harder vector/eval tooling |
| (d) | A separate database per business | Strongest | Highest operating cost; overkill for a pilot |

**Decision.** (b): every business-owned table has a non-null `business_id`; RLS policies
enforce it; the API sets the current business on each database connection; vector search includes
the `business_id` filter inside the query. The same scoping applies to files, caches, prompts,
tool credentials and logs (business-context §5). A cross-business "canary" leakage test suite runs
in CI and blocks merges on any leak.

**Consequences.** One database and one set of migrations; strong isolation even against coding
mistakes. Requires care with connection pooling (the business must be set per request/transaction)
and with filtered vector-index performance (to be measured in M2–M3). Can move a large customer to
option (c)/(d) later if a business demands it.
