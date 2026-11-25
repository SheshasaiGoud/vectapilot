"""A TEST-ONLY product build for the page routes (M8 PR 2b): the files `apps/web`'s product build
puts where `ui.pages` reads them (Next.js's static export names), each holding a marker, never a
real page. `apps/web` is not built here: the routes serve files, whatever they hold."""

from pathlib import Path
from typing import Final

WIDGET_HTML: Final = b"<!doctype html><title>TEST-ONLY widget page</title>"
STAFF_HTML: Final = b"<!doctype html><title>TEST-ONLY dashboard</title>"
LOGIN_HTML: Final = b"<!doctype html><title>TEST-ONLY staff login</title>"
SCRIPT: Final = b"/* TEST-ONLY chunk */"
ICON_SVG: Final = b'<svg xmlns="http://www.w3.org/2000/svg"><!-- TEST-ONLY --></svg>'
HIDDEN: Final = b"TEST-ONLY dotfile: never served"
SCRIPT_PATH: Final = "/_next/static/chunks/app-0test.js"


def write_ui(folder: Path) -> Path:
    """The TEST-ONLY build at `folder` (module docstring); its path."""
    files = {
        "widget.html": WIDGET_HTML,
        "staff.html": STAFF_HTML,
        "staff/login.html": LOGIN_HTML,
        "_next/static/chunks/app-0test.js": SCRIPT,
        "_next/static/chunks/.hidden.js": HIDDEN,
        "icon.svg": ICON_SVG,
        "widget.txt": b"TEST-ONLY payload: not a served name",
    }
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return folder
