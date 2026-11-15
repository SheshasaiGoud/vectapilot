"""The dry run's documents and scripted model (M6 PR 4; scoring rules, "Dry or real").

A dry run sends nothing anywhere and **never reads the dataset's documents or gold**: it runs the
extraction core for real (text, the extraction's gateway with its structured output and repair,
signals, routing, the invariant check) over **fixture documents** from `tests/` (default
`apps/api/tests/extracteval/fixtures/dry-run/`), answered by `ScriptProvider`, which plays each
fixture's scripted replies. The fixtures are made up by the team for tests, carry the `TEST-ONLY`
marker, and are no example for the writers' brief; their numbers mean nothing (DRY RUN — plumbing
only, never a result).

**The folder** (`FixtureFile`): `fixtures.json`, `{"synthetic": true, "note": ..., "documents":
[...]}`, each document with its id (`tonly-...`), business, kind, tags, gold (every field of the
kind's template, canonical or null) and `script`: one step per model attempt, each an `answer`
(`{field: {"value", "quote", "unsure"}}`, the shape the closed output model checks), a `raw` text
(invalid output: the gateway repairs once), or either with `stop_reason: "max_tokens"` (a
truncation). A script that runs out answers an invalid text, so the gateway's repair fails and
the core fails closed (E3). Next to it, `<id>.pdf` for each document (and its `<id>.pdf.txt`
source, which the tests rebuild byte for byte).

`fixtures_sha256` (the dry run's fingerprint) hashes `fixtures.json` and every PDF the run reads.
"""

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vectapilot_api.evalset.corpus import describe_validation_error
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.extracteval.gold import FieldName, Tag
from vectapilot_api.extraction.files import ExtractionConfig
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.gateway.types import BusinessId

DEFAULT_FIXTURES: Final = Path("apps", "api", "tests", "extracteval", "fixtures", "dry-run")
"""Relative to the repository root."""
FIXTURES_FILE: Final = "fixtures.json"
PDF_SUFFIX: Final = ".pdf"
SCRIPT_ENDED: Final = "DRY RUN: the script ended."
"""What `ScriptProvider` answers once a document's script is used up: not JSON, so the gateway's
repair fails and the core fails closed into review (E3)."""
FixtureId = Annotated[str, Field(pattern=r"^tonly-[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)]


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ScriptedField(_Closed):
    value: str | None
    quote: str | None
    unsure: bool = False


class ScriptedStep(_Closed):
    """One model attempt's reply: an answer, or a raw text."""

    answer: dict[FieldName, ScriptedField] | None = None
    raw: str | None = None
    stop_reason: Literal["end_turn", "max_tokens"] = "end_turn"

    def text(self) -> str:
        if self.answer is not None:
            return json.dumps({k: v.model_dump() for k, v in self.answer.items()})
        return self.raw if self.raw is not None else SCRIPT_ENDED


class FixtureDocument(_Closed):
    id: FixtureId
    business_id: BusinessId
    kind: FieldName
    tags: tuple[Tag, ...] = ()
    gold: dict[FieldName, str | None] = Field(min_length=1)
    script: tuple[ScriptedStep, ...] = ()


class FixtureFile(_Closed):
    synthetic: Literal[True]
    note: str = Field(min_length=1, max_length=600)
    documents: tuple[FixtureDocument, ...] = Field(min_length=1)


@dataclass(frozen=True)
class Fixtures:
    file: FixtureFile
    pdfs: Mapping[str, bytes]
    sha256: str


def fixture_problems(fixtures: FixtureFile, config: ExtractionConfig) -> list[str]:
    """Each fixture against its business's configuration: the kind is enabled, the gold names
    exactly the template's fields. Ids and names only."""
    problems: list[str] = []
    ids = [document.id for document in fixtures.documents]
    if len(set(ids)) != len(ids):
        problems.append("each fixture id is used once")
    for document in fixtures.documents:
        kind = config.kind_config(document.business_id, document.kind)
        if kind is None:
            problems.append(f"fixture {document.id}: its business does not enable its kind")
            continue
        names = {spec.name for spec in kind.template.fields}
        if set(document.gold) != names:
            problems.append(f"fixture {document.id}: its gold does not name the template's fields")
    return problems


def load_fixtures(folder: Path, config: ExtractionConfig) -> Fixtures:
    """The fixtures in `folder`, checked against `config`. Raises `DatasetError`."""
    path = folder / FIXTURES_FILE
    if not path.is_file():
        raise DatasetError(f"the dry run's fixture file {FIXTURES_FILE} is missing in {folder}")
    data = path.read_bytes()
    try:
        fixtures = FixtureFile.model_validate_json(data)
    except ValidationError as error:
        detail = describe_validation_error(error, "(fixtures)")
        raise DatasetError(f"{FIXTURES_FILE} is not a valid fixture file: {detail}") from None
    problems = fixture_problems(fixtures, config)
    pdfs: dict[str, bytes] = {}
    digest = hashlib.sha256()
    digest.update(f"{FIXTURES_FILE}\0{hashlib.sha256(data).hexdigest()}\n".encode())
    for document in fixtures.documents:
        pdf = folder / f"{document.id}{PDF_SUFFIX}"
        if not pdf.is_file():
            problems.append(f"fixture {document.id} has no {PDF_SUFFIX}")
            continue
        pdfs[document.id] = pdf.read_bytes()
        line = f"{pdf.name}\0{hashlib.sha256(pdfs[document.id]).hexdigest()}\n"
        digest.update(line.encode())
    if problems:
        raise DatasetError("; ".join(problems))
    return Fixtures(file=fixtures, pdfs=pdfs, sha256=digest.hexdigest())


class ScriptProvider:
    """A `Provider` playing one fixture's script per document (`begin`), one step per request."""

    def __init__(self, fixtures: FixtureFile) -> None:
        self._scripts = {document.id: document.script for document in fixtures.documents}
        self._steps: deque[ScriptedStep] = deque()
        self.requests = 0

    def begin(self, document_id: str) -> None:
        """Start a document's script (an unknown id gets an empty one)."""
        self._steps = deque(self._scripts.get(document_id, ()))

    def _next(self) -> ScriptedStep:
        return self._steps.popleft() if self._steps else ScriptedStep()

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests += 1
        step = self._next()
        reply = FakeReply(text=step.text(), stop_reason=step.stop_reason)
        return await FakeProvider([reply]).send(request)

    async def aclose(self) -> None:
        """Nothing to release."""
