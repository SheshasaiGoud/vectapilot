"""Test doubles for the extraction harness (M6 PR 4). Nothing here is H1 or H2.

- `FIXTURES`: the committed dry-run fixtures (`apps/api/tests/extracteval/fixtures/dry-run`).
- `dry_repo(tmp)`: a throwaway repository (`helpers.make_repo`: the committed manifest, collecting,
  with an empty gold.jsonl, the extraction configuration and the corpus manifests). A dry run reads
  only its manifest and configuration, plus the fixtures.
- `frozen_repo(tmp)`: `helpers.small_repo` with its four slots filled by marker-free TEST
  documents (`TEST-ONLY ` removed, so the live path, which refuses placeholders, can load them),
  then frozen: s-1 and p-1 on dev, s-2 and p-2 on test. In tmp_path only.
- `io_for`: an `Io` with HEAD as a dict (`FakeRepo`), a fixed clock, a fake terminal, the test
  settings (paid ones only when asked; nothing is ever sent: `build_provider` is a fake), canned
  H2 metrics for which the summary's identity holds (`consistent_metrics`) and the stand-in
  router.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from tests.conftest import make_settings
from tests.extracteval.fakes import STAND_IN_ROUTE, consistent_metrics
from tests.extracteval.helpers import (
    PAWS,
    REFERRAL,
    REFERRAL_FIELDS,
    SUNRISE,
    TODAY,
    VACCINATION,
    VACCINATION_FIELDS,
    add_document,
    gold,
    make_repo,
    small_repo,
)
from tests.intenteval.harness_helpers import PAID, FakeRepo, Now, Terminal
from tests.tools.helpers import REPO_ROOT
from vectapilot_api.config import Settings
from vectapilot_api.extracteval.checks import freeze_dataset
from vectapilot_api.extracteval.dryrun import DEFAULT_FIXTURES
from vectapilot_api.extracteval.files import read_dataset, write_frozen
from vectapilot_api.extracteval.harness import Io
from vectapilot_api.extracteval.metrics import ExtractMetrics
from vectapilot_api.extraction.service import Route
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply

FIXTURES: Final = REPO_ROOT / DEFAULT_FIXTURES


def no_provider(settings: Settings) -> Provider:
    raise AssertionError("a dry or refused run never builds a provider")


def settings(live: bool) -> Settings:
    """The test settings (never a `.env` file); paid ones only when `live`."""
    return make_settings(**(PAID if live else {}))


def dry_repo(tmp_path: Path) -> tuple[FakeRepo, Path]:
    dataset_dir = make_repo(tmp_path)
    return FakeRepo(root=dataset_dir.parents[3]), dataset_dir


def frozen_repo(tmp_path: Path) -> tuple[FakeRepo, Path]:
    dataset_dir = small_repo(tmp_path)
    referral = REFERRAL.replace("TEST-ONLY ", "")
    vaccination = VACCINATION.replace("TEST-ONLY ", "")
    for doc_id, business_id, split, source in (
        ("s-1", SUNRISE, "dev", referral),
        ("s-2", SUNRISE, "test", referral.replace("Policy no", "Policy number")),
        ("p-1", PAWS, "dev", vaccination),
        ("p-2", PAWS, "test", vaccination.replace("Species", "Animal")),
    ):
        add_document(dataset_dir, gold(doc_id, business_id, split=split), source)
    write_frozen(dataset_dir, freeze_dataset(read_dataset(dataset_dir), frozen_on=TODAY))
    return FakeRepo(root=dataset_dir.parents[3]), dataset_dir


def null_reply(fields: Mapping[str, object]) -> FakeReply:
    """A shape-valid answer proposing nothing for every field of `fields`."""
    body = {name: {"value": None, "quote": None, "unsure": False} for name in fields}
    return FakeReply(text=json.dumps(body))


def live_replies() -> list[FakeReply]:
    """One null answer per dev document of `frozen_repo`, in run order (s-1, then p-1)."""
    return [null_reply(REFERRAL_FIELDS), null_reply(VACCINATION_FIELDS)]


def io_for(
    repo: FakeRepo,
    *,
    live_settings: bool = False,
    typed: str = "",
    route: Route = STAND_IN_ROUTE,
    metrics: ExtractMetrics | None = None,
    provider: Callable[[Settings], Provider] = no_provider,
    environment: str | None = None,
) -> Io:
    def load(live: bool) -> Settings:
        base = settings(live_settings)
        return base if environment is None else base.model_copy(update={"environment": environment})

    return Io(
        git=repo.git,
        head_file=repo.head_file,
        head_paths=repo.head_paths,
        head_ref=lambda root: "branch m6/close-out at ccccccc",
        now=Now(),
        stdin=Terminal(typed),
        stdout_is_terminal=lambda: True,
        load_settings=load,
        build_provider=provider,
        metrics=consistent_metrics() if metrics is None else metrics,
        route=route,
    )


def scripted(replies: Sequence[FakeReply]) -> tuple[Callable[[Settings], Provider], FakeProvider]:
    """A `build_provider` handing out one `FakeProvider` playing `replies` (a paid stand-in)."""
    fake = FakeProvider(list(replies))
    return (lambda settings: fake), fake


def dry_args(dataset_dir: Path, *extra: str) -> list[str]:
    """`run --split dev` on the committed fixtures, then `extra`."""
    return [
        "--dataset",
        str(dataset_dir),
        "run",
        "--split",
        "dev",
        "--fixtures",
        str(FIXTURES),
        *extra,
    ]


def json_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
