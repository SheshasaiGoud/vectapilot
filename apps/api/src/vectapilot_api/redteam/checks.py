"""One stage-aware check of a `redteam` version (M7 PR 2), and its freeze.

`check` (and CI) refuses, each as a problem naming the case id and field, never a value:
- a line that is not a valid case: an unknown category, surface, route, expectation or
  obfuscation class, or a wrong fixture shape (`cases.Case`'s closed vocabularies);
- a repeated case id;
- a business that is neither a sample business (`<corpus>/<id>/manifest.json`) nor a fixture
  business (`fixtures/<id>/manifest.json`), or a fixture business that shadows a sample one;
- a language its business's manifest does not list, and any `te` case (D6: none until a Telugu
  reviewer exists);
- a G case on a business that is not a health type, or outside the message surface; an H case
  outside the message (H-req) and reply (H-out) surfaces;
- a route its surface cannot be reached through (`cases.SURFACE_ROUTES`) [proposal], and a
  fixture missing what its surface needs (`fixture_problems`) [proposal];
- a placeholder of a missing or unknown type, or any other `{{`/`}}`;
- a PII-shaped literal (`pii.literal_findings`): anywhere in `input`; in a fixture text unless it
  is a fake contact line its own business publishes (a reply quoting the business's number from a
  cited chunk, category L) [proposal]; in a file under `fixtures/` unless it is in the sample
  corpus's fake form;
- `masked:<labels>` whose labels are not exactly those of the input's placeholders [proposal];
- an urgent or high hand-off expected on any surface but a message (the red-flag screen reads
  messages, G1) [proposal];
- `gates.json`: a case without a label or a label without a case; any label but `hard` in F,
  G-in, H and K; a ceiling above the number of soft cases [proposal];
- once frozen (`freeze.py`): anything changed since the freeze, a loosened gate, a changed PII
  generator, counts other than the pinned ones.
"""

from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from vectapilot_api.evalset.checks import CheckResult, FreezeRefused
from vectapilot_api.intents.policy import HEALTH_TYPES
from vectapilot_api.redteam.cases import (
    CASES_FILE,
    FREEZE_FILE,
    GATES_FILE,
    HARD_ONLY,
    SURFACE_ROUTES,
    Case,
    Category,
    Counts,
    Gates,
    RedteamManifest,
    Surface,
    masked_labels,
)
from vectapilot_api.redteam.freeze import (
    RULES_NOTE,
    RedteamFreeze,
    file_sha256,
    loosening_problems,
    pii_pins,
    tightenings,
)
from vectapilot_api.redteam.pii import (
    GENERATOR_VERSION,
    LABEL_OF,
    PiiGenerator,
    literal_findings,
    placeholders,
)
from vectapilot_api.tools.config import ToolName

TELUGU: Final = "te"
TEXT_SUFFIXES: Final = frozenset({".md", ".html", ".csv", ".txt", ".json", ".jsonl"})
"""What `fixtures/` may hold: text only. A poisoned document is built at run time, never
committed as a PDF (scoring rules, `fixture.document`)."""
_CLOSED_FIELDS: Final = frozenset(
    {
        "category",
        "business_id",
        "surface",
        "route",
        "language",
        "obfuscation",
        "expect",
        "author",
        "reviewed_by",
        "created",
        "action",
        "tool",
        "reason",
        "mode",
        "audience",
        "outage",
    }
)
"""Model fields whose values are closed codes, a known business or a date: not text. Ids are
read (a chunk id is a free slug), and so is every value of a tool result's `fields` and a call's
`arguments`, whatever its key is called (`text_fields`)."""
_OPEN_MAPS: Final = frozenset({"fields", "arguments"})


@dataclass(frozen=True)
class BusinessInfo:
    """What a case's checks need of its business (from its corpus manifest)."""

    business_id: str
    business_type: str
    languages: tuple[str, ...]
    published: frozenset[str]
    """Keys of the fake contact literals its public, approved documents print (`pii`)."""
    fixture: bool
    """A test-only business under `fixtures/` (`rt-shop`)."""


@dataclass(frozen=True)
class RedteamDataset:
    """A version as read from disk (`files.read_dataset`)."""

    manifest: RedteamManifest
    cases: tuple[Case, ...]
    parse_problems: tuple[str, ...]
    gates: Gates
    cases_bytes: bytes
    gates_bytes: bytes
    businesses: Mapping[str, BusinessInfo]
    business_problems: tuple[str, ...]
    """Problems reading a business (a fixture business shadowing a sample one, no manifest)."""
    fixture_files: Mapping[str, bytes]
    """Every file under `fixtures/`, by its path relative to the version folder."""
    freeze: RedteamFreeze | None


# ── Per case ─────────────────────────────────────────────────────────────────────────────────


def text_fields(case: Case) -> Iterator[tuple[str, str]]:
    """Every free-text string of a case with its path (`input`, `fixture.chunks[0].text`, ...)."""

    def walk(value: Any, path: str, *, open_map: bool = False) -> Iterator[tuple[str, str]]:
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, dict):
            for key, item in value.items():
                if open_map or key not in _CLOSED_FIELDS:
                    where = f"{path}.{key}" if path else key
                    yield from walk(item, where, open_map=not open_map and key in _OPEN_MAPS)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                yield from walk(item, f"{path}[{i}]")

    yield from walk(case.model_dump(mode="json"), "")


def _steps(case: Case) -> list[str]:
    return [step.action for step in case.fixture.scripted_model]


def fixture_problems(case: Case) -> list[str]:
    """What the case's surface needs from its fixture [proposal], as reasons."""
    fixture, steps = case.fixture, _steps(case)
    problems: list[str] = []
    calls = [s.tool for s in fixture.scripted_model if s.action == "call_tool"]
    match case.surface:
        case Surface.MESSAGE:
            if not case.input.strip():
                problems.append("input: a message case has a message")
        case Surface.CHUNK:
            if not fixture.chunks:
                problems.append("fixture.chunks: a chunk case has at least one chunk")
        case Surface.TOOL_OUTPUT:
            if not fixture.tool_results or not calls:
                problems.append(
                    "fixture: a tool_output case has a tool result and a scripted tool call"
                )
        case Surface.TICKET_TEXT:
            if ToolName.CREATE_TICKET not in calls:
                problems.append("fixture.scripted_model: a ticket_text case proposes create_ticket")
        case Surface.DOCUMENT:
            if fixture.document is None:
                problems.append("fixture.document: a document case has a document")
        case Surface.REPLY:
            if "reply" not in steps and "answer" not in steps:
                problems.append("fixture.scripted_model: a reply case scripts a reply or answer")
        case Surface.REDIS | Surface.LOG:
            pass
    if fixture.document is not None and case.surface is not Surface.DOCUMENT:
        problems.append("fixture.document: only a document case has one")
    return problems


def _business_problems(case: Case, business: BusinessInfo) -> list[str]:
    problems: list[str] = []
    if case.language == TELUGU:
        problems.append("language: no te case until a Telugu reviewer exists (D6)")
    elif case.language not in business.languages:
        problems.append("language: not one of its business's manifest languages")
    if case.category is Category.G and business.business_type not in HEALTH_TYPES:
        problems.append("business_id: a G case is on a health-type business")
    return problems


def _shape_problems(case: Case) -> list[str]:
    problems: list[str] = []
    if case.category is Category.G and case.surface is not Surface.MESSAGE:
        problems.append("surface: a G case is a message")
    if case.category is Category.H and case.surface not in (Surface.MESSAGE, Surface.REPLY):
        problems.append("surface: an H case is a message (H-req) or a reply (H-out)")
    if case.route not in SURFACE_ROUTES[case.surface]:
        problems.append("route: its surface cannot be reached through it")
    if case.expect in ("urgent_handoff", "high_handoff") and case.surface is not Surface.MESSAGE:
        problems.append("expect: an urgent or high hand-off is expected of a message only")
    return problems


def _text_problems(case: Case, business: BusinessInfo | None) -> list[str]:
    problems: list[str] = []
    for path, text in text_fields(case):
        if placeholders(text)[1]:
            problems.append(f"{path}: a placeholder of a missing or unknown type, or a stray {{{{")
        for finding in literal_findings(text):
            published = business is not None and finding.fake and finding.key in business.published
            if path == "input" or not published:
                problems.append(f"{path}: {finding.description} (write {{{{pii:<type>}}}})")
                break
    labels = masked_labels(case.expect)
    if labels is not None:
        wanted = {LABEL_OF[p.type] for p in placeholders(case.input)[0]}
        if set(labels) != wanted:
            problems.append("expect: masked:<labels> names exactly the input's placeholder labels")
    return problems


def case_problems(case: Case, businesses: Mapping[str, BusinessInfo]) -> list[str]:
    """Every problem of one case, each `case <id> <field>: <rule>`."""
    business = businesses.get(case.business_id)
    reasons = _shape_problems(case) + fixture_problems(case)
    if business is None:
        reasons.append("business_id: neither a sample business nor a fixture business")
    else:
        reasons += _business_problems(case, business)
    reasons += _text_problems(case, business)
    return [f"case {case.id} {reason}" for reason in reasons]


# ── Gates, fixtures, the freeze ──────────────────────────────────────────────────────────────


def gate_problems(cases: tuple[Case, ...], gates: Gates) -> list[str]:
    ids = {case.id for case in cases}
    problems = [f"{GATES_FILE}: case {i} has no label" for i in sorted(ids - set(gates.labels))]
    problems += [
        f"{GATES_FILE}: label for case {i}, which is not in {CASES_FILE}"
        for i in sorted(set(gates.labels) - ids)
    ]
    for case in cases:
        label = gates.labels.get(case.id)
        if label is not None and label != "hard" and case.subgroup in HARD_ONLY:
            problems.append(
                f"{GATES_FILE}: case {case.id} is {case.subgroup}, which accepts only hard"
            )
    soft = sum(1 for label in gates.labels.values() if label == "soft")
    if gates.max_over_escalated > soft:
        problems.append(f"{GATES_FILE}: max_over_escalated is above the number of soft cases")
    return problems


def fixture_file_problems(files: Mapping[str, bytes]) -> list[str]:
    problems: list[str] = []
    for path, data in sorted(files.items()):
        if not any(path.endswith(suffix) for suffix in TEXT_SUFFIXES):
            problems.append(f"{path}: fixtures hold text files only (documents are built at run)")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{path}: not UTF-8 text")
            continue
        found = next((f for f in literal_findings(text) if not f.fake), None)
        if found is not None:
            problems.append(
                f"{path}: {found.description} that is not in the sample corpus's fake form"
                " (+91 00000 ddddd, an address at .example)"
            )
    return problems


def counts_of(cases: tuple[Case, ...]) -> Counts:
    per = Counter(case.category for case in cases)
    return Counts(total=len(cases), categories={c: per.get(c, 0) for c in Category})


def freeze_problems(dataset: RedteamDataset, generator: PiiGenerator) -> list[str]:
    """Everything that differs from the freeze (frozen stage only)."""
    freeze, manifest = dataset.freeze, dataset.manifest
    if freeze is None:
        return [f"{manifest.version} is frozen but has no {FREEZE_FILE}"]
    bump = f"{manifest.version} is frozen: {RULES_NOTE}"
    problems: list[str] = []
    if (freeze.dataset, freeze.version) != (manifest.dataset, manifest.version):
        problems.append(f"{FREEZE_FILE} belongs to another dataset or version")
    if file_sha256(dataset.cases_bytes) != freeze.cases_sha256:
        problems.append(f"{CASES_FILE} changed after the freeze; {bump}")
    problems += loosening_problems(freeze.gates, dataset.gates)
    if pii_pins(generator, GENERATOR_VERSION) != freeze.pii:
        problems.append(f"the PII generator, its seed or its test vectors changed; {bump}")
    if manifest.counts != counts_of(dataset.cases):
        problems.append(f"the cases' counts differ from the manifest's pinned counts; {bump}")
    return problems


def generator_of(dataset: RedteamDataset) -> PiiGenerator:
    """The frozen seed's generator, or the default one while collecting."""
    return PiiGenerator() if dataset.freeze is None else PiiGenerator(dataset.freeze.pii.seed)


def check_dataset(dataset: RedteamDataset, *, against_freeze: bool = True) -> CheckResult:
    """Every problem of the version for its stage (module docstring). `against_freeze=False`
    leaves out the freeze comparison (`freeze --refreeze` only)."""
    problems = list(dataset.parse_problems) + list(dataset.business_problems)
    repeated = sorted(i for i, n in Counter(c.id for c in dataset.cases).items() if n > 1)
    problems += [f"case {i} is listed more than once" for i in repeated]
    for case in dataset.cases:
        problems += case_problems(case, dataset.businesses)
    problems += gate_problems(dataset.cases, dataset.gates)
    problems += fixture_file_problems(dataset.fixture_files)
    warnings: list[str] = []
    frozen = dataset.manifest.stage == "frozen"
    if frozen and against_freeze:
        problems += freeze_problems(dataset, generator_of(dataset))
        if dataset.freeze is not None:
            warnings += [
                f"tightened since the freeze: {t}"
                for t in tightenings(dataset.freeze.gates, dataset.gates)
            ]
    elif not frozen and dataset.freeze is not None:
        problems.append(f"{FREEZE_FILE} exists while the manifest's stage is collecting")
    return CheckResult(problems=tuple(problems), warnings=tuple(warnings))


@dataclass(frozen=True)
class FrozenRedteam:
    manifest: RedteamManifest
    freeze: RedteamFreeze
    refrozen_from: date | None
    """The earlier freeze's date when this is a re-freeze (reported), else None."""


def freeze_dataset(
    dataset: RedteamDataset,
    *,
    frozen_on: date,
    refreeze: bool = False,
    generator: PiiGenerator | None = None,
) -> FrozenRedteam:
    """The freeze of a checked version. Raises `FreezeRefused` with the reasons: no case, any
    problem, an already frozen version without `refreeze`, or `refreeze` of an unfrozen one."""
    manifest = dataset.manifest
    reasons: list[str] = []
    frozen = manifest.stage == "frozen"
    if frozen and not refreeze:
        reasons.append(
            f"{manifest.version} is already frozen: --refreeze re-pins it, allowed only until"
            " the PR that froze it merges (every re-freeze is reported)"
        )
    if refreeze and not frozen:
        reasons.append(f"{manifest.version} is not frozen: freeze it without --refreeze")
    if not dataset.cases:
        reasons.append(f"{CASES_FILE} has no case")
    reasons += check_dataset(dataset, against_freeze=False).problems
    if reasons:
        raise FreezeRefused(reasons)
    generator = generator or PiiGenerator()
    freeze = RedteamFreeze(
        dataset=manifest.dataset,
        version=manifest.version,
        frozen_on=frozen_on,
        cases_sha256=file_sha256(dataset.cases_bytes),
        gates_sha256=file_sha256(dataset.gates_bytes),
        gates=dataset.gates,
        pii=pii_pins(generator, GENERATOR_VERSION),
    )
    counts = counts_of(dataset.cases).model_dump(mode="json")
    new_manifest = RedteamManifest.model_validate(
        manifest.model_dump(mode="json") | {"stage": "frozen", "counts": counts}
    )
    previous = dataset.freeze.frozen_on if refreeze and dataset.freeze is not None else None
    return FrozenRedteam(manifest=new_manifest, freeze=freeze, refrozen_from=previous)
