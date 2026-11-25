"""The product pages, served from `ui_dir` on the API's own origin (M8 PR 2b; D2 (a) with (i), D3
(a), D12 (b), S9, S13, ADR 0019 decision 4).

`ui_dir` holds `apps/web`'s product build (`npm run build:ui`, `out-ui/`, mounted read-only). Its
names follow Next.js's static export without trailing slashes: the page `app/widget/` is
`widget.html`, `app/staff/` is `staff.html` and `app/staff/login/` is `staff/login.html`; the
scripts, styles and fonts live under `_next/static/`. Served here, and nothing else of the folder:

- **`GET /widget/{business_id}`** → `widget.html`, for a business whose widget is enabled
  (`app.state.widgets`, files only). The page reads the business from its own path (static export:
  no request-time rendering).
- **`GET /staff/`** → `staff.html`, and **`GET /staff/{page}`** → `staff/{page}.html` for a page
  name of lowercase letters, digits and single hyphens (the login link's `/staff/login`)
  [proposal: PR 5 names its pages so].
- **`GET /_next/static/{path}`** → the build's static assets, and **`GET /icon.svg`**, the one
  top-level file a page links.

**Headers** (S9; on these routes only, never on an existing one, S13). Every page gets
`Content-Security-Policy: default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self'
'unsafe-inline'; connect-src ...; img-src 'self' data:; font-src 'self'; base-uri 'none';
form-action 'none'; frame-ancestors ...`, `X-Content-Type-Options: nosniff` and
`Referrer-Policy: no-referrer`:
- the widget page: `frame-ancestors` from that business's `allowed_origins` (D3 (a): the host pages
  that may frame it, each validated strictly at startup, so a header can hold them), and, under
  D12 (b), `connect-src <public_origin>/v1/widget/`: the page may call the widget's routes and
  nothing else, the staff routes on the same origin included;
- the dashboard: `frame-ancestors 'none'`, `X-Frame-Options: DENY` (for browsers without CSP
  level 2) and, under D12 (b), `Cross-Origin-Opener-Policy: same-origin` [proposal; its effect is
  checked by hand, rule 7], with `connect-src 'self'` (S9);
- the assets: `nosniff` and `no-referrer`.

**Pages revalidate on every load** (`Cache-Control: no-cache`, zara's PR 2 review): a page's
`frame-ancestors` is that business's current `allowed_origins`, so a browser must never reuse a
copy with an origin since removed (FileResponse's `Last-Modified` and `ETag` would otherwise allow
heuristic freshness). A revalidation is a conditional request answered from the current file and
headers. The hashed `_next/static` assets and the icon keep the browser's default caching.

**Every miss is one 404** with the router's own body (`{"detail": "Not Found"}`): `ui_dir` unset,
an unknown business or a disabled widget, a page the build does not have, and any path that is
not plainly a file under `ui_dir`. A path is resolved one segment at a time from fixed patterns
(letters, digits, `_`, `-`, `.`, `~`, never starting with a dot: no `..`, no dotfile, no empty
segment, no separator), then the resolved file, symbolic links followed, must still be a regular
file under the resolved `ui_dir`. The business id in the widget's path only ever looks up the
registry, never a file.

Read-only by construction: nothing here writes, and the handlers are synchronous (FastAPI runs them
in its thread pool, so a slow disk never blocks the event loop). Nothing is logged.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.router import get_widgets

router = APIRouter(include_in_schema=False)

WIDGET_PAGE: Final = "widget.html"
STAFF_PAGE: Final = "staff.html"
STAFF_FOLDER: Final = "staff"
ASSETS: Final = ("_next", "static")
ICON: Final = "icon.svg"
WIDGET_API: Final = "/v1/widget/"
"""D12 (b): the one path prefix the widget page may connect to."""

SEGMENT: Final = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._~-]{0,254}")
"""One path segment of a served file (module docstring)."""
MAX_SEGMENTS: Final = 8
PAGE_NAME: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
"""A dashboard page's name under `/staff/` [proposal]."""
NOT_FOUND: Final = "Not Found"
"""The router's own 404 body's text: every miss looks the same."""

CSP: Final = "Content-Security-Policy"
COMMON_HEADERS: Final = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
PAGE_CACHE: Final = {"Cache-Control": "no-cache"}
"""Every page revalidates (module docstring); assets do not carry it."""


class UiDirError(ValueError):
    """`ui_dir` is not a folder: the app does not start. The message names the setting only."""


def content_security_policy(*, connect_src: str, frame_ancestors: str) -> str:
    """S9's policy for a product page, with its `connect-src` and `frame-ancestors`."""
    return "; ".join(
        (
            "default-src 'none'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            f"connect-src {connect_src}",
            "img-src 'self' data:",
            "font-src 'self'",
            "base-uri 'none'",
            "form-action 'none'",
            f"frame-ancestors {frame_ancestors}",
        )
    )


def widget_headers(public_origin: str, allowed_origins: Sequence[str]) -> dict[str, str]:
    """The widget page's headers for one business (module docstring)."""
    policy = content_security_policy(
        connect_src=f"{public_origin}{WIDGET_API}",
        frame_ancestors=" ".join(allowed_origins) or "'none'",
    )
    return {**COMMON_HEADERS, **PAGE_CACHE, CSP: policy}


STAFF_HEADERS: Final = {
    **COMMON_HEADERS,
    **PAGE_CACHE,
    CSP: content_security_policy(connect_src="'self'", frame_ancestors="'none'"),
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
}
"""The dashboard's headers (module docstring)."""


@dataclass(frozen=True)
class UiFiles:
    """The resolved `ui_dir` and the API's public origin (the widget page's `connect-src`)."""

    root: Path
    public_origin: str

    def file(self, *parts: str) -> Path | None:
        """The regular file at `parts` under the root, or None (module docstring)."""
        if not 0 < len(parts) <= MAX_SEGMENTS:
            return None
        if not all(SEGMENT.fullmatch(part) for part in parts):
            return None
        try:
            found = self.root.joinpath(*parts).resolve(strict=True)
        except (OSError, RuntimeError):  # missing, unreadable, or a loop of links
            return None
        if not found.is_relative_to(self.root) or not found.is_file():
            return None
        return found


def load_ui(ui_dir: Path, public_origin: str) -> UiFiles:
    """`ui_dir`, resolved once at startup (`create_app`); a path that is not a folder stops the
    app. Its pages need not exist yet: a missing one is a 404 when asked for."""
    try:
        root = ui_dir.resolve(strict=True)
    except (OSError, RuntimeError):
        root = None
    if root is None or not root.is_dir():
        raise UiDirError("ui_dir (VECTAPILOT_UI_DIR) must be an existing folder")
    return UiFiles(root, public_origin)


def not_found() -> JSONResponse:
    return JSONResponse({"detail": NOT_FOUND}, status_code=404)


def ui_of(request: Request) -> UiFiles | None:
    ui: UiFiles | None = request.app.state.ui
    return ui


def _served(ui: UiFiles | None, parts: Sequence[str], headers: dict[str, str]) -> Response:
    found = None if ui is None else ui.file(*parts)
    if found is None:
        return not_found()
    return FileResponse(found, headers=dict(headers))


@router.get("/widget/{business_id}")
def widget_page(
    business_id: str,
    request: Request,
    widgets: Annotated[WidgetRegistry, Depends(get_widgets)],
) -> Response:
    """The widget page for `business_id`'s enabled widget, framed only by its own origins."""
    ui = ui_of(request)
    widget = widgets.enabled(business_id)
    if ui is None or widget is None:
        return not_found()
    headers = widget_headers(ui.public_origin, widget.file.allowed_origins)
    return _served(ui, (WIDGET_PAGE,), headers)


@router.get("/staff/")
def staff_home(request: Request) -> Response:
    """The dashboard's page, never framed."""
    return _served(ui_of(request), (STAFF_PAGE,), STAFF_HEADERS)


@router.get("/staff/{page}")
def staff_page(page: str, request: Request) -> Response:
    """Another dashboard page (`/staff/login`), never framed."""
    if not PAGE_NAME.fullmatch(page):
        return not_found()
    return _served(ui_of(request), (STAFF_FOLDER, f"{page}.html"), STAFF_HEADERS)


@router.get("/_next/static/{asset:path}")
def static_asset(asset: str, request: Request) -> Response:
    """One of the build's scripts, styles or fonts."""
    return _served(ui_of(request), (*ASSETS, *asset.split("/")), COMMON_HEADERS)


@router.get("/icon.svg")
def icon(request: Request) -> Response:
    """The icon the pages link."""
    return _served(ui_of(request), (ICON,), COMMON_HEADERS)
