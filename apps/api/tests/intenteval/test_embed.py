"""Embedding the rows (M4 PR 4): `embed.embed_rows`' stops, and the `embed` command's folder in
every outcome (complete, stopped by the cap, failed) and what reads it (`read_embed`, train,
calibrate). Fakes only: the fake embedder, made-up TEST-ONLY rows, HEAD as a dict. Nothing is
sent anywhere; "live" folders here are fakes in tmp_path.
"""

import base64
import json
import struct
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from tests.intenteval.harness_helpers import (
    FakeRepo,
    Now,
    Terminal,
    dry_embed,
    folders,
    frozen_copy,
    live_embed,
    make_io,
    reformat_json,
    rewrite_run,
    settings,
    texts,
    written_text,
)
from tests.intenteval.helpers import complete_eval, complete_train
from vectapilot_api.config import Settings
from vectapilot_api.embeddings.base import Embedder, Embeddings, EmbeddingSpec, InputType
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.evalrun.budget import CapReached, CostCap
from vectapilot_api.intenteval.cache import CACHE_FILE, parse_cache, render_cache
from vectapilot_api.intenteval.cli import harness_main
from vectapilot_api.intenteval.embed import embed_rows
from vectapilot_api.intenteval.model import MODEL_FILE, sha256_of
from vectapilot_api.intenteval.runinfo import INTENT_REPORTS, read_run
from vectapilot_api.intenteval.spend import m4_spend


@pytest.fixture
def world(tmp_path: Path) -> tuple[FakeRepo, Path, Now]:
    root = tmp_path.resolve()
    return FakeRepo(root), frozen_copy(root), Now()


def fake_spec() -> EmbeddingSpec:
    s = settings(False)
    return load_embedding_catalog().resolve(
        s.embeddings_provider, s.embeddings_model, s.embeddings_dimension
    )


class Scripted:
    """The fake embedder, which raises `fail` on call number `on` (1-based)."""

    def __init__(self, spec: EmbeddingSpec, *, on: int, fail: BaseException) -> None:
        self._fake = FakeEmbedder(spec)
        self.on, self.fail, self.calls = on, fail, 0

    @property
    def model(self) -> str:
        return self._fake.model

    @property
    def dimension(self) -> int:
        return self._fake.dimension

    async def embed(
        self, texts: Sequence[str], *, input_type: InputType, business_id: str
    ) -> Embeddings:
        self.calls += 1
        if self.calls == self.on:
            raise self.fail
        return await self._fake.embed(texts, input_type=input_type, business_id=business_id)

    async def aclose(self) -> None:
        await self._fake.aclose()


def runner(embedder: Embedder, spec: EmbeddingSpec) -> EmbeddingRunner:
    return EmbeddingRunner(embedder, spec, settings(False).llm_retry_policy(), deadline_s=100.0)


def ticks() -> float:
    return 0.0


# ── embed_rows ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_cap_reached_stops_with_the_rows_embedded_so_far() -> None:
    spec = fake_spec()
    train, evals = complete_train(), complete_eval()
    first = [r for r in train if r.business_id == train[0].business_id]
    embedder = Scripted(spec, on=2, fail=CapReached("TEST-ONLY: refused by the cap"))

    cap = CostCap(cap=None)

    embedded = await embed_rows(runner(embedder, spec), cap, train, evals, clock=ticks)

    assert embedded.stop == "TEST-ONLY: refused by the cap"
    assert [e.id for e in embedded.entries] == [r.id for r in first]  # the first business only


@pytest.mark.anyio
async def test_a_cap_stop_rule_ends_the_embedding_after_the_current_batch() -> None:
    spec = fake_spec()
    train, evals = complete_train(), complete_eval()
    first = [r for r in train if r.business_id == train[0].business_id]
    assert len(first) < len(train)  # two businesses
    cap = CostCap(cap=None)
    cap.stop_after("TEST-ONLY stop rule")

    embedded = await embed_rows(runner(FakeEmbedder(spec), spec), cap, train, evals, clock=ticks)

    assert embedded.stop == "TEST-ONLY stop rule"
    assert [e.id for e in embedded.entries] == [r.id for r in first]  # before any eval row


@pytest.mark.anyio
async def test_any_other_error_propagates() -> None:
    spec = fake_spec()
    embedder = Scripted(spec, on=1, fail=RuntimeError("TEST-ONLY"))

    with pytest.raises(RuntimeError):
        await embed_rows(
            runner(embedder, spec), CostCap(cap=None), complete_train(), (), clock=ticks
        )


# ── The embed command ────────────────────────────────────────────────────────────────────────


def test_a_failed_live_embed_keeps_run_json_with_its_counted_spend(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    secret = texts(dataset)[0]
    built: list[Scripted] = []

    def build(s: Settings, spec: EmbeddingSpec) -> Embedder:
        built.append(Scripted(spec, on=2, fail=RuntimeError(secret)))
        return built[-1]

    io = replace(make_io(repo, stdin=Terminal("0.50\n"), now=now), build_embedder=build)
    argv = ["--dataset", str(dataset), "embed", "--live", "--max-usd", "0.50"]

    assert harness_main(argv, io) == 1

    [folder] = folders(repo.root, True, "-live-embed")
    info = read_run(folder)
    assert (info.status, info.stop, info.rows) == ("failed", "RuntimeError (message not shown)", 0)
    assert info.embedding_cache_sha256 is None and not (folder / CACHE_FILE).exists()
    # The failed request's reservation is kept (it may have been billed).
    assert Decimal(0) < info.cost.total_usd <= Decimal("0.50") == info.cost.cap_usd
    out = capsys.readouterr()
    assert secret not in written_text(folder) + out.out + out.err
    # Committed, it counts at its run.json figure, never at the $1.00 ceiling.
    repo.commit(folder)
    [counted] = m4_spend(repo.root, repo.head_paths, repo.head_file).counted
    assert (counted.total_usd, counted.basis) == (info.cost.total_usd, "run.json")
    # And nothing trains on it.
    assert harness_main(["--dataset", str(dataset), "train", "--embed", str(folder)], io) == 1
    assert "is failed: embed again" in capsys.readouterr().err


def test_a_live_embed_the_cap_stops_is_partial_and_never_trained_on(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    io = make_io(repo, stdin=Terminal("0.000001\n"), now=now)
    argv = ["--dataset", str(dataset), "embed", "--live", "--max-usd", "0.000001"]

    assert harness_main(argv, io) == 3  # partial (the CLI's exit codes)

    [folder] = folders(repo.root, True, "-live-embed")
    info = read_run(folder)
    assert (info.status, info.cost.cap_usd) == ("partial", Decimal("0.000001"))
    assert info.stop is not None and "would cross the cap" in info.stop
    repo.commit(folder)
    train = ["--dataset", str(dataset), "train", "--embed", str(folder)]
    assert harness_main(train, make_io(repo, now=now)) == 1
    assert "is partial: embed again" in capsys.readouterr().err


def test_a_live_embed_needs_a_frozen_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path.resolve()
    repo, dataset, now = FakeRepo(root), frozen_copy(root, frozen=False), Now()

    assert live_embed(repo, dataset, now) == 1

    assert "a live embed needs a frozen dataset" in capsys.readouterr().err
    assert not (root / INTENT_REPORTS).exists()


def other_vector(cache: Path) -> None:
    """Replace the first entry's vector with another valid one (same ids, hashes, dimension)."""
    entries = list(parse_cache(cache.read_text("utf-8")))
    first = entries[0]
    values = struct.pack(f"<{first.dimension}f", *([0.5] * first.dimension))
    entries[0] = first.model_copy(update={"vector": base64.b64encode(values).decode("ascii")})
    cache.write_text(render_cache(entries), "utf-8")


def test_a_cache_edited_after_its_run_json_is_refused(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    embed = dry_embed(repo, dataset, now)
    other_vector(embed / CACHE_FILE)

    train = ["--dataset", str(dataset), "train", "--embed", str(embed)]
    assert harness_main(train, make_io(repo, now=now)) == 1
    assert f"{CACHE_FILE} is not the cache its run.json names" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"status": "partial", "stop": "TEST-ONLY"}, "is partial: embed again"),
        ({"freeze_sha256": "0" * 64}, "embedded another freeze of the dataset"),
    ],
    ids=["partial", "another-freeze"],
)
def test_an_embed_folder_that_is_not_this_freezes_complete_cache_is_refused(
    world: tuple[FakeRepo, Path, Now],
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, str],
    refusal: str,
) -> None:
    repo, dataset, now = world
    embed = dry_embed(repo, dataset, now)
    rewrite_run(embed, **changes)  # the cache itself is complete and unchanged

    train = ["--dataset", str(dataset), "train", "--embed", str(embed)]
    assert harness_main(train, make_io(repo, now=now)) == 1
    assert refusal in capsys.readouterr().err


def test_a_model_used_with_another_embed_folder_is_refused(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    io = make_io(repo, now=now)
    first = dry_embed(repo, dataset, now)
    second = dry_embed(repo, dataset, now)
    other_vector(second / CACHE_FILE)  # other vectors, its run.json consistent with them
    rewrite_run(second, embedding_cache_sha256=sha256_of(second / CACHE_FILE))
    assert harness_main(["--dataset", str(dataset), "train", "--embed", str(first)], io) == 0
    [model] = folders(repo.root, False, "-fake-model")
    data = json.loads((model / MODEL_FILE).read_text("utf-8"))
    data["embed_folder"] = second.relative_to(repo.root).as_posix()
    (model / MODEL_FILE).write_text(json.dumps(data), "utf-8")

    assert harness_main(["--dataset", str(dataset), "calibrate", str(model)], io) == 1
    assert "was trained on another embedding cache" in capsys.readouterr().err


def test_a_committed_embed_folder_not_named_live_embed_is_dry(
    world: tuple[FakeRepo, Path, Now],
) -> None:
    repo, dataset, now = world
    assert live_embed(repo, dataset, now) == 0
    [embed] = folders(repo.root, True, "-live-embed")
    renamed = embed.with_name(embed.name.replace("-live-embed", "-other-embed"))
    embed.rename(renamed)
    repo.commit(renamed)

    train = ["--dataset", str(dataset), "train", "--embed", str(renamed)]
    assert harness_main(train, make_io(repo, now=now)) == 0

    assert folders(repo.root, True, "-model") == []
    [model] = folders(repo.root, False, "-fake-model")
    reason = json.loads((model / MODEL_FILE).read_text("utf-8"))["dry_reason"]
    assert "not a -live-embed folder" in reason


def test_reformat_json_keeps_the_values(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text('{\n  "a": 1\n}\n', "utf-8")
    reformat_json(path)
    assert json.loads(path.read_text("utf-8")) == {"a": 1}
    assert path.read_text("utf-8") == '{"a": 1}\n'
