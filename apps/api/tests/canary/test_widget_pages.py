"""Framing headers per business (M8 PR 2b; D3 (a), D12 (b), S9, ADR 0019 decisions 4 and 8): the
widget page of one business may be framed only by that business's own origins. Unit (files only,
no database, no Redis), kept under tests/canary/ so the canary gate's no-skip rule covers it.

The real app serves both sample businesses' widget files and a TEST-ONLY build
(`tests/ui/helpers.py`). For each business, its page's `frame-ancestors` is exactly its own
`allowed_origins` and names no origin of the other business, and its `connect-src` is the
widget's own routes only. The positive control: the two businesses' origins differ, so the check
could see a page that took the other's. The dashboard is framed by no one.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from tests.ui.helpers import write_ui
from tests.widget.helpers import BUSINESSES, ORIGIN, WIDGET_DIR, read_json, widget_settings
from tests.widget.served import Served, serve

CSP: Final = "content-security-policy"


def origins_of(business_id: str) -> set[str]:
    data = read_json(WIDGET_DIR / "businesses" / f"{business_id}.json")
    return {str(origin) for origin in data["allowed_origins"]}


def directive(header: str, name: str) -> list[str]:
    [value] = [part.strip() for part in header.split(";") if part.strip().startswith(f"{name} ")]
    return value.split(" ")[1:]


@pytest.fixture
def served(tmp_path: Path) -> Iterator[Served]:
    with serve(widget_settings(ui_dir=write_ui(tmp_path / "ui"))) as app:
        yield app


def test_each_widget_page_is_framed_only_by_its_own_businesss_origins(served: Served) -> None:
    first, second = BUSINESSES
    assert origins_of(first) and origins_of(second)
    assert not origins_of(first) & origins_of(second)  # the positive control: they differ

    for business_id in BUSINESSES:
        response = served.client.get(f"/widget/{business_id}")
        assert response.status_code == 200, business_id
        ancestors = set(directive(response.headers[CSP], "frame-ancestors"))
        others = set().union(*(origins_of(b) for b in BUSINESSES if b != business_id))
        assert ancestors == origins_of(business_id), business_id
        assert not ancestors & others, business_id
        assert directive(response.headers[CSP], "connect-src") == [f"{ORIGIN}/v1/widget/"]


def test_the_dashboard_is_framed_by_no_business(served: Served) -> None:
    response = served.client.get("/staff/")

    assert directive(response.headers[CSP], "frame-ancestors") == ["'none'"]
    assert response.headers["x-frame-options"] == "DENY"
    every = set().union(*(origins_of(b) for b in BUSINESSES))
    assert not any(origin in response.headers[CSP] for origin in every)
