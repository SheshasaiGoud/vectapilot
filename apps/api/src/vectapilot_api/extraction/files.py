"""Reading the extraction configuration folder (`VECTAPILOT_EXTRACTION_CONFIG_DIR`, ADR 0017
decision 2; M6 PR 1).

    <dir>/README.md                               optional, ignored
    <dir>/templates/<business_type>/<kind>.json   one template (`config.Template`)
    <dir>/businesses/<business_id>.json           one business (`config.BusinessFile`)

Fail closed and fail fast, as `tools.files`: the API reads the whole folder once at startup
(`create_app`) and any problem stops it, naming the file and the rule, never a value. Every
template is compiled into its output model even if no business enables it yet, so a broken
template never waits for its first business. Refused: a missing folder or subfolder, no template
or no business, an unknown business type folder, a file whose name differs from the `kind` or
`business_id` it holds, a business whose type differs from its corpus manifest's (the M5 check,
`agent.profiles.corpus_business_type`), a kind without a template for the business's type,
anything that loosens a template (E5), and stray files (a typo such as `referal_letter.jsn` must
not leave a kind silently unconfigured). Names starting with a dot (`.DS_Store`) are ignored.
Files are read as UTF-8 and at most `MAX_FILE_BYTES` each.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from vectapilot_api.agent.profiles import AgentConfigError, corpus_business_type
from vectapilot_api.db import InvalidBusinessId, parse_business_id
from vectapilot_api.extraction.config import (
    BusinessFile,
    CompiledTemplate,
    ExtractionConfigError,
    KindConfig,
    compile_output_model,
    parse_business_file,
    parse_template,
    resolve_kind,
)
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.intents.policy import BusinessType

README: Final = "README.md"
TEMPLATES: Final = "templates"
BUSINESSES: Final = "businesses"
JSON_SUFFIX: Final = ".json"
MAX_FILE_BYTES: Final = 64 * 1024
"""A bound, not a measurement: the committed files are a few kilobytes."""


@dataclass(frozen=True)
class LoadedBusiness:
    """One business file, validated, with its kinds resolved against their templates."""

    file: BusinessFile
    sha256: str
    kinds: Mapping[str, KindConfig]


@dataclass(frozen=True)
class ExtractionConfig:
    """The whole folder: every template, compiled, and every business, resolved."""

    templates: Mapping[tuple[BusinessType, str], CompiledTemplate]
    businesses: Mapping[BusinessId, LoadedBusiness]

    def kind_config(self, business_id: str, kind: str) -> KindConfig | None:
        """What `business_id`'s extraction of `kind` runs with, or None when the business has no
        file or does not enable the kind. Both are the same answer (E8: the route's same 404), and
        the template comes from the business's own type, never from the caller."""
        business = self.businesses.get(business_id)
        return None if business is None else business.kinds.get(kind)


def _read(path: Path, where: str) -> tuple[str, str]:
    """The file's text and its SHA-256."""
    try:
        data = path.read_bytes()
    except OSError:
        raise ExtractionConfigError(f"{where} cannot be read") from None
    if len(data) > MAX_FILE_BYTES:
        raise ExtractionConfigError(f"{where} is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ExtractionConfigError(f"{where} is not UTF-8") from None
    return text, hashlib.sha256(data).hexdigest()


def _entries(folder: Path) -> list[Path]:
    return sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))


def _require_folder(folder: Path, where: str) -> None:
    if not folder.is_dir():
        raise ExtractionConfigError(f"the extraction configuration folder has no {where} folder")


def _json_stem(entry: Path, where: str) -> str:
    if not entry.is_file() or entry.suffix != JSON_SUFFIX:
        raise ExtractionConfigError(f"unexpected entry {entry.name!r} in {where}")
    return entry.stem


def load_templates(folder: Path) -> Mapping[tuple[BusinessType, str], CompiledTemplate]:
    """Every template under `folder` (`<dir>/templates`), validated and compiled."""
    _require_folder(folder, TEMPLATES)
    found: dict[tuple[BusinessType, str], CompiledTemplate] = {}
    for type_folder in _entries(folder):
        try:
            business_type = BusinessType(type_folder.name)
        except ValueError:
            raise ExtractionConfigError(
                f"{TEMPLATES}: folder {type_folder.name!r} is not a known business type"
            ) from None
        if not type_folder.is_dir():
            raise ExtractionConfigError(f"{TEMPLATES}: {type_folder.name!r} is not a folder")
        for entry in _entries(type_folder):
            where = f"{TEMPLATES}/{business_type.value}/{entry.name}"
            kind = _json_stem(entry, f"{TEMPLATES}/{business_type.value}")
            text, sha256 = _read(entry, where)
            template = parse_template(text, where=where)
            if template.business_type is not business_type or template.kind != kind:
                raise ExtractionConfigError(
                    f"{where} names a different business_type or kind than its path"
                )
            found[(business_type, kind)] = CompiledTemplate(
                template, compile_output_model(template), sha256
            )
    if not found:
        raise ExtractionConfigError("the extraction configuration folder holds no template")
    return MappingProxyType(found)


def load_business(
    entry: Path,
    templates: Mapping[tuple[BusinessType, str], CompiledTemplate],
    corpus_dir: Path,
) -> LoadedBusiness:
    """One business file: validated, its type checked against its corpus manifest, each enabled
    kind resolved against the template of the business's own type (E5)."""
    stem = _json_stem(entry, BUSINESSES)
    try:
        business_id = parse_business_id(stem)
    except InvalidBusinessId:
        raise ExtractionConfigError(
            f"{BUSINESSES}: file {entry.name!r} is not named after a business id"
        ) from None
    where = f"{BUSINESSES}/{entry.name}"
    text, sha256 = _read(entry, where)
    business = parse_business_file(text, where=where)
    if business.business_id != business_id:
        raise ExtractionConfigError(f"{where} names a different business_id")
    problem = None
    try:
        corpus_type = corpus_business_type(corpus_dir, business_id)
    except AgentConfigError as error:
        problem = str(error)  # names the manifest and the rule, never a value
    if problem is not None:
        raise ExtractionConfigError(f"{where}: {problem}")
    if business.business_type is not corpus_type:
        raise ExtractionConfigError(
            f"{where} and the corpus manifest name different business types (the template is"
            " selected by the type, so both must agree)"
        )
    kinds = {
        settings.kind: resolve_kind(
            business,
            index,
            templates.get((business.business_type, settings.kind)),
            where=where,
        )
        for index, settings in enumerate(business.kinds)
    }
    return LoadedBusiness(business, sha256, MappingProxyType(kinds))


def load_extraction_dir(directory: Path, corpus_dir: Path) -> ExtractionConfig:
    """The whole folder, read and validated (module docstring); read-only, sorted by name."""
    if not directory.is_dir():
        raise ExtractionConfigError(
            "the extraction configuration folder (VECTAPILOT_EXTRACTION_CONFIG_DIR) is missing"
        )
    if not corpus_dir.is_dir():
        raise ExtractionConfigError("the corpus folder (VECTAPILOT_CORPUS_DIR) is missing")
    for entry in _entries(directory):
        if entry.name == README and entry.is_file():
            continue
        if entry.name not in (TEMPLATES, BUSINESSES) or not entry.is_dir():
            raise ExtractionConfigError(
                f"unexpected entry {entry.name!r} in the extraction configuration folder"
            )
    templates = load_templates(directory / TEMPLATES)
    _require_folder(directory / BUSINESSES, BUSINESSES)
    businesses: dict[BusinessId, LoadedBusiness] = {}
    for entry in _entries(directory / BUSINESSES):
        loaded = load_business(entry, templates, corpus_dir)
        businesses[loaded.file.business_id] = loaded
    if not businesses:
        raise ExtractionConfigError("the extraction configuration folder holds no business")
    return ExtractionConfig(templates, MappingProxyType(businesses))
