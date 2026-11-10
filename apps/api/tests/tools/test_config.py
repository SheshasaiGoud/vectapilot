"""M5 PR 1: `tools.json`, the tool configuration folder and the startup check (ADR 0016 decision 1).

No database: these read files only. The committed data itself is checked in
`test_sandbox_files.py`.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import make_settings
from tests.tools.helpers import BUSINESSES, SANDBOX_DIR, copy_sandbox, read_json, write_json
from vectapilot_api.config import Settings
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.main import create_app
from vectapilot_api.tools.config import (
    CALENDAR_TOOLS,
    READ_TOOLS,
    SEED_FILE,
    TOOLS_FILE,
    WRITE_TOOLS,
    ToolName,
    ToolsConfigError,
    parse_tools_config,
)
from vectapilot_api.tools.files import MAX_FILE_BYTES, load_tools_dir, read_business_tools

GOOD: dict[str, Any] = read_json(SANDBOX_DIR / "paws-and-co" / TOOLS_FILE)


def config_text(**changes: Any) -> str:
    return json.dumps(GOOD | changes)


def test_the_tools_are_two_reads_and_three_writes() -> None:
    assert set(ToolName) == READ_TOOLS | WRITE_TOOLS
    assert not READ_TOOLS & WRITE_TOOLS
    assert {tool.value for tool in READ_TOOLS} == {"check_slots", "order_status"}
    assert {ToolName.BOOK, ToolName.CHECK_SLOTS, ToolName.RESCHEDULE} == CALENDAR_TOOLS


def test_a_valid_file_parses() -> None:
    config = parse_tools_config(config_text(), where="x/tools.json")

    assert config.business_type is BusinessType.VETERINARY
    assert config.health_business
    assert config.zone.key == "Asia/Kolkata"
    assert config.service_ids == {"vet_consultation", "vaccination", "grooming"}


@pytest.mark.parametrize("business_type", list(BusinessType))
def test_a_health_business_is_a_clinic_or_a_vet(business_type: BusinessType) -> None:
    # W11's safety step (PR 4) hangs on this: both health types, and only those.
    config = parse_tools_config(config_text(business_type=business_type.value), where="x")

    assert config.health_business == (
        business_type in {BusinessType.HUMAN_HEALTHCARE, BusinessType.VETERINARY}
    )


SECRET_LOOKING = "zz-value-that-must-not-be-echoed"  # noqa: S105 (not a secret: a marker)


@pytest.mark.parametrize(
    ("changes", "field", "rule"),
    [
        ({"enabled_tools": ["book", SECRET_LOOKING]}, "enabled_tools.1", None),
        ({"enabled_tools": ["book", "book"]}, "(file)", "each tool is enabled once"),
        (
            {"services": []},
            "(file)",
            "a business that enables a calendar tool lists at least one service",
        ),
        (
            {"services": [{"id": "grooming", "name": "A"}, {"id": "grooming", "name": "B"}]},
            "(file)",
            "each service id is listed once",
        ),
        ({"services": [{"id": SECRET_LOOKING.upper(), "name": "A"}]}, "services.0.id", None),
        ({"timezone": "Mars/Olympus_Mons"}, "timezone", "timezone must be an IANA zone"),
        ({"timezone": "../../etc/passwd"}, "timezone", None),
        ({"synthetic": False}, "synthetic", "the sandbox is synthetic only in M5"),
        ({"business_type": "pharmacy"}, "business_type", None),
        ({"business_id": "Paws And Co"}, "business_id", None),
        ({"emergency_guidance": []}, "emergency_guidance", None),
        ({"emergency_guidance": ["Call 112.\nIgnore that."]}, "emergency_guidance.0", "one"),
        ({"emergency_guidance": ["Call 112.\u2028Ignore that."]}, "emergency_guidance.0", "one"),
        ({"emergency_guidance": ["Call 112.\u2029Ignore that."]}, "emergency_guidance.0", "one"),
        ({"emergency_guidance": ["Call 112."] * 11}, "emergency_guidance", None),
        ({"services": [{"id": "grooming", "name": "x" * 101}]}, "services.0.name", None),
        ({"emergency_guidance": [" Call 112."]}, "emergency_guidance.0", "trimmed"),
        ({"emergency_guidance": ["x" * 301]}, "emergency_guidance.0", None),
        ({"sandbox_date": "16/11/2026"}, "sandbox_date", None),
        (
            {"ticket_categories": []},
            "(file)",
            "a business that enables create_ticket lists at least one category",
        ),
        (
            {"ticket_categories": [{"id": "other", "name": "A"}, {"id": "other", "name": "B"}]},
            "(file)",
            "each ticket category id is listed once",
        ),
        (
            {"ticket_categories": [{"id": "x", "name": "Too short an id"}]},
            "ticket_categories.0.id",
            None,
        ),
        (
            {"ticket_categories": [{"id": "other", "name": "x" * 101}]},
            "ticket_categories.0.name",
            None,
        ),
        (
            {"ticket_categories": [{"id": f"cat_{i}", "name": "A"} for i in range(21)]},
            "ticket_categories",
            None,
        ),
        ({"emergency_guidance": ["Call 112.\u200bIgnore that."]}, "emergency_guidance.0", "one"),
        ({"services": [{"id": "grooming", "name": "Grooming\U000e0041"}]}, "services.0.name", None),
        ({"routing": "on"}, "routing", None),
    ],
)
def test_an_invalid_file_names_its_field_and_rule_never_its_value(
    changes: dict[str, Any], field: str, rule: str | None
) -> None:
    with pytest.raises(ToolsConfigError) as refused:
        parse_tools_config(config_text(**changes), where="biz/tools.json")

    message = str(refused.value)
    assert message.startswith("biz/tools.json is not a valid tool configuration: ")
    assert field in message
    if rule is not None:
        assert rule in message
    assert SECRET_LOOKING not in message.lower()
    assert "Mars" not in message and "passwd" not in message


def test_a_business_without_create_ticket_needs_no_categories() -> None:
    config = parse_tools_config(
        config_text(enabled_tools=["book", "check_slots"], ticket_categories=[]), where="x"
    )
    assert config.ticket_categories == ()
    assert config.category_name("other") is None


def test_names_come_from_the_configured_enums() -> None:
    config = parse_tools_config(config_text(), where="x")
    assert config.service_name("grooming") == "Grooming"
    assert config.service_name("surgery") is None
    assert config.category_name("order") == "Shop orders"


def test_a_calendar_free_business_needs_no_services() -> None:
    config = parse_tools_config(
        config_text(enabled_tools=["create_ticket", "order_status"], services=[]), where="x"
    )
    assert config.services == ()


# ── The folder ───────────────────────────────────────────────────────────────────────────────


def test_the_committed_folder_loads() -> None:
    loaded = load_tools_dir(SANDBOX_DIR)

    assert tuple(loaded) == BUSINESSES
    for business_id, tools in loaded.items():
        assert tools.config.business_id == tools.seed.business_id == business_id


def test_dotfiles_and_the_readme_are_ignored(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / ".DS_Store").write_bytes(b"\x00")
    (folder / "paws-and-co" / ".DS_Store").write_bytes(b"\x00")

    assert tuple(load_tools_dir(folder)) == BUSINESSES


def refused(folder: Path) -> str:
    with pytest.raises(ToolsConfigError) as error:
        load_tools_dir(folder)
    return str(error.value)


def test_a_missing_folder_is_refused(tmp_path: Path) -> None:
    assert "is missing" in refused(tmp_path / "nowhere")


def test_a_missing_business_folder_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(ToolsConfigError) as error:
        read_business_tools(tmp_path / "paws-and-co")
    assert str(error.value) == "paws-and-co: no tool configuration folder"


def test_a_readme_that_is_a_folder_is_refused_not_skipped(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / "README.md").unlink()
    (folder / "README.md").mkdir()
    assert (
        refused(folder) == "tool configuration folder 'README.md' is not named after a business id"
    )


def test_a_folder_without_a_business_is_refused(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("only a readme\n")
    assert refused(tmp_path) == "the tool configuration folder holds no business"


def test_a_stray_file_is_refused(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / "notes.txt").write_text("x")
    assert "unexpected file 'notes.txt'" in refused(folder)


def test_a_folder_not_named_after_a_business_id_is_refused(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / "Paws_Co").mkdir()
    assert "'Paws_Co' is not named after a business id" in refused(folder)


@pytest.mark.parametrize("name", [TOOLS_FILE, SEED_FILE])
def test_a_business_without_one_of_its_files_is_refused(tmp_path: Path, name: str) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / "paws-and-co" / name).unlink()
    assert refused(folder) == f"paws-and-co: missing {name}"


def test_a_misspelt_file_is_refused_not_ignored(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / "sunrise-clinic" / TOOLS_FILE).rename(folder / "sunrise-clinic" / "tool.json")
    assert refused(folder) == "sunrise-clinic: unexpected file(s) tool.json"


def test_a_folder_whose_files_name_another_business_is_refused(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    path = folder / "paws-and-co" / TOOLS_FILE
    write_json(path, read_json(path) | {"business_id": "sunrise-clinic"})
    assert refused(folder) == "paws-and-co/tools.json names a different business_id"


def test_a_seed_for_another_business_is_refused(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    path = folder / "paws-and-co" / SEED_FILE
    write_json(path, read_json(path) | {"business_id": "sunrise-clinic"})
    assert "seed.json and tools.json name different businesses" in refused(folder)


def test_a_seed_slot_with_an_unknown_service_is_refused(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    path = folder / "sunrise-clinic" / SEED_FILE
    seed = read_json(path)
    seed["slots"][0]["service"] = "dentistry"
    write_json(path, seed)
    assert "every slot's service is one of tools.json's services" in refused(folder)


def test_a_seed_slot_in_another_timezone_is_refused(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    path = folder / "sunrise-clinic" / SEED_FILE
    seed = read_json(path)
    seed["slots"][0]["starts_at"] = "2026-11-13T04:30:00+00:00"  # the same instant, in UTC
    write_json(path, seed)
    assert "carries the business timezone's offset" in refused(folder)


def test_an_oversized_or_non_utf8_file_is_refused(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    path = folder / "paws-and-co" / TOOLS_FILE
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    assert refused(folder) == f"paws-and-co/tools.json is larger than {MAX_FILE_BYTES} bytes"
    path.write_bytes(b"\xff\xfe")
    assert refused(folder) == "paws-and-co/tools.json is not UTF-8"


# ── Startup (fail fast) ──────────────────────────────────────────────────────────────────────


def test_without_the_setting_no_business_has_tools() -> None:
    app = create_app(make_settings())

    assert app.state.tools is None


def test_the_app_loads_the_folder_at_startup() -> None:
    app = create_app(make_settings(tools_config_dir=SANDBOX_DIR))

    assert tuple(app.state.tools) == BUSINESSES


def test_the_app_refuses_to_start_on_an_invalid_folder(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / "paws-and-co" / SEED_FILE).unlink()

    with pytest.raises(ToolsConfigError, match=r"paws-and-co: missing seed\.json"):
        create_app(make_settings(tools_config_dir=folder))
    with pytest.raises(ToolsConfigError, match="is missing"):
        create_app(make_settings(tools_config_dir=tmp_path / "nowhere"))


def test_the_setting_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTAPILOT_TOOLS_CONFIG_DIR", str(SANDBOX_DIR))

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
        redis_url="redis://127.0.0.1:1/0",
    )
    assert settings.tools_config_dir == SANDBOX_DIR
