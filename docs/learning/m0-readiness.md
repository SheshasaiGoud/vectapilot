# M0 hand-write: the `/readyz` readiness checks

**Your task:** implement `check_postgres`, `check_redis` and `readyz` in
[`apps/api/src/vectapilot_api/health.py`](../../apps/api/src/vectapilot_api/health.py) so every
test in [`apps/api/tests/test_readyz.py`](../../apps/api/tests/test_readyz.py) passes.
Expected size: about 30–40 lines. Everything else (settings, app factory, clients, wiring) is done.

## 1. The concepts

**Liveness vs readiness.** An orchestrator (Docker, Kubernetes, a load balancer) asks two
different questions:

| Probe | Question | If it fails… | Checks dependencies? |
|---|---|---|---|
| `/healthz` (liveness) | Is the process alive and able to answer? | Restart the container | **No** — a database outage is not fixed by restarting the API |
| `/readyz` (readiness) | Should traffic be sent here right now? | Stop sending traffic until it recovers | **Yes** — Postgres and Redis |

**Timeouts.** A probe that hangs is worse than one that fails: the orchestrator waits, traffic
piles up. Every check must give up after `timeout_seconds`.

**Concurrency.** Two checks of 1 s each should take ~1 s, not 2 s. Start them together and
wait for both.

**Fail closed, never crash.** A check that raises must become a failed result, not a 500 error.
The probe's job is to *report* problems.

**Don't leak secrets.** Health endpoints are often public. Exception messages can contain hosts,
users or even passwords from the connection URL. Report only something safe, such as the
exception's class name (`type(error).__name__`).

**Dependency injection.** `readyz` doesn't create its own database clients; FastAPI passes it a
list of check functions via `Depends(get_readiness_checks)`. That's why the tests can replace the
real checks with fakes (`app.dependency_overrides`) and test your endpoint logic in milliseconds,
without a database.

## 2. Useful building blocks (look them up — reading docs is part of the exercise)

- Postgres: `async with engine.connect() as conn:` then `await conn.execute(text("SELECT 1"))`
  (`from sqlalchemy import text`).
- Redis: `await client.ping()`.
- Timeouts: `asyncio.timeout(...)` (Python 3.11+) or `asyncio.wait_for(...)`. Note what exception
  a timeout raises.
- Running things concurrently: `asyncio.gather(...)` or `asyncio.TaskGroup`. Think about what
  happens to the *other* checks when one raises — `return_exceptions=True` exists for a reason.
- Status codes: `JSONResponse(content=..., status_code=503)`.

## 3. How to work

```bash
cd apps/api
uv run pytest tests/test_readyz.py -v          # see what is expected
# write the code…
uv run pytest -q && uv run mypy && uv run ruff check .
```

1. Create your branch: `git switch -c m0/readyz` (after PR 1 is merged into main).
2. When your tests start **passing**, pytest reports them as failures (`XPASS(strict)`) — that is
   the signal to **delete the `pytestmark = ...` line** at the top of `test_readyz.py`.
3. Integration tests (real Postgres/Redis) run in CI automatically. Locally, after PR 2 adds
   Docker Compose: `make up`, then `RUN_INTEGRATION=1 uv run pytest`.
4. Commit with your own message (e.g. `feat(api): implement readiness checks`), push, open a PR.
   Ask `@agent-diego-backend` (or Claude) to review it — reviewers must not rewrite it for you.

## 4. Check yourself afterwards
- Why must `/healthz` not check the database?
- What would happen to `/readyz` if one check hung forever and you had no timeout?
- Why does the test for an unreachable database also assert that `not-a-real-secret` is absent?
