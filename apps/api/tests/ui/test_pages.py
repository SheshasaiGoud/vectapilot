"""The product pages from `ui_dir` (M8 PR 2b; D2 (a) with (i), D3 (a), D12 (b), S9, S13, S14,
ADR 0019 decision 4). Unit: the real app on a TEST-ONLY build (`tests/ui/helpers.py`), the
committed widget folder, no database and no Redis (a page reads neither: S14).

- **The widget page** per business: `widget.html` with S9's headers, `frame-ancestors` exactly
  that business's `allowed_origins` and, under D12 (b), `connect-src` exactly
  `<public_origin>/v1/widget/`.
- **The dashboard**: `staff.html` and `staff/login.html`, never framed (`frame-ancestors 'none'`,
  `X-Frame-Options: DENY`), with `Cross-Origin-Opener-Policy: same-origin` [proposal].
- **Assets** from `_next/static/` and `icon.svg`, with `nosniff`.
- **Every miss is one 404**: `ui_dir` unset, an unknown business, a disabled widget, a page the
  build lacks, a page name outside the pattern, and every path-traversal form (encoded `..`, a
  dotfile, an empty segment, a NUL, a symbolic link out of `ui_dir`), each next to a file it could
  have reached (the positive control that the traversal had a target).
- **Startup:** a `ui_dir` that is not a folder stops the app; `ui_dir` without `public_origin` is
  refused by Settings.
- **S13:** an existing route carries none of these headers.
"""

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings
from tests.ui.helpers import (
    ICON_SVG,
    LOGIN_HTML,
    SCRIPT,
    SCRIPT_PATH,
    STAFF_HTML,
    WIDGET_HTML,
    write_ui,
)
from tests.widget.helpers import BUSINESSES, ORIGIN, WIDGET_DIR, read_json, widget_settings
from tests.widget.served import Served, serve
from vectapilot_api.config import UI_DIR_NEEDS_ORIGIN
from vectapilot_api.main import create_app
from vectapilot_api.ui.pages import (
    MAX_SEGMENTS,
    PAGE_CACHE,
    STAFF_HEADERS,
    UiDirError,
    UiFiles,
    content_security_policy,
    load_ui,
    widget_headers,
)
from vectapilot_api.widget.config import WidgetRegistry

NOT_FOUND: Final = {"detail": "Not Found"}
SECRET: Final = b"TEST-ONLY outside ui_dir: never served"
PAGE_PATHS: Final = ("/widget/paws-and-co", "/staff/", "/staff/login", SCRIPT_PATH, "/icon.svg")


def origins_of(business_id: str) -> list[str]:
    origins = read_json(WIDGET_DIR / "businesses" / f"{business_id}.json")["allowed_origins"]
    assert isinstance(origins, list) and origins
    return [str(origin) for origin in origins]


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    """The TEST-ONLY build inside `tmp_path`, with a file beside it (outside `ui_dir`) that every
    traversal below aims at."""
    (tmp_path / "secret.txt").write_bytes(SECRET)
    return write_ui(tmp_path / "ui")


@pytest.fixture
def served(ui_dir: Path) -> Iterator[Served]:
    with serve(widget_settings(ui_dir=ui_dir)) as app:
        yield app


def policy(header: str) -> dict[str, str]:
    """A CSP header's directives, by name."""
    directives = [part.strip().split(" ", 1) for part in header.split(";")]
    return {name: value for name, value in directives}


# ── The widget page ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("business_id", BUSINESSES)
def test_the_widget_page_is_framed_only_by_its_businesss_origins_and_calls_only_the_widget(
    served: Served, business_id: str
) -> None:
    response = served.client.get(f"/widget/{business_id}", params={"lang": "en"})

    assert response.status_code == 200 and response.content == WIDGET_HTML
    assert response.headers["content-type"].startswith("text/html")
    directives = policy(response.headers["content-security-policy"])
    assert directives["frame-ancestors"] == " ".join(origins_of(business_id))
    assert directives["connect-src"] == f"{ORIGIN}/v1/widget/"  # D12 (b)
    assert directives["default-src"] == "'none'" and directives["base-uri"] == "'none'"
    assert directives["form-action"] == "'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    # zara's PR 2 review: every load revalidates, so a removed framing origin is gone at once.
    assert response.headers["cache-control"] == "no-cache"
    assert "x-frame-options" not in response.headers  # framing is the CSP's, per business
    assert (
        response.headers["content-security-policy"]
        == widget_headers(ORIGIN, origins_of(business_id))["Content-Security-Policy"]
    )
    # S14: a page view opens no session and spends no window.
    assert served.limiter.calls == [] and served.app.state.database.opened == []


def test_the_whole_widget_policy_is_s9s_with_the_two_narrowed_directives() -> None:
    assert content_security_policy(connect_src="X", frame_ancestors="Y") == (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
        " connect-src X; img-src 'self' data:; font-src 'self'; base-uri 'none';"
        " form-action 'none'; frame-ancestors Y"
    )
    assert policy(widget_headers(ORIGIN, [])["Content-Security-Policy"])["frame-ancestors"] == (
        "'none'"
    )


@pytest.mark.parametrize(
    "business_id", ["no-such-business", "Paws-and-co", "paws-and-co.json", "..", "%2e%2e"]
)
def test_an_unknown_business_is_the_one_404(served: Served, business_id: str) -> None:
    response = served.client.get(f"/widget/{business_id}")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert "content-security-policy" not in response.headers


def test_a_disabled_widget_has_no_page(served: Served) -> None:
    widget = served.app.state.widgets.enabled("paws-and-co")
    disabled = replace(widget, file=widget.file.model_copy(update={"enabled": False}))
    served.app.state.widgets = WidgetRegistry({"paws-and-co": disabled})

    response = served.client.get("/widget/paws-and-co")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


# ── The dashboard and the assets ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "content"), [("/staff/", STAFF_HTML), ("/staff/login", LOGIN_HTML)]
)
def test_the_dashboards_pages_are_never_framed(served: Served, path: str, content: bytes) -> None:
    response = served.client.get(path)

    assert response.status_code == 200 and response.content == content
    directives = policy(response.headers["content-security-policy"])
    assert directives["frame-ancestors"] == "'none'" and directives["connect-src"] == "'self'"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"  # D12 (b) [proposal]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-cache"  # as the widget page's
    assert {key.lower(): value for key, value in STAFF_HEADERS.items()}.items() <= dict(
        response.headers
    ).items()


@pytest.mark.parametrize("page", ["Login", "login.html", "log_in", "-login", "missing"])
def test_a_dashboard_page_outside_the_pattern_or_the_build_is_the_one_404(
    served: Served, page: str
) -> None:
    response = served.client.get(f"/staff/{page}")

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


@pytest.mark.parametrize(
    ("path", "content", "media"),
    [(SCRIPT_PATH, SCRIPT, "text/javascript"), ("/icon.svg", ICON_SVG, "image/svg+xml")],
)
def test_the_assets_are_served_as_files_with_nosniff(
    served: Served, path: str, content: bytes, media: str
) -> None:
    response = served.client.get(path)

    assert response.status_code == 200 and response.content == content
    assert response.headers["content-type"].startswith(media)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers
    assert "cache-control" not in response.headers  # hashed assets keep the default caching


@pytest.mark.parametrize("path", [SCRIPT_PATH, "/icon.svg"])
def test_every_asset_carries_nosniff_and_no_referrer(served: Served, path: str) -> None:
    # Mutation probe (ui-assets-without-referrer-policy): the module docstring's promise.
    response = served.client.get(path)

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


# ── Every miss is one 404 ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_without_ui_dir_every_page_route_is_the_one_404(path: str) -> None:
    with serve(widget_settings()) as served:
        assert served.app.state.ui is None
        response = served.client.get(path)

    assert (response.status_code, response.json()) == (404, NOT_FOUND)


def test_a_page_the_build_lacks_is_the_one_404(served: Served, ui_dir: Path) -> None:
    (ui_dir / "widget.html").unlink()
    (ui_dir / "staff.html").unlink()

    for path in ("/widget/paws-and-co", "/staff/"):
        response = served.client.get(path)
        assert (response.status_code, response.json()) == (404, NOT_FOUND), path


TRAVERSALS: Final = [
    "/_next/static/..%2F..%2F..%2Fsecret.txt",
    "/_next/static/%2e%2e/%2e%2e/%2e%2e/secret.txt",
    "/_next/static/chunks/..%2F..%2F..%2F..%2Fsecret.txt",
    "/_next/static/chunks/%2E%2E%2F%2E%2E%2F%2E%2E%2F%2E%2E%2Fsecret.txt",
    "/_next/static/..%5C..%5C..%5Csecret.txt",
    "/_next/static/chunks/.hidden.js",
    "/_next/static/chunks//app-0test.js",
    "/_next/static/chunks/app-0test.js%00.html",
    "/_next/static/%2Fetc%2Fpasswd",
    "/_next/static/chunks/link-out.js",
    "/staff/..%2F..%2Fsecret",
    "/widget/..%2F..%2Fsecret.txt",
]
"""Each aims at `secret.txt` beside `ui_dir`, a dotfile in it, or a link out of it."""


@pytest.mark.parametrize("path", TRAVERSALS)
def test_no_path_reaches_a_file_outside_ui_dir_or_a_hidden_one(
    served: Served, ui_dir: Path, path: str
) -> None:
    link = ui_dir / "_next" / "static" / "chunks" / "link-out.js"
    link.symlink_to(ui_dir.parent / "secret.txt")

    response = served.client.get(path)

    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert SECRET not in response.content
    # The positive controls: the target exists, and a plain asset beside it is served.
    assert (ui_dir.parent / "secret.txt").read_bytes() == SECRET
    assert served.client.get(SCRIPT_PATH).content == SCRIPT


@pytest.mark.parametrize(
    "parts",
    [
        ("..", "secret.txt"),
        (".", "widget.html"),
        ("", "widget.html"),
        ("_next", "static", "chunks", ".hidden.js"),
        ("/etc", "passwd"),
        ("widget.html/",),
        ("staff\\..\\..\\secret.txt",),
        ("a\x00b",),
        ("x",) * (MAX_SEGMENTS + 1),
        (),
        ("_next",),  # a folder, not a file
    ],
)
def test_ui_files_refuse_every_part_that_is_not_a_plain_name_of_a_file(
    ui_dir: Path, parts: tuple[str, ...]
) -> None:
    files = load_ui(ui_dir, ORIGIN)

    assert files.file(*parts) is None
    assert files.file("widget.html") == (ui_dir / "widget.html").resolve()  # the positive control


def test_a_real_file_deeper_than_max_segments_is_never_served(served: Served, ui_dir: Path) -> None:
    # Mutation probe (PR 2): the depth bound against a file that exists (the parametrized case
    # above is also missing on disk, so it cannot tell the bound from the lookup).
    dirs = ["d"] * (MAX_SEGMENTS - 2)  # under _next/static: MAX_SEGMENTS + 1 parts in all
    folder = ui_dir / "_next" / "static" / Path(*dirs)
    folder.mkdir(parents=True)
    (folder / "x.js").write_bytes(b"/* deep */")
    (folder.parent / "y.js").write_bytes(b"/* ok */")

    parts = ("_next", "static", *dirs, "x.js")
    assert len(parts) == MAX_SEGMENTS + 1
    assert load_ui(ui_dir, ORIGIN).file(*parts) is None
    assert served.client.get("/" + "/".join(parts)).status_code == 404
    shallow = ("_next", "static", *dirs[:-1], "y.js")  # the positive control: MAX_SEGMENTS parts
    assert served.client.get("/" + "/".join(shallow)).content == b"/* ok */"


def test_a_link_inside_ui_dir_to_a_file_inside_it_is_served(ui_dir: Path) -> None:
    (ui_dir / "alias.html").symlink_to(ui_dir / "widget.html")
    (ui_dir / "loop.html").symlink_to(ui_dir / "loop.html")

    files = load_ui(ui_dir, ORIGIN)

    assert files.file("alias.html") == (ui_dir / "widget.html").resolve()
    assert files.file("loop.html") is None


# ── Startup ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_a_ui_dir_that_is_not_a_folder_stops_the_app(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "ui"
    if kind == "file":
        path.write_bytes(WIDGET_HTML)

    with pytest.raises(UiDirError) as raised:
        create_app(widget_settings(ui_dir=path))

    assert str(raised.value) == "ui_dir (VECTAPILOT_UI_DIR) must be an existing folder"
    assert str(tmp_path) not in str(raised.value)


def test_ui_dir_without_the_public_origin_is_refused_at_startup(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as raised:
        make_settings(ui_dir=write_ui(tmp_path / "ui"), public_origin=None)

    assert UI_DIR_NEEDS_ORIGIN in str(raised.value)


def test_ui_dir_is_resolved_once_at_startup(ui_dir: Path) -> None:
    os.symlink(ui_dir, ui_dir.parent / "current")

    files = load_ui(ui_dir.parent / "current", ORIGIN)

    assert files == UiFiles(ui_dir.resolve(), ORIGIN)


# ── S13: the headers on the new pages only ───────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/healthz", "/v1/widget/paws-and-co/config", "/openapi.json"])
def test_no_existing_route_carries_the_pages_headers(served: Served, path: str) -> None:
    response = served.client.get(path)

    assert response.status_code == 200
    for header, value in STAFF_HEADERS.items():
        if header in PAGE_CACHE:  # the widget routes' own `no-store` (S9) is not the pages'
            assert response.headers.get(header.lower()) != value, (path, header)
        else:
            assert header.lower() not in response.headers, (path, header)
