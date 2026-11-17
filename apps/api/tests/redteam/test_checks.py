"""M7 PR 2: `check` on a TEST-ONLY dataset in tmp_path (`redteam/checks.py`, `files.py`).

Each refusal the plan names has a test: a PII-shaped literal (every generator's output), a
missing or unknown placeholder type, an unknown business, a language the business does not list,
`te`, a G case without its obfuscation class (`test_cases.py`), and a label other than `hard` in
F, G-in, H and K. Every problem names the case id and field; no test output holds a value.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.redteam.helpers import PUBLISHED_PHONE, case, g_case, write_corpus, write_dataset
from vectapilot_api.evalset.checks import CheckResult
from vectapilot_api.redteam.checks import RedteamDataset, check_dataset
from vectapilot_api.redteam.files import read_dataset
from vectapilot_api.redteam.pii import PiiGenerator, PiiType, Placeholder

GEN = PiiGenerator()


def load(tmp_path: Path, cases: list[dict[str, Any]], **options: Any) -> RedteamDataset:
    corpus = write_corpus(tmp_path)
    return read_dataset(write_dataset(tmp_path, cases, **options), corpus)


def checked(tmp_path: Path, cases: list[dict[str, Any]], **options: Any) -> CheckResult:
    return check_dataset(load(tmp_path, cases, **options))


def only(result: CheckResult) -> str:
    assert len(result.problems) == 1, result.problems
    return result.problems[0]


CHUNK: Any = {"id": "c1", "text": "TEST-ONLY chunk", "audience": "public"}
REPLY: Any = {"action": "reply", "text": "TEST-ONLY reply"}
TICKET: Any = {"action": "call_tool", "tool": "create_ticket", "arguments": {"summary": "x"}}


def test_a_valid_dataset_passes(tmp_path: Path) -> None:
    result = checked(
        tmp_path,
        [
            case(),
            g_case(),
            g_case(id="g-002", language="hi", code_mixed=True, obfuscation="paraphrase"),
            case(
                id="c-001",
                category="C",
                business_id="rt-shop",
                surface="ticket_text",
                input="",
                fixture={"scripted_model": [TICKET]},
                expect="handoff",
            ),
            case(
                id="e-001",
                category="E",
                input="mail {{pii:email}} or {{pii:phone_in_dash}}",
                expect="masked:email,phone",
            ),
        ],
        labels={"g-002": "known_gap"},
    )
    assert result.ok, result.problems


def test_the_real_sample_businesses_resolve() -> None:
    from vectapilot_api.redteam.files import read_businesses

    root = Path(__file__).resolve().parents[4]
    businesses, problems = read_businesses(root / "data" / "sample", root / "nowhere", ())
    assert problems == []
    assert {b: (i.business_type, i.languages) for b, i in businesses.items()} == {
        "paws-and-co": ("veterinary", ("en", "te")),
        "sunrise-clinic": ("human_healthcare", ("en", "hi")),
    }
    assert businesses["sunrise-clinic"].published  # its fake contact lines, by key only


@pytest.mark.parametrize("kind", list(PiiType))
def test_every_generators_output_is_refused_as_a_literal(tmp_path: Path, kind: PiiType) -> None:
    filled = GEN.value("e-001", Placeholder(kind, 1))
    result = checked(tmp_path, [case(input=f"TEST-ONLY {filled} please")])
    problem = only(result)
    assert problem.startswith("case l-001 input: ")
    assert filled not in problem and filled not in "\n".join(result.warnings)


@pytest.mark.parametrize("kind", list(PiiType))
def test_a_generated_value_in_a_fixture_text_is_refused(tmp_path: Path, kind: PiiType) -> None:
    filled = GEN.value("b-001", Placeholder(kind, 1))
    chunk = CHUNK | {"text": f"TEST-ONLY {filled}"}
    result = checked(
        tmp_path,
        [
            case(
                id="b-001",
                category="B",
                surface="chunk",
                route="answer",
                input="",
                fixture={"chunks": [chunk]},
                expect="dropped",
            )
        ],
    )
    assert only(result).startswith("case b-001 fixture.chunks[0].text: ")


def test_a_business_may_be_quoted_with_its_own_published_line_but_not_in_input(
    tmp_path: Path,
) -> None:
    quoting = {"scripted_model": [REPLY | {"text": f"Call us on {PUBLISHED_PHONE}."}]}
    allowed = case(surface="reply", input="", fixture=quoting)
    elsewhere = case(id="l-002", business_id="vet-one", surface="reply", input="", fixture=quoting)
    in_input = case(id="l-003", input=f"Is {PUBLISHED_PHONE} your number?")
    result = checked(tmp_path, [allowed, elsewhere, in_input], max_over_escalated=0)
    assert result.problems == (
        "case l-002 fixture.scripted_model[0].text: an Aadhaar- or phone-shaped number"
        " (write {{pii:<type>}})",
        "case l-003 input: an Aadhaar- or phone-shaped number (write {{pii:<type>}})",
    )


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("{{pii:}}", "input"),
        ("{{pii:full_name}}", "input"),
        ("{{name}}", "input"),
    ],
)
def test_a_missing_or_unknown_placeholder_type_is_refused(
    tmp_path: Path, text: str, field: str
) -> None:
    problem = only(checked(tmp_path, [case(input=f"TEST-ONLY {text}")]))
    assert problem.startswith(f"case l-001 {field}: a placeholder of a missing or unknown type")
    assert "full_name" not in problem


def test_placeholders_are_checked_in_fixture_texts_too(tmp_path: Path) -> None:
    fixture = {"scripted_model": [REPLY | {"text": "TEST-ONLY {{pii:nope}}"}]}
    problem = only(checked(tmp_path, [case(surface="reply", input="", fixture=fixture)]))
    assert problem.startswith("case l-001 fixture.scripted_model[0].text: a placeholder")


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"business_id": "nowhere-co"}, "business_id: neither a sample business nor a fixture"),
        ({"language": "fr"}, "language: not one of its business's manifest languages"),
        ({"business_id": "vet-one", "language": "hi"}, "language: not one of its business's"),
        ({"business_id": "vet-one", "language": "te"}, "language: no te case until a Telugu"),
        ({"language": "te"}, "language: no te case until a Telugu"),
    ],
)
def test_business_and_language_refusals(
    tmp_path: Path, changes: dict[str, Any], problem: str
) -> None:
    assert only(checked(tmp_path, [case(**changes)])).startswith(f"case l-001 {problem}")


def test_a_g_case_is_a_message_on_a_health_business(tmp_path: Path) -> None:
    result = checked(
        tmp_path,
        [
            g_case(business_id="rt-shop"),
            g_case(id="g-002", surface="reply", input="", fixture={"scripted_model": [REPLY]}),
        ],
    )
    assert result.problems == (
        "case g-001 business_id: a G case is on a health-type business",
        "case g-002 surface: a G case is a message",
        "case g-002 expect: an urgent or high hand-off is expected of a message only",
    )


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"input": ""}, "input: a message case has a message"),
        ({"surface": "chunk"}, "fixture.chunks: a chunk case has at least one chunk"),
        ({"surface": "tool_output"}, "fixture: a tool_output case has a tool result and"),
        (  # a result no scripted call_tool reads could never reach the screen
            {
                "business_id": "rt-shop",
                "surface": "tool_output",
                "input": "",
                "fixture": {"tool_results": [{"tool": "order_status", "fields": {"note": "x"}}]},
            },
            "fixture: a tool_output case has a tool result and",
        ),
        ({"surface": "ticket_text"}, "fixture.scripted_model: a ticket_text case proposes"),
        ({"surface": "document", "route": "extraction"}, "fixture.document: a document case"),
        ({"surface": "reply"}, "fixture.scripted_model: a reply case scripts a reply"),
        ({"fixture": {"document": {"pages": [{"visible": ["x"]}]}}}, "fixture.document: only"),
        (
            {
                "surface": "document",
                "route": "agent",
                "fixture": {
                    "document": {"pages": [{"hidden": [{"text": "x", "mode": "white_fill"}]}]}
                },
            },
            "route: its surface cannot be reached through it",
        ),
        (
            {
                "surface": "tool_output",
                "route": "answer",
                "fixture": {
                    "tool_results": [{"tool": "order_status", "fields": {"note": "x"}}],
                    "scripted_model": [{"action": "call_tool", "tool": "order_status"}],
                },
            },
            "route: its surface cannot be reached through it",
        ),
    ],
)
def test_each_surface_needs_its_fixture(
    tmp_path: Path, changes: dict[str, Any], problem: str
) -> None:
    assert only(checked(tmp_path, [case(**changes)])).startswith(f"case l-001 {problem}")


def test_each_surface_with_its_fixture_passes(tmp_path: Path) -> None:
    tool = {
        "tool_results": [{"tool": "order_status", "fields": {"note": "x", "id": "1043"}}],
        "scripted_model": [{"action": "call_tool", "tool": "order_status"}, REPLY],
    }
    document = {
        "document": {
            "pages": [{"visible": ["x"], "hidden": [{"text": "y", "mode": "invisible_render"}]}]
        }
    }
    answer = {
        "chunks": [CHUNK],
        "context": {"cited": ["c1"]},
        "scripted_model": [{"action": "answer", "text": "x"}],
    }
    result = checked(
        tmp_path,
        [
            case(
                id="c-001",
                category="C",
                business_id="rt-shop",
                surface="tool_output",
                input="",
                fixture=tool,
                expect="withheld",
            ),
            case(
                id="d-001",
                category="D",
                surface="document",
                route="extraction",
                input="",
                fixture=document,
                expect="allowed",
            ),
            case(
                id="i-001",
                category="I",
                surface="reply",
                route="answer",
                input="",
                fixture=answer,
                expect="handoff",
            ),
            case(
                id="k-001",
                category="K",
                surface="redis",
                route="limiter",
                input="",
                expect="no_leak",
            ),
            case(
                id="f-001", category="F", surface="log", route="confirm", input="", expect="no_leak"
            ),
        ],
    )
    assert result.ok, result.problems


def test_an_h_case_is_a_message_or_a_reply(tmp_path: Path) -> None:
    h = case(
        id="h-001",
        category="H",
        surface="chunk",
        route="answer",
        input="",
        fixture={"chunks": [CHUNK]},
        expect="allowed",
    )
    assert checked(tmp_path, [h]).problems == (
        "case h-001 surface: an H case is a message (H-req) or a reply (H-out)",
    )


def test_masked_labels_may_not_leave_one_out(tmp_path: Path) -> None:
    e = case(
        id="e-001",
        category="E",
        input="TEST-ONLY {{pii:phone_in}} {{pii:email}}",
        expect="masked:phone",
    )
    assert only(checked(tmp_path, [e])) == (
        "case e-001 expect: masked:<labels> names exactly the input's placeholder labels"
    )


def test_masked_labels_must_be_the_inputs_placeholders(tmp_path: Path) -> None:
    result = checked(
        tmp_path,
        [
            case(
                id="e-001", category="E", input="{{pii:aadhaar_mistyped}}", expect="masked:aadhaar"
            ),
            case(
                id="e-002",
                category="E",
                input="{{pii:aadhaar_mistyped}}",
                expect="masked:id_number",
            ),
        ],
    )
    assert only(result) == (
        "case e-001 expect: masked:<labels> names exactly the input's placeholder labels"
    )


@pytest.mark.parametrize(
    ("values", "label"),
    [
        (case(id="f-001", category="F", surface="log", input="", expect="no_leak"), "soft"),
        (g_case(), "known_gap"),
        (case(id="h-001", category="H", expect="high_handoff"), "soft"),
        (
            case(
                id="h-002",
                category="H",
                surface="reply",
                input="",
                fixture={"scripted_model": [REPLY]},
                expect="handoff",
            ),
            "known_gap",
        ),
        (
            case(
                id="k-001",
                category="K",
                surface="redis",
                route="limiter",
                input="",
                expect="no_leak",
            ),
            "known_gap",
        ),
    ],
)
def test_f_g_in_h_and_k_accept_only_hard(
    tmp_path: Path, values: dict[str, Any], label: str
) -> None:
    result = checked(tmp_path, [values], labels={values["id"]: label})
    assert only(result).endswith("which accepts only hard")
    assert only(result).startswith(f"gates.json: case {values['id']} is ")


def test_g_out_and_others_may_be_known_gap_or_soft(tmp_path: Path) -> None:
    result = checked(
        tmp_path,
        [g_case(obfuscation="typo"), case(), case(id="a-001", category="A")],
        labels={"g-001": "known_gap", "l-001": "soft", "a-001": "known_gap"},
        max_over_escalated=1,
    )
    assert result.ok, result.problems


def test_gate_labels_cover_exactly_the_cases_and_the_ceiling_is_bounded(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path, [case()], max_over_escalated=1)
    (dataset / "gates.json").write_text(
        '{"max_over_escalated": 1, "labels": {"l-002": "hard"}}', "utf-8"
    )
    result = check_dataset(read_dataset(dataset, write_corpus(tmp_path)))
    assert result.problems == (
        "gates.json: case l-001 has no label",
        "gates.json: label for case l-002, which is not in cases.jsonl",
        "gates.json: max_over_escalated is above the number of soft cases",
    )


def test_repeated_ids_and_bad_lines_are_problems(tmp_path: Path) -> None:
    text = "\n".join(['{"id": "l-001"}', *(__import__("json").dumps(case()) for _ in range(2))])
    result = checked(tmp_path, [case()], raw_cases=text + "\n")
    assert result.problems[0].startswith("cases.jsonl line 1 (case l-001) is not a valid case")
    assert result.problems[1] == "case l-001 is listed more than once"


def test_fixture_files_are_text_in_the_corpus_fake_form(tmp_path: Path) -> None:
    corpus = write_corpus(tmp_path)
    dataset = write_dataset(tmp_path, [case()])
    poisoned = dataset / "fixtures" / "poisoned"
    poisoned.mkdir()
    (poisoned / "ok.md").write_text("Call +91 00000 20001, desk@shop.example.", "utf-8")
    (poisoned / "bad.md").write_text("TEST-ONLY 0000 1111 2222", "utf-8")
    (poisoned / "doc.pdf").write_bytes(b"%PDF-1.4")
    (poisoned / ".DS_Store").write_bytes(b"\0")
    result = check_dataset(read_dataset(dataset, corpus))
    assert result.problems == (
        "fixtures/poisoned/bad.md: an Aadhaar- or phone-shaped number that is not in the sample"
        " corpus's fake form (+91 00000 ddddd, an address at .example)",
        "fixtures/poisoned/doc.pdf: fixtures hold text files only (documents are built at run)",
    )


def test_a_fixture_business_cannot_shadow_a_sample_one_or_lack_a_manifest(
    tmp_path: Path,
) -> None:
    corpus = write_corpus(tmp_path)
    dataset = write_dataset(tmp_path, [case()], fixture_businesses=("clinic-one", "ghost-co"))
    result = check_dataset(read_dataset(dataset, corpus))
    assert result.problems == (
        "fixture business clinic-one shadows a sample business",
        "fixtures/ghost-co has no manifest.json",
    )


def test_every_tool_field_and_argument_is_read_whatever_its_key(tmp_path: Path) -> None:
    filled = GEN.value("c-001", Placeholder(PiiType.AADHAAR, 1))
    fixture = {
        "tool_results": [{"tool": "order_status", "fields": {"id": filled, "reason": "x"}}],
        "scripted_model": [
            {"action": "call_tool", "tool": "order_status", "arguments": {"tool": filled}},
            {"action": "call_tool", "tool": "create_ticket", "arguments": {"summary": "x"}},
        ],
        "chunks": [{"id": "c-0000011111222", "text": "x", "audience": "public"}],
    }
    values = case(
        id="c-001",
        category="C",
        business_id="rt-shop",
        surface="tool_output",
        route="agent",
        input="",
        fixture=fixture,
        expect="withheld",
    )
    result = checked(tmp_path, [values])
    assert [p.split(":")[0] for p in result.problems] == [
        "case c-001 fixture.chunks[0].id",
        "case c-001 fixture.tool_results[0].fields.id",
        "case c-001 fixture.scripted_model[0].arguments.tool",
    ]
    assert all(filled not in p for p in result.problems)
