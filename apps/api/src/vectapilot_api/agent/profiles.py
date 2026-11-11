"""Each business's agent profile, built once at startup (ADR 0016 decisions 1, 4, 7; M5 PR 4).

A profile holds what one business's turns need: its tool configuration, its executor, its
`AgentStep` schema, its route template (M4's `policy_for`) and whether W11's safety step applies.

**The business type has two sources, and W11 trusts the stricter** [PR 1 review, zara]. M4's route
policy reads `business_type` from the corpus manifest (`<VECTAPILOT_CORPUS_DIR>/<business_id>/
manifest.json`); the tools read it from `tools.json`. A typo in either must never switch W11 off
for a clinic, so:
- startup refuses a business whose two types differ, or whose manifest is missing or invalid
  (`build_profiles` raises `AgentConfigError`, naming the business and the rule, never a value);
- and `health` is true if either type is a health type (`HEALTH_TYPES`), so even a check that
  was skipped by a later change would fail towards the safety step.

The manifest is read for its `business_type` only, with the corpus's own validator
(`evalset.corpus.load_corpus_manifest`); no document is loaded.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from vectapilot_api.agent.steps import build_agent_step
from vectapilot_api.agent.types import AgentStep
from vectapilot_api.evalset.corpus import CORPUS_MANIFEST_FILE, CorpusError, load_corpus_manifest
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.intents.policy import (
    HEALTH_TYPES,
    BusinessType,
    RoutePolicy,
    UnknownBusinessType,
    policy_for,
)
from vectapilot_api.tools.config import ToolsConfig
from vectapilot_api.tools.executor import ToolExecutor, TransactionSource
from vectapilot_api.tools.files import BusinessTools

MAX_MANIFEST_BYTES = 256 * 1024
"""A bound, not a measurement: the committed manifests are a few kilobytes."""


class AgentConfigError(ValueError):
    """A business's agent configuration cannot be used. Names the business and the rule."""


@dataclass(frozen=True)
class AgentProfile:
    """One business's agent configuration (module docstring)."""

    config: ToolsConfig
    executor: ToolExecutor
    step_schema: type[AgentStep]
    policy: RoutePolicy
    health: bool
    """W11 applies: either source says the business is a health type."""

    @property
    def business_id(self) -> BusinessId:
        return self.config.business_id

    @property
    def tools_enabled(self) -> bool:
        return bool(self.config.enabled_tools)

    @property
    def guidance(self) -> tuple[str, ...]:
        """The business's emergency guidance lines, in order, never joined (W11)."""
        return self.config.emergency_guidance


def corpus_business_type(corpus_dir: Path, business_id: str) -> BusinessType:
    """The `business_type` of `business_id`'s corpus manifest, validated, as a known type."""
    where = f"{business_id}/{CORPUS_MANIFEST_FILE}"
    path = corpus_dir / business_id / CORPUS_MANIFEST_FILE
    try:
        data = path.read_bytes()
    except OSError:
        raise AgentConfigError(
            f"{where}: no corpus manifest in the corpus folder (VECTAPILOT_CORPUS_DIR)"
        ) from None
    if len(data) > MAX_MANIFEST_BYTES:
        raise AgentConfigError(f"{where} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        manifest = load_corpus_manifest(data.decode("utf-8"), where=where)
    except (CorpusError, UnicodeDecodeError) as error:
        detail = str(error) if isinstance(error, CorpusError) else f"{where} is not UTF-8"
        raise AgentConfigError(detail) from None
    if manifest.business_id != business_id:
        raise AgentConfigError(f"{where} names another business")
    try:
        policy_for(manifest.business_type)
    except UnknownBusinessType:
        raise AgentConfigError(f"{where}: its business_type is not a known type") from None
    return BusinessType(manifest.business_type)


def business_types(
    tools: Mapping[BusinessId, BusinessTools], corpus_dir: Path
) -> Mapping[BusinessId, BusinessType]:
    """Each configured business's type, checked against its corpus manifest (module docstring).
    Called by `create_app` before it serves anything; raises `AgentConfigError`."""
    if not corpus_dir.is_dir():
        raise AgentConfigError("the corpus folder (VECTAPILOT_CORPUS_DIR) is missing")
    found: dict[BusinessId, BusinessType] = {}
    for business_id, business_tools in tools.items():
        corpus_type = corpus_business_type(corpus_dir, business_id)
        if business_tools.config.business_type is not corpus_type:
            raise AgentConfigError(
                f"{business_id}: tools.json and the corpus manifest name different business"
                " types (W11 needs both to agree)"
            )
        found[business_id] = corpus_type
    return MappingProxyType(found)


def build_profile(
    database: TransactionSource, config: ToolsConfig, corpus_type: BusinessType
) -> AgentProfile:
    """One business's profile; `corpus_type` is its manifest's type (`business_types`)."""
    if config.business_type is not corpus_type:
        raise AgentConfigError(
            f"{config.business_id}: tools.json and the corpus manifest name different business"
            " types (W11 needs both to agree)"
        )
    executor = ToolExecutor(database, config)
    return AgentProfile(
        config=config,
        executor=executor,
        step_schema=build_agent_step(executor),
        policy=policy_for(config.business_type.value),
        health=config.business_type in HEALTH_TYPES or corpus_type in HEALTH_TYPES,
    )


def build_profiles(
    database: TransactionSource,
    tools: Mapping[BusinessId, BusinessTools],
    types: Mapping[BusinessId, BusinessType],
) -> Mapping[BusinessId, AgentProfile]:
    """Every configured business's profile, read-only (`types` from `business_types`)."""
    if set(types) != set(tools):
        raise AgentConfigError("every configured business needs its checked business type")
    profiles = {
        business_id: build_profile(database, business_tools.config, types[business_id])
        for business_id, business_tools in tools.items()
    }
    return MappingProxyType(profiles)
