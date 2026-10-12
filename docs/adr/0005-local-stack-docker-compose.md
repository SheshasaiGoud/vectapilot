# ADR 0005 — Local stack with Docker Compose

- **Status:** accepted · 2026-10-12

**Context.** M0 needs Postgres (with pgvector), Redis and the API running the same way on the
human's Mac (8 GB RAM, Apple Silicon) and in CI, with one command, and with database schema
changes applied reproducibly.

**Options.** (a) Install Postgres/Redis with Homebrew; (b) Docker Compose; (c) a hosted dev
database. For observability: (i) self-host Langfuse now (as the playbook's M0 suggests);
(ii) defer the decision to M1.

**Decision.** (b) Docker Compose in `infra/compose.yaml`, driven by a root `Makefile`
(`make up`, `make test-integration`, …):
- `pgvector/pgvector:pg18` and `redis:8-alpine`, ports published on **127.0.0.1 only**.
- A one-off `migrate` service runs `alembic upgrade head`; the API starts only if it succeeds
  (`service_completed_successfully`). It reuses the API image (building it twice raced).
- The API image (`apps/api/Dockerfile`) is multi-stage: uv builds a virtualenv; the runtime image
  has no build tools, runs as a non-root user, cannot modify its own code, and has a `/healthz`
  healthcheck. 68 MB.
- Postgres 18 stores data in `/var/lib/postgresql/18/docker`, so the volume mounts
  `/var/lib/postgresql` (not the older `…/data` path most tutorials show).
- Local-only default credentials, overridable in `infra/.env`; never used outside this stack.
- (ii) Langfuse is **not** in M0: a self-hosted v3 needs ~6 more containers including ClickHouse,
  too heavy for 8 GB next to everything else. Decided in M1 with the LLM gateway.

**Consequences.** One command brings up a production-like stack; CI uses the same images as
service containers and builds the API image on every PR. Requires Docker Desktop running locally.
Row-level security (ADR 0004) will need the API to connect as a non-owner role — added with the
first business-owned table in M2.
