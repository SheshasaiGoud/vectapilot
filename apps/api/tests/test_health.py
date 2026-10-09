from fastapi.testclient import TestClient


def test_healthz_is_ok_even_when_dependencies_are_down(client: TestClient) -> None:
    # The test settings point at unreachable Postgres/Redis on purpose.
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
