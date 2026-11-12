"""M6 PR 1: the extraction folder (`VECTAPILOT_EXTRACTION_CONFIG_DIR`) and the startup check.

Files only, no database: `create_app` validates the folder before any connection is made.
"""

import shutil
from pathlib import Path

import pytest

from tests.conftest import make_settings
from tests.extraction.helpers import (
    BUSINESSES,
    CORPUS_DIR,
    EXTRACTION_DIR,
    MARKER,
    PAWS,
    REFERRAL,
    SUNRISE,
    VACCINATION,
    copy_extraction,
    edit_json,
    read_json,
    write_json,
)
from tests.tools.helpers import write_corpus
from vectapilot_api.config import Settings
from vectapilot_api.extraction.config import ExtractionConfigError, FieldType
from vectapilot_api.extraction.files import (
    MAX_FILE_BYTES,
    ExtractionConfig,
    load_extraction_dir,
)
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.main import create_app


def load(folder: Path = EXTRACTION_DIR, corpus: Path = CORPUS_DIR) -> ExtractionConfig:
    return load_extraction_dir(folder, corpus)


def refused(folder: Path, corpus: Path = CORPUS_DIR) -> str:
    with pytest.raises(ExtractionConfigError) as error:
        load_extraction_dir(folder, corpus)
    message = str(error.value)
    assert MARKER not in message.lower()
    return message


# ── The committed folder ─────────────────────────────────────────────────────────────────────


def test_the_committed_folder_loads_both_templates_and_both_businesses() -> None:
    config = load()

    assert sorted(config.templates) == [
        (BusinessType.HUMAN_HEALTHCARE, "referral_letter"),
        (BusinessType.VETERINARY, "vaccination_record"),
    ]
    assert tuple(config.businesses) == BUSINESSES
    for business in config.businesses.values():
        assert business.file.synthetic is True
        assert len(business.sha256) == 64
    for compiled in config.templates.values():
        assert len(compiled.sha256) == 64


def test_each_business_gets_its_own_types_template_only() -> None:
    config = load()

    paws = config.kind_config("paws-and-co", "vaccination_record")
    sunrise = config.kind_config("sunrise-clinic", "referral_letter")
    assert paws is not None and sunrise is not None
    assert paws.template.business_type is BusinessType.VETERINARY
    assert sunrise.template.business_type is BusinessType.HUMAN_HEALTHCARE
    # A's kind is not B's: unknown, disabled and other-type kinds are the same None (E8's 404).
    assert config.kind_config("paws-and-co", "referral_letter") is None
    assert config.kind_config("sunrise-clinic", "vaccination_record") is None
    assert config.kind_config("no-such-business", "referral_letter") is None
    assert config.kind_config("paws-and-co", "invoice") is None


def test_the_committed_kinds_carry_their_business_settings() -> None:
    config = load()
    paws = config.kind_config("paws-and-co", "vaccination_record")
    sunrise = config.kind_config("sunrise-clinic", "referral_letter")
    assert paws is not None and sunrise is not None

    assert [v.id for v in paws.enum_values["species"]] == ["dog", "cat", "rabbit"]
    assert sunrise.always_review == {"policy_number"}
    assert paws.retention_days == sunrise.retention_days == 30
    for kind in (paws, sunrise):
        assert kind.output_model is kind.compiled.output_model
        assert "business_id" not in kind.output_model.model_fields
        dated = [s for s in kind.template.fields if s.type is FieldType.DATE]
        assert dated and kind.date_formats


def test_a_veterinary_file_enabling_a_human_healthcare_kind_is_refused(tmp_path: Path) -> None:
    # "A's file never selects B's template": the kind is looked up under the business's own type.
    folder = copy_extraction(tmp_path / "extraction")
    data = read_json(folder / SUNRISE)
    edit_json(folder / PAWS, kinds=data["kinds"])

    assert refused(folder) == (
        "businesses/paws-and-co.json: kinds.0.kind has no template for the business's type"
    )


def test_claiming_the_other_type_to_reach_its_template_is_refused(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    edit_json(
        folder / PAWS,
        business_type="human_healthcare",
        kinds=read_json(folder / SUNRISE)["kinds"],
    )

    assert refused(folder) == (
        "businesses/paws-and-co.json and the corpus manifest name different business types"
        " (the template is selected by the type, so both must agree)"
    )


def test_a_business_without_a_corpus_manifest_is_refused(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    corpus = write_corpus(tmp_path / "corpus", {"sunrise-clinic": "human_healthcare"})

    assert refused(folder, corpus) == (
        "businesses/paws-and-co.json: paws-and-co/manifest.json: no corpus manifest in the"
        " corpus folder (VECTAPILOT_CORPUS_DIR)"
    )


def test_a_kind_enabled_without_synthetic_true_stops_loading(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    edit_json(folder / SUNRISE, synthetic=False)

    message = refused(folder)
    assert message.startswith("businesses/sunrise-clinic.json is not a valid extraction")
    assert "only with synthetic: true" in message


def test_a_mixed_date_order_stops_loading(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    data = read_json(folder / SUNRISE)
    data["kinds"][0]["date_formats"] = ["DD/MM/YYYY", "MM/DD/YYYY"]
    write_json(folder / SUNRISE, data)

    assert "mixes day-first and month-first" in refused(folder)


def test_a_loosening_business_file_stops_loading(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    data = read_json(folder / PAWS)
    data["kinds"][0]["enum_values"] = [{"field": "species", "values": ["dog", "lizard"]}]
    write_json(folder / PAWS, data)

    message = refused(folder)
    assert message == (
        "businesses/paws-and-co.json: kinds.0: enum_values.0.values are the template's ids"
        " (E5: tighten, never loosen)"
    )


def test_an_unused_broken_template_still_stops_loading(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    target = folder / "templates" / "ecommerce" / "invoice.json"
    target.parent.mkdir()
    write_json(target, read_json(folder / VACCINATION) | {"business_type": "ecommerce"})

    assert refused(folder) == (
        "templates/ecommerce/invoice.json names a different business_type or kind than its path"
    )


def test_a_template_under_another_types_folder_is_refused(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    (folder / VACCINATION).rename(folder / "templates" / "human_healthcare" / VACCINATION.name)

    assert refused(folder) == (
        "templates/human_healthcare/vaccination_record.json names a different business_type or"
        " kind than its path"
    )


def test_an_invalid_template_names_its_file(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    data = read_json(folder / REFERRAL)
    data["fields"][0]["labels"] = [f"Patient‮{MARKER}"]
    write_json(folder / REFERRAL, data)

    message = refused(folder)
    assert message.startswith(
        "templates/human_healthcare/referral_letter.json is not a valid extraction template: "
    )
    assert "fields.0.labels.0" in message and "invisible characters" in message


# ── The folder's shape ───────────────────────────────────────────────────────────────────────


def test_dotfiles_and_the_readme_are_ignored(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    for place in (folder, folder / "templates", folder / "templates" / "veterinary"):
        (place / ".DS_Store").write_bytes(b"\x00")
    (folder / "businesses" / ".DS_Store").write_bytes(b"\x00")

    assert tuple(load(folder).businesses) == BUSINESSES


def test_missing_folders_are_refused(tmp_path: Path) -> None:
    assert "(VECTAPILOT_EXTRACTION_CONFIG_DIR) is missing" in refused(tmp_path / "nowhere")
    folder = copy_extraction(tmp_path / "extraction")
    assert "(VECTAPILOT_CORPUS_DIR) is missing" in refused(folder, tmp_path / "no-corpus")


@pytest.mark.parametrize("name", ["templates", "businesses"])
def test_a_missing_subfolder_is_refused(tmp_path: Path, name: str) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    shutil.rmtree(folder / name)
    assert refused(folder) == f"the extraction configuration folder has no {name} folder"


def test_an_empty_folder_holds_no_template_or_no_business(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    for path in (folder / "businesses").iterdir():
        path.unlink()
    assert refused(folder) == "the extraction configuration folder holds no business"
    for path in (folder / VACCINATION, folder / REFERRAL):
        path.unlink()
    assert refused(folder) == "the extraction configuration folder holds no template"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (
            lambda f: (f / "notes.txt").write_text("x"),
            "unexpected entry 'notes.txt' in the extraction configuration folder",
        ),
        (
            lambda f: (f / "templates" / "pharmacy").mkdir(),
            "templates: folder 'pharmacy' is not a known business type",
        ),
        (
            lambda f: (f / VACCINATION).rename(f / "templates" / "veterinary" / "vaccination.jsn"),
            "unexpected entry 'vaccination.jsn' in templates/veterinary",
        ),
        (
            lambda f: (f / "templates" / "veterinary.json").write_text("{}"),
            "templates: folder 'veterinary.json' is not a known business type",
        ),
        (
            lambda f: (f / PAWS).rename(f / "businesses" / "Paws_Co.json"),
            "businesses: file 'Paws_Co.json' is not named after a business id",
        ),
        (
            lambda f: (f / PAWS).rename(f / "businesses" / "paws-and-co.txt"),
            "unexpected entry 'paws-and-co.txt' in businesses",
        ),
        (
            lambda f: (f / "businesses" / "sunrise-clinic").mkdir(),
            "unexpected entry 'sunrise-clinic' in businesses",
        ),
    ],
)
def test_stray_or_misnamed_entries_are_refused_not_ignored(
    tmp_path: Path, make: object, message: str
) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    assert callable(make)
    make(folder)
    assert refused(folder) == message


def test_a_type_folder_that_is_a_file_is_refused(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    (folder / "templates" / "retail").write_text("x")
    assert refused(folder) == "templates: 'retail' is not a folder"


def test_a_file_naming_another_business_is_refused(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    edit_json(folder / PAWS, business_id="sunrise-clinic")
    assert refused(folder) == "businesses/paws-and-co.json names a different business_id"


def test_an_oversized_or_non_utf8_file_is_refused(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    path = folder / PAWS
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    assert refused(folder) == (f"businesses/paws-and-co.json is larger than {MAX_FILE_BYTES} bytes")
    path.write_bytes(b"\xff\xfe")
    assert refused(folder) == "businesses/paws-and-co.json is not UTF-8"


# ── Startup (fail fast) ──────────────────────────────────────────────────────────────────────


def test_without_the_setting_there_is_no_extraction_config() -> None:
    assert create_app(make_settings()).state.extraction is None


def test_the_app_loads_the_folder_at_startup() -> None:
    app = create_app(make_settings(extraction_config_dir=EXTRACTION_DIR, corpus_dir=CORPUS_DIR))

    assert tuple(app.state.extraction.businesses) == BUSINESSES


def test_the_app_refuses_to_start_on_an_invalid_folder(tmp_path: Path) -> None:
    folder = copy_extraction(tmp_path / "extraction")
    edit_json(folder / PAWS, business_type="human_healthcare")

    with pytest.raises(ExtractionConfigError, match="name different business types"):
        create_app(make_settings(extraction_config_dir=folder, corpus_dir=CORPUS_DIR))
    with pytest.raises(ExtractionConfigError, match="is missing"):
        create_app(make_settings(extraction_config_dir=tmp_path / "nowhere", corpus_dir=CORPUS_DIR))


def test_the_setting_needs_the_corpus_folder() -> None:
    with pytest.raises(ValueError, match=r"corpus_dir .* is required with extraction_config_dir"):
        make_settings(extraction_config_dir=EXTRACTION_DIR)


def test_the_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTAPILOT_EXTRACTION_CONFIG_DIR", str(EXTRACTION_DIR))
    monkeypatch.setenv("VECTAPILOT_CORPUS_DIR", str(CORPUS_DIR))

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
        redis_url="redis://127.0.0.1:1/0",
    )
    assert settings.extraction_config_dir == EXTRACTION_DIR


def test_compose_mounts_the_folder_read_only() -> None:
    compose = (EXTRACTION_DIR.parents[1] / "infra" / "compose.yaml").read_text(encoding="utf-8")

    assert "VECTAPILOT_EXTRACTION_CONFIG_DIR: /app/data/extraction\n" in compose
    assert "- ../data/extraction:/app/data/extraction:ro\n" in compose
