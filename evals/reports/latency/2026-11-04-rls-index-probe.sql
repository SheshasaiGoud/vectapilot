-- RLS index probe (2026-11-04): may the app role use an HNSW index for ORDER BY <=> and a GIN
-- index for WHERE @@ when row-level security is forced? A temp table, one transaction, ROLLBACK:
-- nothing persists. Run as vectapilot_app (not a superuser: superusers always skip RLS).
\set ON_ERROR_STOP on
BEGIN;
SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;
SELECT version();
SELECT extversion AS pgvector FROM pg_extension WHERE extname = 'vector';
CREATE TEMP TABLE probe (id int, business_id text, e vector(3), tsv tsvector);
INSERT INTO probe
SELECT g, CASE WHEN g % 2 = 0 THEN 'a' ELSE 'b' END,
       ARRAY[random(), random(), random()]::vector(3),
       to_tsvector('simple', 'word' || (g % 50)::text || ' common')
FROM generate_series(1, 5000) AS g;
CREATE INDEX probe_hnsw ON probe USING hnsw (e vector_cosine_ops);
CREATE INDEX probe_gin ON probe USING gin (tsv);
ANALYZE probe;
ALTER TABLE probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE probe FORCE ROW LEVEL SECURITY;
CREATE POLICY probe_business ON probe USING (business_id = current_setting('probe.business', true));
SELECT set_config('probe.business', 'a', true);
SET LOCAL enable_seqscan = off;
\echo '--- ORDER BY <=> under forced RLS (HNSW)'
EXPLAIN (COSTS OFF) SELECT id FROM probe WHERE business_id = 'a' ORDER BY e <=> '[1,0,0]' LIMIT 5;
SHOW hnsw.ef_search;
SHOW hnsw.iterative_scan;
\echo '--- WHERE @@ under forced RLS (GIN)'
EXPLAIN (COSTS OFF) SELECT id FROM probe WHERE business_id = 'a' AND tsv @@ 'word7'::tsquery;
\echo '--- control: same @@ query with RLS off for this table'
ALTER TABLE probe NO FORCE ROW LEVEL SECURITY;
ALTER TABLE probe DISABLE ROW LEVEL SECURITY;
EXPLAIN (COSTS OFF) SELECT id FROM probe WHERE business_id = 'a' AND tsv @@ 'word7'::tsquery;
ROLLBACK;
