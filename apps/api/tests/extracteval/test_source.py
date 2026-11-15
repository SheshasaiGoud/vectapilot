"""M6 PR 4: the `.pdf.txt` format (`extracteval/source.py`) and its PDF (`pdfs.py`).

Every text here is TEST-ONLY. Problems name line numbers and rules: a canary in a bad line never
reaches the error.
"""

import io

import pytest
from pypdf import PdfReader

from tests.extracteval.pdfs import pdf_of_text
from tests.ingestion.pdf_builder import build_pdf
from vectapilot_api.extracteval.source import (
    LINE_MAX_CHARS,
    ROW_MAX,
    X_MAX,
    SourceError,
    SourceLine,
    parse_source,
)

CANARY = "CANARY-7f3a"


def problems_of(text: str) -> tuple[str, ...]:
    with pytest.raises(SourceError) as caught:
        parse_source(text)
    assert CANARY not in str(caught.value)
    return caught.value.problems


def test_pages_lines_and_options() -> None:
    text = (
        "=== page 1 ===\n\nA\n\nB\n\n"
        "=== page 2 image ===\n"
        "{x=150 row=3} V\n{hidden} H\n{row=4 hidden x=10} W\n"
        "=== page 3 ===\n"
    )
    document = parse_source(text)

    first, second, third = document.pages
    # Leading and trailing blank lines dropped; the inner one stays as an empty row.
    assert [line.text for line in first.lines] == ["A", "", "B"]
    assert first.plain and not first.image
    assert second.image and not second.plain
    assert second.lines == (
        SourceLine("V", x=150, row=3),
        SourceLine("H", hidden=True),
        SourceLine("W", x=10, row=4, hidden=True),
    )
    assert third.lines == () and third.plain  # a blank page
    assert document.image_pages == (2,)
    assert document.has_columns and document.has_hidden
    assert document.texts() == ("A", "", "B", "V", "H", "W")


def test_a_file_without_its_final_newline_or_with_crlf_reads_the_same() -> None:
    assert parse_source("=== page 1 ===\nA") == parse_source("=== page 1 ===\r\nA\r\n")


def test_x_alone_is_a_column_and_row_alone_is_not() -> None:
    assert not parse_source("=== page 1 ===\n{row=5} A\n").has_columns
    assert parse_source("=== page 1 ===\n{x=1} A\n").has_columns


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "no page marker"),
        (f"{CANARY}\n=== page 1 ===\n", "line 1: text before the first page marker"),
        ("=== page 2 ===\n", "numbered 1, 2"),
        ("=== page 1 ===\n=== page 3 ===\n", "numbered 1, 2"),
        ("=== page one ===\n", "line 1: a page marker is"),
        ("=== page 1 ===\n=== page 2 scan ===\n", "line 2: a page marker is"),
        (f"=== page 1 ===\n{CANARY} é\n", "line 2: only printable ASCII"),
        (f"=== page 1 ===\n{CANARY}\t1\n", "line 2: only printable ASCII"),
        (f"=== page 1 ===\n{{bold}} {CANARY}\n", "line 2: options are hidden"),
        (f"=== page 1 ===\n{{x=1 x=2}} {CANARY}\n", "line 2: options are hidden"),
        (f"=== page 1 ===\n{{}} {CANARY}\n", "line 2: empty braces"),
        ("=== page 1 ===\n{hidden} \n", "line 2: a line starting with {"),
        (f"=== page 1 ===\n{{hidden}}{CANARY}\n", "line 2: a line starting with {"),
        (f"=== page 1 ===\n{{x={X_MAX + 1}}} {CANARY}\n", f"line 2: x is at most {X_MAX}"),
        (f"=== page 1 ===\n{{row={ROW_MAX + 1}}} {CANARY}\n", f"row is at most {ROW_MAX}"),
        ("=== page 1 ===\n" + CANARY * 9 + "\n", f"longer than {LINE_MAX_CHARS}"),
        ("=== page 1 ===\n{hidden} " + CANARY * 9 + "\n", f"longer than {LINE_MAX_CHARS}"),
    ],
)
def test_each_rule_names_its_line_never_its_text(text: str, expected: str) -> None:
    assert any(expected in problem for problem in problems_of(text)), problems_of(text)


def test_lines_below_the_page_are_refused_whether_flowing_or_placed() -> None:
    flowing = "=== page 1 ===\n" + "A\n" * (ROW_MAX + 2)
    assert any("falls below row" in p for p in problems_of(flowing))
    assert parse_source("=== page 1 ===\n" + "A\n" * (ROW_MAX + 1)).pages[0].lines
    placed = f"=== page 1 ===\n{{row={ROW_MAX}}} A\nB\n"
    assert any("line 2 of the page falls below" in p for p in problems_of(placed))


def test_every_problem_is_reported_at_once() -> None:
    problems = problems_of("=== page 1 ===\n{bad} A\n=== page 3 ===\né\n")
    assert len(problems) == 3


def test_a_plain_page_is_built_exactly_as_m2s_sample_pdf() -> None:
    # The writer's original path, byte for byte (corpus PDFs and this format share it).
    assert pdf_of_text("=== page 1 ===\nA\nB (c)\n=== page 2 ===\n") == build_pdf(
        [["A", "B (c)"], []]
    )


def test_the_pdf_shows_the_options_as_pypdf_reads_them() -> None:
    text = (
        "=== page 1 ===\nTEST-ONLY\n{row=2} Name:\n{x=150 row=2} Value\n{hidden} Secret\n"
        "=== page 2 image ===\n"
    )
    reader = PdfReader(io.BytesIO(pdf_of_text(text)))

    first = reader.pages[0].extract_text()
    for line in ("TEST-ONLY", "Name:", "Value", "Secret"):
        assert line in first
    assert b" 3 Tr" in pdf_of_text(text)
    assert "/XObject" in reader.pages[1]["/Resources"]  # type: ignore[operator]
    assert pdf_of_text(text) == pdf_of_text(text)  # same text, same bytes
