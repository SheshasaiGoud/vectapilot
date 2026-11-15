"""A document's PDF from its `.pdf.txt` (M6 PR 4), with the fixture writer of
`tests/ingestion/pdf_builder.py`: the same writer and pattern as M2's sample PDF.

A plain page (no image, no option) goes through the writer's original path, exactly as M2's
sample PDF; any other page is a `Page` of `Line`s with the source's options. Same text, same
bytes (the writer has no clock and no random id).
"""

from collections.abc import Sequence

from tests.ingestion.pdf_builder import Line, Page, build_pdf
from vectapilot_api.extracteval.source import SourceDocument, parse_source


def pdf_of(source: SourceDocument) -> bytes:
    pages: list[Sequence[str | Line] | Page] = []
    for page in source.pages:
        if page.plain:
            pages.append([line.text for line in page.lines])
            continue
        lines: list[str | Line] = [
            line.text if line.plain else Line(line.text, x=line.x, row=line.row, hidden=line.hidden)
            for line in page.lines
        ]
        pages.append(Page(lines, image=page.image))
    return build_pdf(pages)


def pdf_of_text(text: str) -> bytes:
    return pdf_of(parse_source(text))
