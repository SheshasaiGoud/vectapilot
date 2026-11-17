"""M7 PR 2: the suite's database environment works end to end (integration: real Postgres).

An **unguarded baseline**, not a red-team result (no guard exists in PR 2): `rt-shop` is created
from the dataset fixtures and its sandbox seeded (`served.py`), and frozen cases' scripts play
through `POST /v1/agent` on the real app, as the app role, with the replay standing in for M5's
H1 and the fake provider scripted with the same steps. It proves what PR 6's runners will stand
on: the fixture business is served, its business lookup passes, the replay reaches the model
(the fake was called), a read tool sees the seed as the case's own customer (another customer's
order would be `not_found`), and the scripted reply comes back unchanged.
"""

from collections.abc import Iterator

import pytest

from tests.conftest import integration
from tests.redteam.served import ServedSuite, replay_service, served_suite, serving
from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.redteam.harness import fill

pytestmark = [integration]


@pytest.fixture(scope="module")
def redteam_app() -> Iterator[ServedSuite]:
    yield from served_suite()


def test_rt_shop_is_served_and_a_cases_script_replays_through_the_route(
    redteam_app: ServedSuite,
) -> None:
    case = CASES["l-006"]  # a benign hours question on rt-shop, answered by one scripted reply
    filled = fill(case, GENERATOR)
    service, provider = replay_service(redteam_app, filled)

    with serving(redteam_app, service):
        response = redteam_app.client.post(
            "/v1/agent",
            json={"message": filled.input, "customer_ref": redteam_app.customer_for(case)},
            headers={BUSINESS_HEADER: case.business_id},
        )

    assert response.status_code == 200, response.status_code
    body = response.json()
    assert body["kind"] == "reply"
    assert len(provider.calls) == 1  # the positive control: the replay reached the model
    step = case.fixture.scripted_model[0]
    assert step.action == "reply"
    assert body["text"] == step.text


def test_a_read_tool_sees_the_seed_as_the_cases_own_customer(redteam_app: ServedSuite) -> None:
    case = CASES["c-003"]  # RT-0414 belongs to cust-002 in rt-shop's seed
    filled = fill(case, GENERATOR)
    service, provider = replay_service(redteam_app, filled)
    customer = redteam_app.customer_for(case)

    with serving(redteam_app, service):
        response = redteam_app.client.post(
            "/v1/agent",
            json={"message": filled.input, "customer_ref": customer},
            headers={BUSINESS_HEADER: case.business_id},
        )

    assert customer == "cust-002"
    assert response.status_code == 200, response.status_code
    assert response.json()["kind"] == "reply"
    assert len(provider.calls) == 2  # the read, then the reply
    # The second call's prompt holds the seed's order as observed (no guard in PR 2): the
    # status the seed gives RT-0414, never not_found.
    second = provider.calls[1].messages[-1].content
    assert "Example kitchen scale" in second and "not_found" not in second
