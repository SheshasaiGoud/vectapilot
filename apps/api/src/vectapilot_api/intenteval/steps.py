"""The harness's preparation steps (M4 PR 4): `embed`, `train`, `calibrate`, `thresholds`, `pin`.

- `embed` is dry by default (the fake embedder, $0, into `evals/reports/tmp/`). `--live
  --max-usd X` is shesha's alone: Voyage through `CappedEmbedder`, the cap typed again at a
  terminal, refused above what is left of M4's $2.00 (D7) and unless every manifest says
  synthetic and the dataset is frozen. Its folder is committed on `m4/close-out` before the next
  live run. A cap stop leaves it `partial` (exit 3); an outage or a bug leaves it `failed`, with
  run.json and its counted spend all the same (as M3's harness does), so M4's spend counts the
  real figure rather than the $1.00 per-run ceiling.
- `train` fits on the training rows only (`train.py`), `calibrate` fits T on dev, `thresholds`
  applies D8's rule on dev; each refuses a cache that does not match the frozen rows, and each
  writes into the model folder once (never overwriting).
- `pin` appends the test-run ledger's pin (`ledger.py`): the model folder must be real and
  committed, the tree clean, and the person at a terminal.

Every step prints where it wrote and whether the folder is real or a DRY RUN. No text is
written anywhere: ids, hashes, vectors and numbers.
"""

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.embeddings.runner import EmbeddingRunner, batch_deadline_s
from vectapilot_api.evalrun.budget import CappedEmbedder, CostCap, embedding_reservation, usd
from vectapilot_api.evalrun.intervals import DEFAULT_CONFIDENCE
from vectapilot_api.evalrun.ledger import append_ledger
from vectapilot_api.evalrun.report import DEFAULT_BOOTSTRAP_SEED, DEFAULT_RESAMPLES
from vectapilot_api.intenteval import thresholds as d8
from vectapilot_api.intenteval.cache import (
    CACHE_FILE,
    CacheEntry,
    cache_problems,
    normalise_text,
    parse_cache,
    render_cache,
)
from vectapilot_api.intenteval.classify import dev_logits, dev_rows
from vectapilot_api.intenteval.embed import Embedded, embed_rows
from vectapilot_api.intenteval.harness import (
    Io,
    Loaded,
    Refused,
    check_settings,
    committed_ledger,
    confirm_test,
    corpus_synthetic,
    cost_info,
    embed_realness,
    in_head,
    load,
    relative,
    require_frozen,
    spend_lines,
)
from vectapilot_api.intenteval.ledger import LEDGER_PATH, PinEntry, pin_for
from vectapilot_api.intenteval.model import (
    ARTIFACT_FILE,
    CALIBRATION_FILE,
    DEV_CURVE_FILE,
    MODEL_FILE,
    RAW_ARTIFACT_FILE,
    THRESHOLDS_FILE,
    CalibrationInfo,
    ModelInfo,
    SigmaPointInfo,
    TauPointInfo,
    ThresholdsInfo,
    read_model,
    read_model_info,
    read_raw_artifact,
    sha256_of,
)
from vectapilot_api.intenteval.runinfo import (
    EMBEDDING_LOG_FILE,
    RUN_FILE,
    Fingerprint,
    IntentRunInfo,
    folder_name,
    read_run,
    run_dir,
    write_json,
)
from vectapilot_api.intenteval.train import build_artifact, fit_logistic
from vectapilot_api.intents.calibration import fit_temperature
from vectapilot_api.intents.classifier import ClassifierArtifact, artifact_json
from vectapilot_api.intents.fallback import ROUTER_PROMPT_VERSION
from vectapilot_api.llm_cli import captured, confirmed

EMBEDDINGS_LOGGER = "vectapilot_api.embeddings"
M4_KEEP = (
    "commit this folder on m4/close-out before the next live run, never delete it (without"
    " run.json it counts at the $1.00 per-run ceiling until its spend is recorded)"
)


def _where(folder: Path, real: bool) -> str:
    return f"{folder} ({'real' if real else 'DRY RUN: git ignores it; plumbing only'})"


# ── embed ────────────────────────────────────────────────────────────────────────────────────


def embed(
    io: Io, *, dataset: Path | None, live: bool, max_usd: Decimal | None, cap_text: str | None
) -> Path:
    git = io.git()
    loaded = load(dataset, git, io.head_file, live=live)
    manifest = loaded.dataset.manifest
    if live:
        require_frozen(loaded, "a live embed")
        if not (manifest.synthetic and corpus_synthetic(loaded)):
            raise Refused("--live needs every manifest to say synthetic: true")
    settings = io.load_settings(live)
    check_settings(settings, live=live, needs="embeddings")
    catalog = load_embedding_catalog()
    spec = catalog.resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    price = catalog.price(spec.model)
    train, eval_rows = loaded.dataset.train, loaded.dataset.eval_rows
    if not train:
        raise Refused("no training rows: nothing to embed")
    texts = [normalise_text(r.text) for r in (*train, *eval_rows)]
    bound, worst = embedding_reservation(texts, price)
    print(
        f"{manifest.dataset} {manifest.version} ({manifest.stage}): embed {len(train)} train rows"
        f" (batched per business) and {len(eval_rows)} eval rows (one per request) as queries"
        f" with {spec.model}.\nMode: "
        + (
            "LIVE: paid embedding requests."
            if live
            else "DRY RUN: the fake embedder; nothing is sent or billed, and its vectors mean"
            " nothing."
        )
        + f"\nWorst case at list price: {usd(worst)} ({bound} tokens bound)."
        + ("" if max_usd is None else f" Cap: {usd(max_usd)}."),
        flush=True,
    )
    if live:
        if max_usd is None:
            raise Refused("--live requires --max-usd")
        print(spend_lines(io, git, "live embed", max_usd))
        if not confirmed(io.stdin, cap_text or "", stdout_is_terminal=io.stdout_is_terminal()):
            raise Refused("not confirmed")
    cap = CostCap(cap=max_usd)
    inner = io.build_embedder(settings, spec) if live else FakeEmbedder(spec)
    embedder = CappedEmbedder(inner, price, cap)
    policy = settings.llm_retry_policy()
    runner = EmbeddingRunner(
        embedder,
        spec,
        policy,
        deadline_s=batch_deadline_s(policy, settings.embeddings_timeout_seconds),
    )
    started = io.now()
    name = folder_name("embed", real=live, started=started)
    out = run_dir(git.root, manifest.version, name, real=live)
    out.mkdir(parents=True, exist_ok=False)
    fingerprint = Fingerprint(freeze_sha256=loaded.freeze_sha256)

    def run_info(**fields: object) -> IntentRunInfo:
        """run.json with what every outcome shares; `fields` holds the outcome's own."""
        return IntentRunInfo.model_validate(
            {
                "kind": "embed",
                "run_id": name,
                "dry_run": not live,
                "dry_reason": None if live else "fake embedder",
                "environment": settings.environment,
                "dataset": manifest.dataset,
                "dataset_version": manifest.version,
                "dataset_stage": manifest.stage,
                "freeze_sha256": loaded.freeze_sha256,
                "dataset_committed": loaded.committed,
                "commit": git.commit,
                "uncommitted_changes": git.uncommitted_changes,
                "started_at": started,
                "ended_at": io.now(),
                "fingerprint": fingerprint,
                "fingerprint_sha256": fingerprint.sha256(),
                "confidence": str(DEFAULT_CONFIDENCE),
                "bootstrap_seed": DEFAULT_BOOTSTRAP_SEED,
                "bootstrap_resamples": DEFAULT_RESAMPLES,
                "cost": cost_info(cap),
            }
            | fields
        )

    with (
        (out / EMBEDDING_LOG_FILE).open("x", encoding="utf-8") as log,
        captured(logging.getLogger(EMBEDDINGS_LOGGER), log),
    ):
        try:
            embedded = asyncio.run(_embed(runner, cap, embedder, loaded, io))
        except Exception as error:  # an outage or a bug: the class name only
            # run.json with the counted spend, as M3 does (evalrun/cli.py): without it the
            # folder would count at the $1.00 per-run ceiling. No cache: `read_embed` refuses it.
            write_json(
                out / RUN_FILE,
                run_info(
                    status="failed",
                    stop=f"{type(error).__name__} (message not shown)",
                    rows=0,
                    embedding_model=spec.model,
                ),
            )
            print(
                f"{type(error).__name__}: the embedding FAILED; counted spend {usd(cap.total)}."
                f" Its call records and run.json are in {out}" + (f"; {M4_KEEP}" if live else "")
            )
            raise Refused("the embedding failed (its message is not shown)") from None
    (out / CACHE_FILE).write_text(render_cache(embedded.entries), encoding="utf-8")
    models = sorted({e.model for e in embedded.entries})
    info = run_info(
        status="complete" if embedded.stop is None else "partial",
        stop=embedded.stop,
        rows=len(embedded.entries),
        embedding_model=models[0] if len(models) == 1 else spec.model,
        embedding_cache_sha256=sha256_of(out / CACHE_FILE),
    )
    write_json(out / RUN_FILE, info)
    print(
        f"embed {info.status}: {len(embedded.entries)} vectors"
        + (f" (stopped: {embedded.stop})" if embedded.stop else "")
        + f", spend {usd(cap.total)}. Written to {_where(out, live)}"
        + (f"; {M4_KEEP}." if live else ".")
    )
    return out


async def _embed(
    runner: EmbeddingRunner,
    cap: CostCap,
    embedder: CappedEmbedder,
    loaded: Loaded,
    io: Io,
) -> Embedded:
    try:
        return await embed_rows(
            runner,
            cap,
            loaded.dataset.train,
            loaded.dataset.eval_rows,
            clock=io.clock,
            progress=lambda line: print(line, flush=True),
        )
    finally:
        await embedder.aclose()


# ── Reading an embed folder ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbedFolder:
    folder: Path
    info: IntentRunInfo
    entries: tuple[CacheEntry, ...]
    cache_sha256: str
    dry_reason: str | None

    @property
    def real(self) -> bool:
        return self.dry_reason is None

    def by_id(self) -> dict[str, CacheEntry]:
        return {e.id: e for e in self.entries}


def read_embed(io: Io, root: Path, folder: Path, loaded: Loaded) -> EmbedFolder:
    """An embed folder whose cache matches the dataset's rows; refused otherwise."""
    info = read_run(folder)
    if info.kind != "embed":
        raise Refused(f"{folder.name} is not an embed folder")
    if info.status != "complete":
        raise Refused(f"{folder.name} is {info.status}: embed again")
    entries = parse_cache((folder / CACHE_FILE).read_text("utf-8"))
    sha = sha256_of(folder / CACHE_FILE)
    if sha != info.embedding_cache_sha256:
        raise Refused(f"{folder.name}/{CACHE_FILE} is not the cache its run.json names")
    if info.freeze_sha256 != loaded.freeze_sha256:
        raise Refused(f"{folder.name} embedded another freeze of the dataset: embed again")
    problems = cache_problems(entries, loaded.dataset.train, loaded.dataset.eval_rows)
    if problems:
        for problem in problems:
            print(f"PROBLEM  {problem}")
        raise Refused(f"{folder.name}'s cache does not match the frozen rows")
    reason = embed_realness(root, folder, info, CACHE_FILE, io.head_file)
    return EmbedFolder(folder, info, entries, sha, reason)


# ── train, calibrate, thresholds ─────────────────────────────────────────────────────────────


def train(io: Io, *, dataset: Path | None, embed_folder: Path) -> Path:
    git = io.git()
    loaded = load(dataset, git, io.head_file, live=False)
    freeze = require_frozen(loaded, "train")
    source = read_embed(io, git.root, embed_folder, loaded)
    cache = source.by_id()
    rows = loaded.dataset.train
    fit = fit_logistic([cache[r.id].values() for r in rows], [r.intent for r in rows])
    first = source.entries[0]
    raw = build_artifact(
        fit,
        embedding_model=first.model,
        dimension=first.dimension,
        temperature=1.0,
        dataset_version=loaded.version,
        freeze_sha256=freeze,
        embedding_cache_sha256=source.cache_sha256,
    )
    started = io.now()
    real = source.real
    name = folder_name("model", real=real, started=started)
    out = run_dir(git.root, loaded.version, name, real=real)
    out.mkdir(parents=True, exist_ok=False)
    write_json(
        out / MODEL_FILE,
        ModelInfo(
            dry_run=not real,
            dry_reason=source.dry_reason,
            dataset=loaded.dataset.manifest.dataset,
            dataset_version=loaded.version,
            freeze_sha256=freeze,
            embed_folder=relative(git.root, embed_folder),
            embedding_model=first.model,
            dimension=first.dimension,
            embedding_cache_sha256=source.cache_sha256,
            train_rows=len(rows),
            commit=git.commit,
            uncommitted_changes=git.uncommitted_changes,
            trained_at=started,
        ),
    )
    with (out / RAW_ARTIFACT_FILE).open("x", encoding="utf-8") as handle:
        handle.write(artifact_json(raw))
    print(
        f"train: {len(rows)} training rows, {fit.n_iter} iterations ({raw.train_config.solver},"
        f" C {raw.train_config.c}, scikit-learn {fit.library_version}). Written to"
        f" {_where(out, real)}. Next: calibrate {out}"
    )
    return out


def checked_inputs(
    io: Io, model_folder: Path, dataset: Path | None
) -> tuple[Loaded, EmbedFolder, ModelInfo]:
    git = io.git()
    info = read_model_info(model_folder)
    loaded = load(dataset, git, io.head_file, live=False)
    freeze = require_frozen(loaded, "this step")
    if freeze != info.freeze_sha256:
        raise Refused(f"{model_folder.name} was trained on another freeze of the dataset")
    source = read_embed(io, git.root, git.root / info.embed_folder, loaded)
    if source.cache_sha256 != info.embedding_cache_sha256:
        raise Refused(f"{model_folder.name} was trained on another embedding cache")
    return loaded, source, info


def calibrate(io: Io, *, dataset: Path | None, model_folder: Path) -> Path:
    loaded, source, _ = checked_inputs(io, model_folder, dataset)
    raw = read_raw_artifact(model_folder)
    rows = loaded.split_rows("dev")
    if not rows:
        raise Refused("no dev rows: calibration fits T on dev")
    logits, labels = dev_logits(rows, raw, source.by_id())
    fitted = fit_temperature(logits, labels)
    artifact = ClassifierArtifact.model_validate(
        raw.model_dump() | {"temperature": fitted.temperature}
    )
    with (model_folder / ARTIFACT_FILE).open("x", encoding="utf-8") as handle:
        handle.write(artifact_json(artifact))
    write_json(
        model_folder / CALIBRATION_FILE,
        CalibrationInfo(
            temperature=fitted.temperature,
            nll=fitted.nll,
            nll_at_1=fitted.nll_at_1,
            dev_rows=fitted.rows,
            at_bound=fitted.at_bound,
            raw_artifact_sha256=sha256_of(model_folder / RAW_ARTIFACT_FILE),
            artifact_sha256=sha256_of(model_folder / ARTIFACT_FILE),
        ),
    )
    bound = (
        f" WARNING: T sits on the {fitted.at_bound} bound: the dev rows cannot place it"
        if fitted.at_bound
        else ""
    )
    print(
        f"calibrate on {fitted.rows} dev rows: T {fitted.temperature:.4f}, mean NLL"
        f" {fitted.nll_at_1:.4f} at T = 1 → {fitted.nll:.4f}.{bound} Next: thresholds"
        f" {model_folder}"
    )
    return model_folder


def thresholds(io: Io, *, dataset: Path | None, model_folder: Path) -> Path:
    loaded, source, info = checked_inputs(io, model_folder, dataset)
    if not (model_folder / ARTIFACT_FILE).is_file():
        raise Refused(f"{model_folder.name} has no {ARTIFACT_FILE}: calibrate first")
    artifact = ClassifierArtifact.model_validate_json((model_folder / ARTIFACT_FILE).read_bytes())
    rows = loaded.split_rows("dev")
    if not rows:
        raise Refused("no dev rows: D8's rule runs on dev")
    choice = d8.choose(dev_rows(rows, artifact, source.by_id(), loaded.policies))
    sha = sha256_of(model_folder / ARTIFACT_FILE)
    write_json(
        model_folder / THRESHOLDS_FILE,
        ThresholdsInfo(
            rule_version=d8.RULE_VERSION,
            tau=choice.tau,
            sigma=choice.sigma,
            tau_grid=d8.TAU_GRID,
            sigma_grid=d8.SIGMA_GRID,
            min_kept=d8.MIN_KEPT,
            target_accuracy=str(d8.TARGET_ACCURACY),
            artifact_sha256=sha,
            dev_rows=choice.dev_rows,
            safety_rows=choice.safety_rows,
            still_missed=choice.still_missed,
            tau_curve=tuple(
                TauPointInfo(tau=p.tau, kept=p.kept, correct=p.correct) for p in choice.tau_curve
            ),
            sigma_curve=tuple(
                SigmaPointInfo(
                    sigma=s.sigma,
                    decided_non_safety=s.decided_non_safety,
                    emergency_as_judgement=s.emergency_as_judgement,
                    safety_to_llm=s.safety_to_llm,
                    caught_non_safety=s.caught_non_safety,
                    non_safety_rows=s.non_safety_rows,
                )
                for s in choice.sigma_curve
            ),
        ),
    )
    head = [
        f"# Dev curve of {model_folder.name}",
        "",
        f"artifact {sha[:12]}, dataset {loaded.dataset.manifest.dataset} {loaded.version} freeze"
        f" {info.freeze_sha256[:12]}, commit {io.git().commit[:12]}"
        + ("" if source.real else f". **DRY RUN** ({source.dry_reason}): plumbing only"),
    ]
    with (model_folder / DEV_CURVE_FILE).open("x", encoding="utf-8") as handle:
        handle.write(d8.render_dev_curve(choice, head))
    print(
        f"thresholds (D8, rule {d8.RULE_VERSION}) on {choice.dev_rows} dev rows: tau"
        f" {d8.tau_text(choice.tau)}, sigma {choice.sigma:g}; dev safety rows still missed:"
        f" {', '.join(choice.still_missed) or 'none'}. Written to"
        f" {model_folder / THRESHOLDS_FILE} and {DEV_CURVE_FILE}"
    )
    return model_folder


# ── pin ──────────────────────────────────────────────────────────────────────────────────────


def pin(io: Io, *, dataset: Path | None, model_folder: Path) -> None:
    git = io.git()
    model = read_model(model_folder)
    loaded, source, _ = checked_inputs(io, model_folder, dataset)
    freeze = require_frozen(loaded, "a pin")
    if not source.real or model.info.dry_run:
        raise Refused(f"{model_folder.name} is a dry model ({source.dry_reason}): never pinned")
    if git.uncommitted_changes:
        raise Refused("a pin needs a clean working tree: commit first, so HEAD names the code")
    for name in (MODEL_FILE, ARTIFACT_FILE, CALIBRATION_FILE, THRESHOLDS_FILE):
        if not in_head(git.root, model_folder / name, io.head_file):
            raise Refused(f"{model_folder.name}/{name} is not committed in HEAD as on disk")
    entries = committed_ledger(io, git.root)
    name = loaded.dataset.manifest.dataset
    if pin_for(entries, name, loaded.version, freeze) is not None:
        raise Refused(f"{name} {loaded.version} is already pinned: a new pin needs a new version")
    t = model.thresholds
    print(
        f"Pin for every test run of {name} {loaded.version} (freeze {freeze[:12]}): artifact"
        f" {model.artifact_sha256[:12]}, T {model.artifact.temperature:.4f}, tau"
        f" {d8.tau_text(t.tau)}, sigma {t.sigma:g}, prompt {ROUTER_PROMPT_VERSION}, from"
        f" {model_folder.name}."
    )
    if not confirm_test(io, f"pin {loaded.version}"):
        raise Refused("not confirmed")
    append_ledger(
        git.root / LEDGER_PATH,
        PinEntry(
            dataset=name,
            version=loaded.version,
            freeze_sha256=freeze,
            model_folder=relative(git.root, model_folder),
            artifact_sha256=model.artifact_sha256,
            temperature=model.artifact.temperature,
            tau=t.tau,
            sigma=t.sigma,
            router_prompt_version=ROUTER_PROMPT_VERSION,
            commit=git.commit,
            pinned_at=io.now(),
        ),
    )
    print(f"Pinned in {LEDGER_PATH}: commit it before the first test run.")
