"""The dry run's committed fixtures (`apps/api/tests/extracteval/fixtures/dry-run/`, `dryrun.py`).

They are TEST-ONLY, made up by the team, and never the dataset: each `.pdf` is rebuilt from its
`.pdf.txt` byte for byte, read by the core within E12, carries the TEST-ONLY marker (so the
dataset's check would refuse it), and each gold names exactly its template's fields in canonical
form. `ScriptProvider` plays each document's script, then an invalid text once it runs out.
"""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from tests.extracteval.harness_helpers import FIXTURES
from tests.extracteval.helpers import EXTRACTION_DIR
from tests.extracteval.pdfs import pdf_of_text
from tests.tools.helpers import CORPUS_DIR
from vectapilot_api.evalset.questions import DatasetError, is_placeholder_text
from vectapilot_api.extracteval.checks import read_document
from vectapilot_api.extracteval.dryrun import (
    FIXTURES_FILE,
    SCRIPT_ENDED,
    FixtureFile,
    ScriptProvider,
    load_fixtures,
)
from vectapilot_api.extraction.files import ExtractionConfig, load_extraction_dir
from vectapilot_api.extraction.text import DocumentRefused
from vectapilot_api.gateway.providers.base import ProviderRequest
from vectapilot_api.gateway.types import Message

REQUEST = ProviderRequest(
    model="test-model",
    effort="low",
    max_tokens=100,
    system="TEST-ONLY system",
    messages=(Message(role="user", content="TEST-ONLY"),),
)


@pytest.fixture(scope="module")
def config() -> ExtractionConfig:
    return load_extraction_dir(EXTRACTION_DIR, CORPUS_DIR)


def test_the_committed_fixtures_load_and_cover_both_businesses(config: ExtractionConfig) -> None:
    fixtures = load_fixtures(FIXTURES, config)
    documents = fixtures.file.documents
    assert {d.business_id for d in documents} == {"sunrise-clinic", "paws-and-co"}
    assert len(fixtures.sha256) == 64
    assert set(fixtures.pdfs) == {d.id for d in documents}
    tags = {tag for d in documents for tag in d.tags}
    assert tags == {"absent", "confusable", "injection", "image_page", "table"}


@pytest.mark.parametrize("source", sorted(FIXTURES.glob("*.pdf.txt")), ids=lambda p: p.name)
def test_each_pdf_is_rebuilt_from_its_source_and_read_within_e12(source: Path) -> None:
    text = source.read_text("utf-8")
    pdf = source.with_name(source.name.removesuffix(".txt"))
    assert pdf.read_bytes() == pdf_of_text(text), f"rebuild {pdf.name} from its source"
    assert is_placeholder_text(text.splitlines()[1])
    assert not isinstance(read_document(pdf.read_bytes()), DocumentRefused)


def test_every_source_has_a_fixture_and_every_fixture_a_source() -> None:
    data = json.loads((FIXTURES / FIXTURES_FILE).read_text("utf-8"))
    ids = {document["id"] for document in data["documents"]}
    sources = {p.name.removesuffix(".pdf.txt") for p in FIXTURES.glob("*.pdf.txt")}
    assert ids == sources
    assert {p.name for p in FIXTURES.iterdir()} == {
        FIXTURES_FILE,
        *(f"{i}.pdf.txt" for i in ids),
        *(f"{i}.pdf" for i in ids),
    }


def test_the_fixture_gold_is_canonical(config: ExtractionConfig) -> None:
    fixtures = load_fixtures(FIXTURES, config)
    for document in fixtures.file.documents:
        kind = config.kind_config(document.business_id, document.kind)
        assert kind is not None
        for spec in kind.template.fields:
            value = document.gold[spec.name]
            if value is None:
                continue
            if spec.type.value == "date":
                assert len(value) == 10 and value[4] == value[7] == "-", (document.id, spec.name)
            if spec.type.value == "enum":
                ids = {v.id for v in kind.enum_values.get(spec.name, ())}
                assert value in ids, (document.id, spec.name)


def test_a_fixture_off_its_configuration_is_refused(
    tmp_path: Path, config: ExtractionConfig
) -> None:
    data = json.loads((FIXTURES / FIXTURES_FILE).read_text("utf-8"))
    data["documents"][0]["kind"] = "vaccination_record"
    data["documents"][1]["gold"].pop("insurer")
    (tmp_path / FIXTURES_FILE).write_text(json.dumps(data), "utf-8")
    with pytest.raises(DatasetError) as refused:
        load_fixtures(tmp_path, config)
    message = str(refused.value)
    assert "tonly-referral-plain: its business does not enable its kind" in message
    assert "tonly-referral-swap: its gold does not name the template's fields" in message
    assert "has no .pdf" in message
    assert "Corvane" not in message


def test_a_fixture_file_without_the_synthetic_flag_is_refused(
    tmp_path: Path, config: ExtractionConfig
) -> None:
    data = json.loads((FIXTURES / FIXTURES_FILE).read_text("utf-8"))
    data["synthetic"] = False
    (tmp_path / FIXTURES_FILE).write_text(json.dumps(data), "utf-8")
    with pytest.raises(DatasetError, match="not a valid fixture file"):
        load_fixtures(tmp_path, config)
    with pytest.raises(DatasetError, match="is missing"):
        load_fixtures(tmp_path / "none", config)


def test_the_dry_fingerprint_covers_every_fixture_pdf(
    tmp_path: Path, config: ExtractionConfig
) -> None:
    # Mutation audit DR01: a changed PDF is a changed dry config.
    copy = tmp_path / "dry-run"
    shutil.copytree(FIXTURES, copy)
    before = load_fixtures(copy, config).sha256
    assert before == load_fixtures(FIXTURES, config).sha256
    pdf = copy / "tonly-vaccination-plain.pdf"
    pdf.write_bytes(pdf.read_bytes() + b"\n")
    assert load_fixtures(copy, config).sha256 != before


async def _texts(provider: ScriptProvider, document_id: str, count: int) -> list[tuple[str, str]]:
    provider.begin(document_id)
    out: list[tuple[str, str]] = []
    for _ in range(count):
        response = await provider.send(REQUEST)
        text = "".join(getattr(block, "text", "") for block in response.content)
        out.append((text, response.stop_reason))
    return out


def test_each_document_starts_a_fresh_script() -> None:
    # A step left over from one document never answers the next (mutation audit DR03).
    fixtures = FixtureFile.model_validate_json((FIXTURES / FIXTURES_FILE).read_bytes())
    plain = next(d for d in fixtures.documents if d.id == "tonly-referral-plain")
    doubled = plain.model_copy(update={"script": plain.script * 2})
    provider = ScriptProvider(fixtures.model_copy(update={"documents": (doubled,)}))

    first = asyncio.run(_texts(provider, plain.id, 1))
    assert first[0][0] != SCRIPT_ENDED  # one of its two steps used, one left
    assert asyncio.run(_texts(provider, "tonly-unknown", 1)) == [(SCRIPT_ENDED, "end_turn")]


def test_the_script_provider_plays_each_documents_script_then_an_invalid_text() -> None:
    fixtures = FixtureFile.model_validate_json((FIXTURES / FIXTURES_FILE).read_bytes())
    provider = ScriptProvider(fixtures)

    async def texts(document_id: str, count: int) -> list[tuple[str, str]]:
        return await _texts(provider, document_id, count)

    plain = asyncio.run(texts("tonly-referral-plain", 2))
    assert json.loads(plain[0][0])["patient_name"]["quote"] == "Patient name: Ilsa Corvane"
    assert plain[1] == (SCRIPT_ENDED, "end_turn")
    truncated = asyncio.run(texts("tonly-referral-truncated", 1))
    assert truncated[0][1] == "max_tokens"
    assert asyncio.run(texts("tonly-unknown", 1)) == [(SCRIPT_ENDED, "end_turn")]
    assert provider.requests == 4
