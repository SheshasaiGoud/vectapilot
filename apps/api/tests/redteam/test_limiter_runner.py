"""M7 PR 5: the limiter path's runner (`redteam/limiter.py`), registered for `CasePath.LIMITER`.

k-002 and k-004 themselves run against real Redis through `test_cases.py` (integration, the
database environment); these tests pin what needs no Redis.
"""

import asyncio

import pytest
from redis.asyncio import Redis

from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.redteam.harness import RUNNERS, RunEnv, fill
from vectapilot_api.redteam.limiter import (
    ISOLATION_DB,
    NOT_ISOLATED,
    POLICY,
    _run,
    isolated_redis_url,
    keys_of,
    require_isolated,
    run_limiter,
)
from vectapilot_api.redteam.plan import CasePath, path_of


def test_the_runner_is_registered_for_k_002_and_k_004_only() -> None:
    assert RUNNERS[CasePath.LIMITER] is run_limiter
    assert sorted(c.id for c in CASES.values() if path_of(c) is CasePath.LIMITER) == [
        "k-002",
        "k-004",
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://:secret@127.0.0.1:6379/0", "redis://:secret@127.0.0.1:6379/15"),
        ("redis://localhost:6379", "redis://localhost:6379/15"),
        ("redis://localhost:1/3?socket_timeout=1", "redis://localhost:1/15?socket_timeout=1"),
        # redis-py lets `db` in the query override the path [diego's PR 5 review]: dropped.
        ("redis://127.0.0.1:6379/0?db=0", "redis://127.0.0.1:6379/15"),
        ("redis://h:6379?db=3&socket_timeout=1", "redis://h:6379/15?socket_timeout=1"),
        ("rediss://h:6380/0?DB=0", "rediss://h:6380/15"),
    ],
)
def test_the_isolated_url_replaces_only_the_database(url: str, expected: str) -> None:
    assert isolated_redis_url(url) == expected
    assert ISOLATION_DB == 15
    client = Redis.from_url(isolated_redis_url(url))  # parsing only: no connection
    assert client.connection_pool.connection_kwargs["db"] == ISOLATION_DB
    require_isolated(client)


@pytest.mark.parametrize(
    "url", ["unix:///tmp/redis.sock?db=0", "unix:///tmp/redis.sock", "http://h:6379/0"]
)
def test_the_isolated_url_refuses_a_socket_or_another_scheme(url: str) -> None:
    with pytest.raises(ValueError, match="redis://"):
        isolated_redis_url(url)


@pytest.mark.parametrize("url", ["redis://127.0.0.1:1/0", "redis://127.0.0.1:1/15?db=0"])
def test_a_client_off_the_isolated_database_is_refused(url: str) -> None:
    # The parsed `db` decides, never the URL's path: `?db=0` wins over `/15` in redis-py.
    with pytest.raises(RuntimeError, match=NOT_ISOLATED):
        require_isolated(Redis.from_url(url))


def test_the_runner_refuses_the_apps_database_before_any_command() -> None:
    # Port 1 is dead: had the runner sent a DEL first, this would be a ConnectionError instead.
    with pytest.raises(RuntimeError, match=NOT_ISOLATED):
        asyncio.run(_run("k-002", "cross-business-limit", "rt-shop", "redis://127.0.0.1:1/0", ()))


def test_the_runner_refuses_to_run_outside_the_database_environment() -> None:
    case = CASES["k-002"]
    with pytest.raises(ValueError, match="database environment"):
        run_limiter(fill(case, GENERATOR), RunEnv(GENERATOR, "in_process"))
    with pytest.raises(ValueError, match="database environment"):
        run_limiter(fill(case, GENERATOR), RunEnv(GENERATOR, "database", object()))


def test_the_runner_deletes_only_its_own_keys() -> None:
    keys = keys_of(["paws-and-co"])
    assert len(keys) == 5  # two rate windows and three days' budget keys
    assert all(key.startswith("vp:paws-and-co:") for key in keys)
    assert POLICY.business.limit > POLICY.customer.limit >= 1
