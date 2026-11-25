"""The widget configuration folder (M8 PR 1; rule 9, S8, S9, D9, D11, D12 (b)): the committed
sample files, and one refusal per rule. Every refusal names the file and the location, never a
value (a marker written into the file must never come back)."""

import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.conftest import make_settings
from tests.guards.helpers import (
    SAFETY_DIR,
    SAMPLE_CORPUS_DIR,
    SAMPLE_EXTRACTION_DIR,
    SANDBOX_DIR,
    load_safety,
    use_answer_key_selftest,
)
from tests.tools.helpers import write_corpus
from tests.widget.helpers import (
    BUSINESSES,
    REPO_DIR,
    WIDGET_DIR,
    copy_widget,
    edit_widget,
    guidance_of,
    read_json,
    widget_settings,
    write_json,
)
from vectapilot_api.auth.origins import parse_origin
from vectapilot_api.config import WIDGET_HTTP_ORIGIN_ENVIRONMENTS
from vectapilot_api.guards.config import GuardConfig, load_guard_dir
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.main import create_app
from vectapilot_api.widget.config import (
    ACCENT_TEXT_COLOR,
    DEFAULT_RETENTION_DAYS,
    MAX_FILE_BYTES,
    MIN_ACCENT_CONTRAST,
    WEAK_EVIDENCE_HANDOFF_REFUSED,
    WidgetConfigError,
    WidgetRegistry,
    contrast_ratio,
    load_widget_dir,
)

MARKER: Final = "zqxmarker"
"""Text a test writes where a value goes: no error may echo it."""
SUNRISE: Final = "sunrise-clinic"
PAWS: Final = "paws-and-co"


def load(folder: Path, *, guards: GuardConfig | None = None, http: bool = False) -> WidgetRegistry:
    return load_widget_dir(
        folder,
        SAMPLE_CORPUS_DIR,
        load_safety() if guards is None else guards,
        allow_http_loopback=http,
    )


def refused(folder: Path, *, http: bool = False, guards: GuardConfig | None = None) -> str:
    with pytest.raises(WidgetConfigError) as caught:
        load(folder, guards=guards, http=http)
    message = str(caught.value)
    assert MARKER not in message.lower()
    return message


def edited(tmp_path: Path, change: Callable[[dict[str, Any]], object]) -> Path:
    """A copy of the committed folder with Sunrise Clinic's file changed by `change`."""
    folder = copy_widget(tmp_path / "widget")
    edit_widget(folder, SUNRISE, change)
    return folder


def setting(key: str, value: object) -> Callable[[dict[str, Any]], object]:
    return lambda data: data.__setitem__(key, value)


def setting_text(key: str, value: object, index: int = 0) -> Callable[[dict[str, Any]], object]:
    return lambda data: data["texts"][index].__setitem__(key, value)


# ── The committed files ──────────────────────────────────────────────────────────────────────


def test_the_committed_folder_loads_both_sample_businesses() -> None:
    registry = load(WIDGET_DIR)

    assert sorted(registry.businesses) == list(BUSINESSES)
    for business_id, widget in registry.businesses.items():
        manifest = read_json(SAMPLE_CORPUS_DIR / business_id / "manifest.json")
        assert widget.enabled and widget.health and widget.file.synthetic
        assert widget.business_type == BusinessType(manifest["business_type"])
        # S8: the guidance is the guard file's, verbatim, never copied into the widget file.
        assert list(widget.guidance) == guidance_of(business_id)
        assert "guidance" not in read_json(WIDGET_DIR / "businesses" / f"{business_id}.json")
        assert list(widget.file.languages) == manifest["languages"]
        assert widget.file.languages[0] == manifest["default_language"]
        assert widget.file.handoff_retention_days == DEFAULT_RETENTION_DAYS  # P5
        for texts in widget.file.texts:
            # Q9 and D11 (b): shesha writes the follow-ups; absent means nothing is shown.
            assert texts.handoff_follow_up is None and texts.urgent_follow_up is None
            assert texts.contact_fallback
        # D9: the non-English texts are labelled unreviewed.
        others = [language for language in widget.file.languages if language != "en"]
        assert others and all(
            f"{language} not reviewed" in widget.file.review for language in others
        )


def test_the_committed_files_name_https_origins_only_so_they_load_everywhere() -> None:
    for business_id in BUSINESSES:
        data = read_json(WIDGET_DIR / "businesses" / f"{business_id}.json")
        for origin in data["allowed_origins"]:
            assert parse_origin(origin, allow_http_loopback=False) == origin
            assert origin.endswith(".example")  # RFC 2606: synthetic, never a real site


def test_the_committed_accent_colours_clear_the_contrast_rule() -> None:
    for widget in load(WIDGET_DIR).businesses.values():
        assert widget.file.accent_color is not None
        assert contrast_ratio(widget.file.accent_color, ACCENT_TEXT_COLOR) >= MIN_ACCENT_CONTRAST


def test_compose_mounts_the_folder_read_only_and_names_it() -> None:
    compose = (REPO_DIR / "infra" / "compose.yaml").read_text(encoding="utf-8")

    assert "VECTAPILOT_WIDGET_CONFIG_DIR: /app/data/widget\n" in compose
    assert "- ../data/widget:/app/data/widget:ro\n" in compose
    assert compose.count("../data/widget") == 1  # one mount


COMPOSE_ORIGIN: Final = "http://localhost:8000"
"""M8 PR 2 (PR 1's decision): compose's `VECTAPILOT_PUBLIC_ORIGIN`, the API's local origin."""


def test_compose_names_the_apis_public_origin() -> None:
    compose = (REPO_DIR / "infra" / "compose.yaml").read_text(encoding="utf-8")

    assert f"  VECTAPILOT_PUBLIC_ORIGIN: {COMPOSE_ORIGIN}\n" in compose
    assert compose.count("VECTAPILOT_PUBLIC_ORIGIN") == 1


def test_the_app_starts_with_every_folder_as_compose_sets_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # infra/compose.yaml's folders and origin together, in its environment (local).
    use_answer_key_selftest(monkeypatch)
    settings = make_settings(
        environment="local",
        tools_config_dir=SANDBOX_DIR,
        extraction_config_dir=SAMPLE_EXTRACTION_DIR,
        corpus_dir=SAMPLE_CORPUS_DIR,
        guard_config_dir=SAFETY_DIR,
        widget_config_dir=WIDGET_DIR,
        public_origin=COMPOSE_ORIGIN,
    )

    app = create_app(settings)

    assert sorted(app.state.widgets.businesses) == list(BUSINESSES)
    assert app.state.settings.public_origin == COMPOSE_ORIGIN


# ── Contrast (WCAG 2.2) ──────────────────────────────────────────────────────────────────────


def test_contrast_is_wcags_ratio() -> None:
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0)
    assert contrast_ratio("#767676", "#ffffff") == contrast_ratio("#ffffff", "#767676")
    # The well-known AA boundary for grey on white: #767676 is about 4.54:1, #777777 about 4.48:1.
    assert contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert contrast_ratio("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)


@pytest.mark.parametrize("color", ["#767676", "#000000", "#0f5c8c"])
def test_an_accent_at_or_above_the_ratio_loads(tmp_path: Path, color: str) -> None:
    widget = load(edited(tmp_path, setting("accent_color", color))).businesses[SUNRISE]

    assert widget.file.accent_color == color


def test_without_an_accent_there_is_none(tmp_path: Path) -> None:
    folder = edited(tmp_path, lambda data: data.pop("accent_color"))

    assert load(folder).businesses[SUNRISE].file.accent_color is None


@pytest.mark.parametrize("color", ["#777777", "#eeeeee", "#ffff00"])
def test_an_accent_below_the_ratio_is_refused(tmp_path: Path, color: str) -> None:
    message = refused(edited(tmp_path, setting("accent_color", color)))

    assert "accent_color has less than 4.5:1 contrast" in message
    assert color not in message


@pytest.mark.parametrize("color", ["#0F5C8C", "#fff", "red", "0f5c8c", "#0f5c8cff", ""])
def test_an_accent_that_is_not_lowercase_rrggbb_is_refused(tmp_path: Path, color: str) -> None:
    message = refused(edited(tmp_path, setting("accent_color", color)))

    assert "accent_color" in message


# ── Origins: strict `scheme://host[:port]` (they are written into a CSP header) ────────────────

BAD_ORIGINS: Final = [
    pytest.param(f"https://{MARKER}.example/", id="a slash"),
    pytest.param(f"https://{MARKER}.example/widget", id="a path"),
    pytest.param(f"https://{MARKER}.example?x=1", id="a query"),
    pytest.param(f"https://{MARKER}.example#x", id="a fragment"),
    pytest.param(f"https://*.{MARKER}.example", id="a wildcard"),
    pytest.param("*", id="a bare wildcard"),
    pytest.param("'none'", id="a CSP keyword"),
    pytest.param("'self'", id="another CSP keyword"),
    pytest.param(f"https://{MARKER}.example ", id="trailing space"),
    pytest.param(f" https://{MARKER}.example", id="leading space"),
    pytest.param(f"https://{MARKER}.example\n", id="a newline"),
    pytest.param(f"https://{MARKER}\t.example", id="a tab"),
    pytest.param(f"https://{MARKER}.example;", id="a semicolon"),
    pytest.param(f"https://{MARKER}.example; script-src *", id="a second directive"),
    pytest.param(f"https://{MARKER}.example,https://b.example", id="a comma"),
    pytest.param(f"https://{MARKER.upper()}.example", id="an uppercase host"),
    pytest.param(f"HTTPS://{MARKER}.example", id="an uppercase scheme"),
    pytest.param(f"https://{MARKER}.example:443", id="https's default port"),
    pytest.param(f"https://{MARKER}.example:0", id="port 0"),
    pytest.param(f"https://{MARKER}.example:0443", id="a leading zero"),
    pytest.param(f"https://{MARKER}.example:65536", id="a port above 65535"),
    pytest.param(f"https://{MARKER}.example:", id="an empty port"),
    pytest.param(f"https://user@{MARKER}.example", id="user info"),
    pytest.param(f"https://{MARKER}.example.", id="a trailing dot"),
    pytest.param(f"https://-{MARKER}.example", id="a label starting with a hyphen"),
    pytest.param(f"https://{MARKER}_x.example", id="an underscore"),
    pytest.param("https://10.0.0.1", id="a private IP"),
    pytest.param("https://127.0.0.2", id="another loopback IP"),
    pytest.param("https://0x7f000001", id="an IP as one hex number"),
    pytest.param("https://1.2.3.0x4", id="an IP with a hex last label"),
    pytest.param("https://0x7f.0x0.0x0.0x1", id="an IP in hex labels"),
    pytest.param("https://[::1]", id="IPv6"),
    pytest.param(f"http://{MARKER}.example", id="http on a public host"),
    pytest.param("http://localhost:3000", id="http on localhost, not allowed here"),
    pytest.param("http://127.0.0.1:3000", id="http on 127.0.0.1, not allowed here"),
    pytest.param(f"ftp://{MARKER}.example", id="another scheme"),
    pytest.param("javascript:alert(1)", id="javascript"),
    pytest.param("https://", id="no host"),
    pytest.param("", id="empty"),
]


@pytest.mark.parametrize("origin", BAD_ORIGINS)
def test_each_origin_form_but_the_strict_one_is_refused(tmp_path: Path, origin: str) -> None:
    message = refused(edited(tmp_path, setting("allowed_origins", [origin])))

    assert message.startswith(f"businesses/{SUNRISE}.json: allowed_origins.0: ")
    if origin.strip():
        assert origin.strip() not in message


def test_http_on_a_default_port_is_refused_even_where_http_is_allowed(tmp_path: Path) -> None:
    message = refused(
        edited(tmp_path, setting("allowed_origins", ["http://localhost:80"])), http=True
    )

    assert "allowed_origins.0: an origin never names its scheme's default port" in message


@pytest.mark.parametrize(
    "origin",
    [
        "https://www.example.org",
        "https://shop.example:8443",
        "https://localhost",
        "https://127.0.0.1:8443",
    ],
)
def test_the_strict_form_loads(tmp_path: Path, origin: str) -> None:
    widget = load(edited(tmp_path, setting("allowed_origins", [origin]))).businesses[SUNRISE]

    assert widget.file.allowed_origins == (origin,)


@pytest.mark.parametrize("origin", ["http://localhost:4173", "http://127.0.0.1:4173"])
def test_http_on_a_loopback_host_loads_only_where_allowed(tmp_path: Path, origin: str) -> None:
    folder = edited(tmp_path, setting("allowed_origins", [origin]))

    assert load(folder, http=True).businesses[SUNRISE].file.allowed_origins == (origin,)
    assert "allowed_origins.0: an origin is https" in refused(folder, http=False)


def test_http_loopback_origins_are_allowed_only_in_local() -> None:
    assert frozenset({"local"}) == WIDGET_HTTP_ORIGIN_ENVIRONMENTS  # the plan: "in local"


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
def test_the_app_applies_the_local_only_http_rule(tmp_path: Path, environment: str) -> None:
    folder = edited(tmp_path, setting("allowed_origins", ["http://localhost:4173"]))
    settings = widget_settings(environment=environment, widget_config_dir=folder)

    if environment == "local":
        assert SUNRISE in create_app(settings).state.widgets.businesses
    else:
        with pytest.raises(WidgetConfigError, match=r"allowed_origins\.0: an origin is https"):
            create_app(settings)


def test_an_origin_listed_twice_is_refused(tmp_path: Path) -> None:
    twice = ["https://a.example", "https://a.example"]

    assert "each allowed origin is listed once" in refused(
        edited(tmp_path, setting("allowed_origins", twice))
    )


def test_an_enabled_widget_names_at_least_one_origin(tmp_path: Path) -> None:
    assert "an enabled widget names at least one allowed origin" in refused(
        edited(tmp_path, setting("allowed_origins", []))
    )


def test_a_disabled_widget_may_name_none_and_is_not_served(tmp_path: Path) -> None:
    def disable(data: dict[str, Any]) -> None:
        data["enabled"] = False
        data["allowed_origins"] = []

    registry = load(edited(tmp_path, disable))

    assert not registry.businesses[SUNRISE].enabled
    assert registry.enabled(SUNRISE) is None
    assert registry.enabled(PAWS) is registry.businesses[PAWS]
    assert registry.enabled("no-such-business") is None


def test_more_origins_than_the_bound_are_refused(tmp_path: Path) -> None:
    many = [f"https://site-{i}.example" for i in range(11)]

    assert "allowed_origins" in refused(edited(tmp_path, setting("allowed_origins", many)))


# ── S8: an enabled health widget needs its guard guidance ──────────────────────────────────────


def without_sunrise_guard_file(tmp_path: Path) -> GuardConfig:
    """The real guard folder without Sunrise Clinic's business file: the guard loader refuses a
    health business file without guidance (G4), so a missing file is how a health business ends
    up with none."""
    guard = Path(shutil.copytree(SAFETY_DIR, tmp_path / "safety"))
    (guard / "businesses" / f"{SUNRISE}.json").unlink()
    return load_guard_dir(guard, SAMPLE_CORPUS_DIR)


def test_an_enabled_health_widget_without_guard_guidance_is_refused(tmp_path: Path) -> None:
    guards = without_sunrise_guard_file(tmp_path)

    message = refused(WIDGET_DIR, guards=guards)

    assert message == (
        f"businesses/{SUNRISE}.json: an enabled widget of a health business needs its guard"
        " guidance (VECTAPILOT_GUARD_CONFIG_DIR, businesses/<business_id>.json; S8)"
    )


def test_without_a_guard_folder_an_enabled_health_widget_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WidgetConfigError, match="needs its guard guidance"):
        load_widget_dir(WIDGET_DIR, SAMPLE_CORPUS_DIR, None, allow_http_loopback=False)
    with pytest.raises(WidgetConfigError, match="needs its guard guidance"):
        create_app(widget_settings(guard_config_dir=None))


def test_a_disabled_health_widget_without_guidance_loads(tmp_path: Path) -> None:
    folder = edited(tmp_path, setting("enabled", False))

    widget = load(folder, guards=without_sunrise_guard_file(tmp_path)).businesses[SUNRISE]

    assert (widget.enabled, widget.health, widget.guidance) == (False, True, ())


def retail_folder(tmp_path: Path) -> tuple[Path, Path]:
    """A widget folder and a corpus for one fictional `retail` business, `rt-shop`."""
    corpus = write_corpus(tmp_path / "corpus", {"rt-shop": BusinessType.RETAIL.value})
    folder = tmp_path / "widget"
    (folder / "businesses").mkdir(parents=True)
    data = read_json(WIDGET_DIR / "businesses" / f"{PAWS}.json")
    data["business_id"] = "rt-shop"
    data["languages"] = ["en"]
    data["texts"] = [data["texts"][0]]
    write_json(folder / "businesses" / "rt-shop.json", data)
    return folder, corpus


def test_a_non_health_widget_without_guidance_loads_and_shows_none(tmp_path: Path) -> None:
    folder, corpus = retail_folder(tmp_path)

    registry = load_widget_dir(folder, corpus, None, allow_http_loopback=False)

    widget = registry.businesses["rt-shop"]
    assert (widget.enabled, widget.health, widget.guidance) == (True, False, ())


def test_a_guard_file_that_says_health_makes_the_widget_health(tmp_path: Path) -> None:
    # The union the docstring promises: the manifest says retail, but the guard loader (which
    # unions the manifest's type with the tools' and extraction's) says health, so the widget is
    # health and its 429 carries the guidance (create_app refuses a type mismatch first today).
    folder, corpus = retail_folder(tmp_path)
    guarded = SimpleNamespace(health=True, guidance=("Synthetic emergency line.",))
    guards = cast(GuardConfig, SimpleNamespace(businesses={"rt-shop": guarded}))

    widget = load_widget_dir(folder, corpus, guards, allow_http_loopback=False).businesses[
        "rt-shop"
    ]

    assert (widget.health, widget.guidance) == (True, ("Synthetic emergency line.",))


# ── Languages and texts ──────────────────────────────────────────────────────────────────────


def test_a_language_the_manifest_does_not_support_is_refused(tmp_path: Path) -> None:
    def add_telugu(data: dict[str, Any]) -> None:
        data["languages"].append("te")
        data["texts"].append({**data["texts"][0], "language": "te"})

    message = refused(edited(tmp_path, add_telugu))

    assert message == (
        f"businesses/{SUNRISE}.json: languages.2: a UI language its corpus manifest does not"
        " support"
    )


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda data: data["texts"].pop(), id="a language without texts"),
        pytest.param(
            lambda data: data["texts"].append(data["texts"][0]), id="a language written twice"
        ),
        pytest.param(
            lambda data: data["texts"][1].__setitem__("language", "te"), id="texts of another"
        ),
    ],
)
def test_texts_have_exactly_one_entry_per_language(
    tmp_path: Path, change: Callable[[dict[str, Any]], object]
) -> None:
    assert "texts has exactly one entry per language in languages" in refused(
        edited(tmp_path, change)
    )


def test_a_language_listed_twice_is_refused(tmp_path: Path) -> None:
    assert "each language is listed once" in refused(
        edited(tmp_path, setting("languages", ["en", "hi", "en"]))
    )


@pytest.mark.parametrize(
    "languages", [[], ["EN"], ["english"], ["en_IN"], [f"en-{'x' * 33}"]], ids=str
)
def test_a_language_list_out_of_form_is_refused(tmp_path: Path, languages: list[str]) -> None:
    assert "languages" in refused(edited(tmp_path, setting("languages", languages)))


def test_texts_for_names_one_language() -> None:
    widget = load(WIDGET_DIR).businesses[SUNRISE]

    assert widget.file.texts_for("hi").language == "hi"
    with pytest.raises(KeyError):
        widget.file.texts_for("te")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("greeting", ""),
        ("greeting", "x" * 301),
        ("greeting", f"two\nlines {MARKER}"),
        ("handoff_follow_up", "x" * 301),
        ("urgent_follow_up", f"two\nlines {MARKER}"),
        ("contact_fallback", []),
        ("contact_fallback", [f"line {i}" for i in range(6)]),
        ("contact_fallback", ["x" * 201]),
        ("quick_replies", ["Same", "Same"]),
        ("quick_replies", [f"reply {i}" for i in range(5)]),
        ("quick_replies", ["x" * 81]),
        ("quick_replies", [""]),
    ],
    ids=lambda value: str(value)[:24],
)
def test_texts_out_of_their_bounds_are_refused(tmp_path: Path, key: str, value: object) -> None:
    message = refused(edited(tmp_path, setting_text(key, value)))

    assert "texts.0" in message


def test_the_follow_ups_are_optional_and_shown_as_written(tmp_path: Path) -> None:
    def follow_ups(data: dict[str, Any]) -> None:
        data["texts"][0]["handoff_follow_up"] = "A member of our team will reply here."
        data["texts"][0]["urgent_follow_up"] = "Staff have been alerted."

    texts = load(edited(tmp_path, follow_ups)).businesses[SUNRISE].file.texts_for("en")

    assert texts.handoff_follow_up == "A member of our team will reply here."
    assert texts.urgent_follow_up == "Staff have been alerted."
    assert texts.quick_replies  # the sample's own


# ── The rest of the file ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("days", [0, 91, -1, "30", 1.5, True], ids=repr)
def test_retention_outside_one_to_ninety_days_is_refused(tmp_path: Path, days: object) -> None:
    assert "handoff_retention_days" in refused(
        edited(tmp_path, setting("handoff_retention_days", days))
    )


@pytest.mark.parametrize("days", [1, 90])
def test_retention_at_its_bounds_loads(tmp_path: Path, days: int) -> None:
    folder = edited(tmp_path, setting("handoff_retention_days", days))

    assert load(folder).businesses[SUNRISE].file.handoff_retention_days == days


def test_retention_defaults_to_thirty_days(tmp_path: Path) -> None:
    folder = edited(tmp_path, lambda data: data.pop("handoff_retention_days"))

    assert load(folder).businesses[SUNRISE].file.handoff_retention_days == 30  # P5


def test_a_file_that_is_not_synthetic_is_refused(tmp_path: Path) -> None:
    assert "a widget file is synthetic while Q4c is open" in refused(
        edited(tmp_path, setting("synthetic", False))
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("enabled", "true"),
        ("display_name", ""),
        ("display_name", "x" * 81),
        ("display_name", f"Two\nlines {MARKER}"),
        ("review", "x" * 201),
        ("business_id", "Sunrise Clinic"),
    ],
    ids=lambda value: str(value)[:24],
)
def test_fields_out_of_their_form_are_refused(tmp_path: Path, key: str, value: object) -> None:
    assert key in refused(edited(tmp_path, setting(key, value)))


def test_an_unknown_key_is_refused_without_its_name(tmp_path: Path) -> None:
    message = refused(edited(tmp_path, setting(f"{MARKER}_extra", MARKER)))

    assert "(unknown key)" in message


@pytest.mark.parametrize("key", ["business_id", "enabled", "texts", "languages", "synthetic"])
def test_a_required_key_missing_is_refused(tmp_path: Path, key: str) -> None:
    assert key in refused(edited(tmp_path, lambda data: data.pop(key)))


# ── The folder ───────────────────────────────────────────────────────────────────────────────


def test_a_missing_folder_is_refused(tmp_path: Path) -> None:
    message = refused(tmp_path / "nowhere")

    assert message == "the widget configuration folder (VECTAPILOT_WIDGET_CONFIG_DIR) is missing"


def test_a_missing_corpus_folder_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WidgetConfigError, match=r"the corpus folder \(VECTAPILOT_CORPUS_DIR\)"):
        load_widget_dir(WIDGET_DIR, tmp_path / "nowhere", None, allow_http_loopback=False)


def test_a_folder_without_its_businesses_folder_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "widget"
    folder.mkdir()
    (folder / "README.md").write_text("notes\n", encoding="utf-8")

    assert refused(folder) == "the widget configuration folder has no businesses folder"


def test_the_readme_and_hidden_entries_are_ignored(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    (folder / ".DS_Store").write_bytes(b"\0")
    (folder / "businesses" / ".DS_Store").write_bytes(b"\0")

    assert sorted(load(folder).businesses) == list(BUSINESSES)


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        pytest.param(
            lambda folder: (folder / f"{MARKER}.txt").write_text("x"),
            "unexpected entry",
            id="a file at the top",
        ),
        pytest.param(
            lambda folder: (folder / "README.md").unlink() or (folder / "README.md").mkdir(),
            "unexpected entry",
            id="a README that is a folder",
        ),
        pytest.param(
            lambda folder: (folder / "businesses" / f"{MARKER}.yaml").write_text("x"),
            "unexpected entry",
            id="not a json file",
        ),
        pytest.param(
            lambda folder: (folder / "businesses" / f"{MARKER}.json").mkdir(),
            "unexpected entry",
            id="a folder named like a file",
        ),
    ],
)
def test_an_unexpected_entry_is_refused(
    tmp_path: Path, make: Callable[[Path], object], expected: str
) -> None:
    folder = copy_widget(tmp_path / "widget")
    make(folder)

    with pytest.raises(WidgetConfigError) as caught:
        load(folder)
    message = str(caught.value)

    assert expected in message  # the entry's own name is shown: a file name, never a value


def test_a_file_not_named_after_a_business_id_is_refused(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    shutil.copy(folder / "businesses" / f"{SUNRISE}.json", folder / "businesses" / "Sunrise.json")

    assert refused(folder) == "businesses/Sunrise.json: the file is not named after a business id"


def test_a_file_naming_another_business_is_refused(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    shutil.copy(folder / "businesses" / f"{SUNRISE}.json", folder / "businesses" / "rt-other.json")

    assert refused(folder) == "businesses/rt-other.json names a different business_id"


def test_a_file_over_the_bound_is_refused(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    path = folder / "businesses" / f"{SUNRISE}.json"
    path.write_bytes(path.read_bytes() + b" " * MAX_FILE_BYTES)

    assert refused(folder) == f"businesses/{SUNRISE}.json is larger than {MAX_FILE_BYTES} bytes"


def test_a_file_that_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = copy_widget(tmp_path / "widget")
    unreadable = folder / "businesses" / f"{SUNRISE}.json"
    read_bytes = Path.read_bytes

    def refusing(path: Path) -> bytes:
        if path == unreadable:
            raise PermissionError(f"denied {MARKER}")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refusing)

    assert refused(folder) == f"businesses/{SUNRISE}.json cannot be read"


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    (folder / "businesses" / f"{SUNRISE}.json").write_bytes(b'{"business_id": "\xff"}')

    assert refused(folder) == f"businesses/{SUNRISE}.json is not UTF-8"


def test_a_file_that_is_not_json_is_refused(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    (folder / "businesses" / f"{SUNRISE}.json").write_text(f"{{{MARKER}", encoding="utf-8")

    assert f"businesses/{SUNRISE}.json is not a valid widget file" in refused(folder)


def test_a_business_without_a_corpus_manifest_is_refused(tmp_path: Path) -> None:
    folder, _ = retail_folder(tmp_path)

    message = refused(folder)  # the sample corpus has no `rt-shop`

    assert message == (
        "businesses/rt-shop.json: rt-shop/manifest.json: no corpus manifest in the corpus folder"
        " (VECTAPILOT_CORPUS_DIR)"
    )


def test_a_manifest_naming_another_business_is_refused(tmp_path: Path) -> None:
    folder, corpus = retail_folder(tmp_path)
    manifest = corpus / "rt-shop" / "manifest.json"
    data = read_json(manifest)
    data["business_id"] = "rt-other"
    write_json(manifest, data)

    with pytest.raises(WidgetConfigError, match=r"rt-shop/manifest\.json names another business"):
        load_widget_dir(folder, corpus, None, allow_http_loopback=False)


def test_an_invalid_manifest_is_refused_without_its_values(tmp_path: Path) -> None:
    folder, corpus = retail_folder(tmp_path)
    manifest = corpus / "rt-shop" / "manifest.json"
    data = read_json(manifest)
    data["languages"] = [MARKER.upper()]
    write_json(manifest, data)

    with pytest.raises(WidgetConfigError) as caught:
        load_widget_dir(folder, corpus, None, allow_http_loopback=False)

    assert str(caught.value).startswith("businesses/rt-shop.json: ")
    assert MARKER not in str(caught.value).lower()


def test_a_manifest_with_an_unknown_type_is_refused(tmp_path: Path) -> None:
    folder, corpus = retail_folder(tmp_path)
    manifest = corpus / "rt-shop" / "manifest.json"
    data = read_json(manifest)
    data["business_type"] = "bakery"
    write_json(manifest, data)

    with pytest.raises(WidgetConfigError, match="its business_type is not a known type"):
        load_widget_dir(folder, corpus, None, allow_http_loopback=False)


# ── Settings and startup ─────────────────────────────────────────────────────────────────────


def test_the_app_loads_the_folder_at_startup() -> None:
    app = create_app(widget_settings())

    assert sorted(app.state.widgets.businesses) == list(BUSINESSES)


def test_a_bad_widget_file_stops_startup(tmp_path: Path) -> None:
    folder = edited(tmp_path, setting("accent_color", "#777777"))

    with pytest.raises(WidgetConfigError, match="accent_color"):
        create_app(widget_settings(widget_config_dir=folder))


def test_without_the_setting_no_business_has_a_widget() -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.app.state.widgets.businesses == {}  # type: ignore[attr-defined]


def test_the_folder_needs_the_corpus_folder() -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(widget_config_dir=WIDGET_DIR, corpus_dir=None)

    assert "corpus_dir (VECTAPILOT_CORPUS_DIR) is required with widget_config_dir" in str(
        caught.value
    )


# ── M8 PR 2: the weak-evidence `handoff` outcome while a widget is enabled (noah's review) ─────


def test_a_weak_evidence_handoff_outcome_is_refused_while_a_widget_is_enabled(
    tmp_path: Path,
) -> None:
    # noah's PR 2 review, option (b) [proposal]: an answer labelled `handoff` stores no hand-off,
    # so with a widget enabled no person would ever see it. Fixed text, setting names only.
    with pytest.raises(WidgetConfigError) as caught:
        create_app(widget_settings(rag_weak_evidence_outcome="handoff"))
    assert str(caught.value) == WEAK_EVIDENCE_HANDOFF_REFUSED

    # The positive controls: the default outcome starts, and so does `handoff` with every widget
    # disabled or no widget folder at all.
    assert create_app(widget_settings()).state.widgets.any_enabled()
    folder = copy_widget(tmp_path / "widget")
    for business_id in BUSINESSES:
        edit_widget(folder, business_id, setting("enabled", False))
    disabled = widget_settings(rag_weak_evidence_outcome="handoff", widget_config_dir=folder)
    assert not create_app(disabled).state.widgets.any_enabled()
    none = widget_settings(rag_weak_evidence_outcome="handoff", widget_config_dir=None)
    assert create_app(none).state.widgets.businesses == {}
