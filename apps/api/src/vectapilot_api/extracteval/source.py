"""A document's text source, `<id>.pdf.txt` (M6 PR 4): the pages a person writes, and the options
the fixture writer can draw. The PDF is rebuilt from it, byte for byte (README, "Documents").

The format, one line of the file per printed line (ASCII only: the writer draws Helvetica):

    === page 1 ===              every page starts with its marker, numbered 1, 2, ... in order
    Referral letter             a plain line, printed on the next row
    {x=200 row=3} some text     a line with options (below)
    === page 2 image ===        a page with an image: a stand-in for a logo or a scanned
                                attachment, whose content nobody can read (E4)

**Line options**, in braces at the start of a line, then one space, then the text (not empty):
- `x=<points>`: the line starts that far right of the margin (0 to `X_MAX`). A line with `x` above
  0 is a *column*: a table written as a label column, then a value column (the `table` tag);
- `row=<n>`: the line is printed on text row `n` from the top (0 to `ROW_MAX`); without it, on the
  row after the previous line's;
- `hidden`: the line is in the text layer but never painted (`3 Tr`): only a reader of the text
  layer (pypdf, so the model) sees it, a person looking at the page does not.

A page's leading and trailing blank lines are dropped (a blank line before a marker is only for
the eye); a page with no lines is blank. Inner blank lines stay: they are printed as empty rows.
A plain page without an image is written exactly as M2's sample PDF (the writer's original path).

Parsing is pure: it takes the file's text and returns a `SourceDocument`, or raises `SourceError`
naming line numbers and the rule, never a line's text.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

SOURCE_SUFFIX: Final = ".pdf.txt"
PDF_SUFFIX: Final = ".pdf"

PAGE_MARKER: Final = re.compile(r"=== page (?P<number>[1-9][0-9]*)(?P<image> image)? ===")
OPTION_LINE: Final = re.compile(r"\{(?P<options>[^{}]*)\} (?P<text>.*)")
OPTION: Final = re.compile(r"hidden|x=(?P<x>[0-9]{1,3})|row=(?P<row>[0-9]{1,2})")

X_MAX: Final = 300
"""Points right of the left margin: a column still starts on the page."""
ROW_MAX: Final = 46
"""The last text row above the bottom margin (720 - 14 * 46 = 76 points from the bottom)."""
LINE_MAX_CHARS: Final = 85
"""A line longer than this would run off the right edge at 12 points (a bound, not a measure)."""
PAGES_MAX: Final = 20
"""The parser's own bound; E12's page limit is checked against the core's (`checks.py`)."""


class SourceError(ValueError):
    """A `.pdf.txt` file breaks the format. The message names line numbers and rules, never
    text."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class SourceLine:
    """One printed line and its options (module docstring)."""

    text: str
    x: int = 0
    row: int | None = None
    hidden: bool = False

    @property
    def plain(self) -> bool:
        return self.x == 0 and self.row is None and not self.hidden


@dataclass(frozen=True)
class SourcePage:
    number: int
    image: bool
    lines: tuple[SourceLine, ...]

    @property
    def plain(self) -> bool:
        """No image and no line option: the writer's original path."""
        return not self.image and all(line.plain for line in self.lines)


@dataclass(frozen=True)
class SourceDocument:
    pages: tuple[SourcePage, ...]

    @property
    def image_pages(self) -> tuple[int, ...]:
        return tuple(page.number for page in self.pages if page.image)

    @property
    def has_columns(self) -> bool:
        """A line with `x` above 0: the `table` tag (README)."""
        return any(line.x > 0 for page in self.pages for line in page.lines)

    @property
    def has_hidden(self) -> bool:
        return any(line.hidden for page in self.pages for line in page.lines)

    def texts(self) -> tuple[str, ...]:
        """Every line's text, in file order (for the screens; never printed by `check`)."""
        return tuple(line.text for page in self.pages for line in page.lines)


def _printable(text: str) -> bool:
    return all(" " <= character <= "~" for character in text)


def _parse_line(raw: str, number: int, problems: list[str]) -> SourceLine | None:
    where = f"line {number}"
    if not _printable(raw):
        problems.append(
            f"{where}: only printable ASCII (no tab, no accent, no other script: the writer draws"
            " Helvetica)"
        )
        return None
    if not raw.startswith("{"):
        if len(raw) > LINE_MAX_CHARS:
            problems.append(f"{where}: longer than {LINE_MAX_CHARS} characters (off the page)")
        return SourceLine(raw)
    match = OPTION_LINE.fullmatch(raw)
    if match is None or not match.group("text").strip():
        problems.append(
            f"{where}: a line starting with {{ is options in braces, one space, then text"
        )
        return None
    x, row, hidden = 0, None, False
    seen: set[str] = set()
    for token in match.group("options").split():
        option = OPTION.fullmatch(token)
        name = token.split("=", 1)[0]
        if option is None or name in seen:
            problems.append(f"{where}: options are hidden, x=<points> and row=<n>, each once")
            return None
        seen.add(name)
        if option.group("x") is not None:
            x = int(option.group("x"))
        elif option.group("row") is not None:
            row = int(option.group("row"))
        else:
            hidden = True
    if not seen:
        problems.append(f"{where}: empty braces (write the text without them)")
        return None
    if x > X_MAX:
        problems.append(f"{where}: x is at most {X_MAX}")
    if row is not None and row > ROW_MAX:
        problems.append(f"{where}: row is at most {ROW_MAX}")
    text = match.group("text")
    if len(text) > LINE_MAX_CHARS:
        problems.append(f"{where}: longer than {LINE_MAX_CHARS} characters (off the page)")
    return SourceLine(text, x=x, row=row, hidden=hidden)


def _trimmed(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    while lines and not lines[0][1].strip():
        lines.pop(0)
    while lines and not lines[-1][1].strip():
        lines.pop()
    return lines


def _rows(page: SourcePage, first_line: int, problems: list[str]) -> None:
    """Every line lands on a row inside the page (a line without `row` follows the previous)."""
    row = -1
    for offset, line in enumerate(page.lines):
        row = row + 1 if line.row is None else line.row
        if row > ROW_MAX:
            problems.append(
                f"page {page.number} (from line {first_line}): line {offset + 1} of the page falls"
                f" below row {ROW_MAX} (off the page)"
            )
            return


def parse_source(text: str) -> SourceDocument:
    """The pages of a `.pdf.txt` file (module docstring). Raises `SourceError`."""
    problems: list[str] = []
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()  # the file's final newline
    pages: list[tuple[int, bool, int, list[tuple[int, str]]]] = []
    for number, raw in enumerate(raw_lines, start=1):
        raw = raw.removesuffix("\r")
        marker = PAGE_MARKER.fullmatch(raw)
        if marker is not None:
            pages.append((int(marker.group("number")), bool(marker.group("image")), number, []))
        elif raw.startswith("==="):
            problems.append(f"line {number}: a page marker is '=== page <n> ===' or with ' image'")
        elif not pages:
            if raw.strip():
                problems.append(f"line {number}: text before the first page marker")
        else:
            pages[-1][3].append((number, raw))
    if not pages:
        raise SourceError([*problems, "no page marker: the file starts with '=== page 1 ==='"])
    if [number for number, _, _, _ in pages] != list(range(1, len(pages) + 1)):
        problems.append("page markers are numbered 1, 2, ... in order")
    if len(pages) > PAGES_MAX:
        problems.append(f"more than {PAGES_MAX} pages")
    built: list[SourcePage] = []
    for number, image, first, lines in pages:
        parsed = [_parse_line(raw, at, problems) for at, raw in _trimmed(lines)]
        page = SourcePage(number, image, tuple(line for line in parsed if line is not None))
        _rows(page, first, problems)
        built.append(page)
    if problems:
        raise SourceError(problems)
    return SourceDocument(tuple(built))
