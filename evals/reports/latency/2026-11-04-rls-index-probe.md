# RLS and indexes: may the app role use HNSW or GIN? (2026-11-04, local)

**local Docker on an 8 GB Mac.** A plan-shape check, not a timing: it records which plan Postgres
chooses, never how fast it is. It corrects ADR 0008's 2026-11-02 amendment, which said neither
index could be used under RLS.

## Why

M3 PR 8 (`2026-11-02-local.md`) found no index evaluating `<=>` or `@@`, because none exists
(exact search, ADR 0008). Whether an HNSW or GIN index *would* be used by `vectapilot_app` under
row-level security was a claim, not a measurement. This probe measures it.

## How

- Role `vectapilot_app`: not a superuser, no `BYPASSRLS` (superusers always skip RLS).
- One transaction ending in `ROLLBACK`. The table is a `TEMP` table, so nothing persists and no
  application table is touched.
- 5,000 rows, two businesses (`a`, `b`, half each), random 3-dimensional vectors, `simple`
  tsvectors. An HNSW index (`vector_cosine_ops`) and a GIN index, `ANALYZE`, then RLS enabled and
  forced with a policy shaped like the app's (`business_id = current_setting(...)`), set per
  transaction.
- `enable_seqscan = off`, so the planner picks any index it is allowed to use.
- Control: the same `@@` query with RLS disabled on the table.
- Commit `4f018dd`. PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2), pgvector 0.8.7. `hnsw.ef_search` 40,
  `hnsw.iterative_scan` off (the defaults).

Run it with:

```bash
docker compose -f infra/compose.yaml exec -T -e PGPASSWORD="${VECTAPILOT_APP_DB_PASSWORD:-vectapilot-app-dev}" postgres psql -h 127.0.0.1 -U vectapilot_app -d vectapilot -X -q < evals/reports/latency/2026-11-04-rls-index-probe.sql
```

## Result

| query, RLS forced, as `vectapilot_app` | plan | index used? |
|---|---|---|
| `ORDER BY e <=> '[1,0,0]' LIMIT 5` | `Index Scan using probe_hnsw`, `Order By: (e <=> ...)`, `Filter: (business_id = 'a')` | **HNSW: yes** |
| `WHERE tsv @@ 'word7'` | `Seq Scan` (marked `Disabled: true`: chosen although seq scans are off) | **GIN: no** |
| control: the same `@@` query, RLS disabled | `Bitmap Index Scan on probe_gin` | GIN: yes |

## What it means

- **`@@` (GIN): confirmed unusable under RLS.** `@@` is a `WHERE` condition and is not leakproof,
  so Postgres will not evaluate it inside an index ahead of the RLS policy. The control shows the
  same query uses the GIN index once RLS is off. A GIN index on `chunks.tsv` would not help
  `text_search` as the app role, unless the operator's function were marked leakproof (a
  superuser-only change to a core function, which this project does not make).
- **`<=>` (HNSW): usable under RLS.** `ORDER BY <=>` is an ordering, not a condition, so the
  leakproof rule does not apply to it. The index produces rows in distance order, and the business
  filter (the query's own clause and the policy) runs **after** the index scan. With
  `hnsw.ef_search` 40 and iterative scans off, a business holding a small share of the rows can get
  **fewer than k** hits: the scan's candidates are mostly other businesses' rows, which the filter
  then drops. pgvector 0.8's iterative index scans (`hnsw.iterative_scan`) or per-business partial
  indexes are the options to weigh then.
- Nothing changes now: exact search stays (ADR 0008's revisit trigger is unchanged). The probe only
  corrects what the trigger's design work starts from.

## Output (verbatim)

```text
  current_user  | rolsuper | rolbypassrls 
----------------+----------+--------------
 vectapilot_app | f        | f
(1 row)

                                                             version                                                              
----------------------------------------------------------------------------------------------------------------------------------
 PostgreSQL 18.6 (Debian 18.6-1.pgdg12+2) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit
(1 row)

 pgvector 
----------
 0.8.7
(1 row)

 set_config 
------------
 a
(1 row)

--- ORDER BY <=> under forced RLS (HNSW)
                                      QUERY PLAN                                      
--------------------------------------------------------------------------------------
 Limit
   ->  Result
         One-Time Filter: (current_setting('probe.business'::text, true) = 'a'::text)
         ->  Index Scan using probe_hnsw on probe
               Order By: (e <=> '[1,0,0]'::vector)
               Filter: (business_id = 'a'::text)
(6 rows)

 hnsw.ef_search 
----------------
 40
(1 row)

 hnsw.iterative_scan 
---------------------
 off
(1 row)

--- WHERE @@ under forced RLS (GIN)
                                   QUERY PLAN                                   
--------------------------------------------------------------------------------
 Result
   One-Time Filter: (current_setting('probe.business'::text, true) = 'a'::text)
   ->  Seq Scan on probe
         Disabled: true
         Filter: ((business_id = 'a'::text) AND (tsv @@ '''word7'''::tsquery))
(5 rows)

--- control: same @@ query with RLS off for this table
                    QUERY PLAN                     
---------------------------------------------------
 Bitmap Heap Scan on probe
   Recheck Cond: (tsv @@ '''word7'''::tsquery)
   Filter: (business_id = 'a'::text)
   ->  Bitmap Index Scan on probe_gin
         Index Cond: (tsv @@ '''word7'''::tsquery)
(5 rows)

```
