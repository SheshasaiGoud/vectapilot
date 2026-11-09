"""The replayed hybrid, `summarize` and `dry-run` (M4 PR 4).

- **`summarize RUN_DIR`** rewrites one folder's summary.md from its files (a hybrid folder's
  summary also reads its two input folders, for the comparison).
- **`summarize --classifier DIR --llm DIR`** computes the hybrid rows through H1's
  `route_message` with the replay fallback (`llmrun.hybrid_rows`) and writes a
  `-replay-hybrid-<split>` folder (`-fake-hybrid-<split>` when either input is dry); its summary
  holds the paired comparison of the three modes. It replays a complete classifier run and an LLM
  run that reached every row (a run made partial only by fail-closed rows on test is replayed;
  the hybrid is then partial too).
- **On test** it replays only the pinned, ledgered test runs, typed at a terminal, on a clean
  tree: any folder whose artifact, T, tau, sigma or prompt version is not the pin's, that the
  ledger does not name, or whose run.json or results.jsonl is not byte for byte the one in HEAD,
  is refused.
- **`dry-run`** chains a dry embed, train, calibrate, thresholds, both modes on dev and the hybrid,
  all under `evals/reports/tmp/` (what `make intent-dry-run` runs); refused on a dataset that is
  not frozen, before any step.

While H1 is unwritten the hybrid is not computed (`H1_MISSING`); nothing is written.
"""

import asyncio
from pathlib import Path
from typing import Final

from vectapilot_api.intenteval.harness import (
    Io,
    Loaded,
    Refused,
    committed_ledger,
    confirm_test,
    in_head,
    load,
    relative,
    require_frozen,
)
from vectapilot_api.intenteval.ledger import LEDGER_PATH, RunEntry, pin_for
from vectapilot_api.intenteval.llmrun import LlmRun, ReplayError, hybrid_rows
from vectapilot_api.intenteval.runinfo import (
    RESULTS_FILE,
    RUN_FILE,
    Fingerprint,
    IntentRunInfo,
    folder_name,
    read_results,
    read_run,
    run_dir,
    write_json,
)
from vectapilot_api.intenteval.runs import (
    EXIT_FAILED,
    EXIT_OK,
    exit_code,
    run_classifier,
    run_llm,
    status_on_split,
    summary_context,
    write_results,
    write_summary,
    zero_cost,
)
from vectapilot_api.intenteval.steps import (
    EmbedFolder,
    calibrate,
    embed,
    read_embed,
    thresholds,
    train,
)
from vectapilot_api.intents.decide import RouterThresholds
from vectapilot_api.llm_cli import GitInfo

H1_MISSING: Final = (
    "the hybrid is not computed: H1 (`route_message`, intents/decide.py) is not written yet"
    " (guide docs/learning/m4-router.md)"
)


# ── summarize ────────────────────────────────────────────────────────────────────────────────


def _embed_of(io: Io, root: Path, info: IntentRunInfo, loaded: Loaded) -> EmbedFolder | None:
    if info.embed_folder is None:
        return None
    return read_embed(io, root, root / info.embed_folder, loaded)


def _matching(loaded: Loaded, info: IntentRunInfo) -> None:
    if (info.dataset, info.dataset_version, info.freeze_sha256) != (
        loaded.dataset.manifest.dataset,
        loaded.version,
        loaded.freeze_sha256,
    ):
        raise Refused(f"{info.run_id} belongs to another dataset version or freeze")


def summarize_folder(io: Io, *, dataset: Path | None, folder: Path) -> int:
    """Rewrite one folder's summary.md from its files."""
    git = io.git()
    info = read_run(folder)
    if info.kind == "embed":
        raise Refused("an embed folder has no summary: train on it")
    loaded = load(dataset, git, io.head_file, live=False)
    _matching(loaded, info)
    lines = read_results(folder)
    embed_folder = _embed_of(io, git.root, info, loaded)
    modes = None
    if info.kind == "hybrid":
        classifier, llm = (git.root / path for path in info.inputs)
        modes = {"classifier": read_results(classifier), "llm": read_results(llm), "hybrid": lines}
    summarized = write_summary(
        folder, info, lines, summary_context(loaded, io, embed_folder), modes
    )
    return EXIT_OK if summarized else EXIT_FAILED


def summarize_hybrid(
    io: Io, *, dataset: Path | None, classifier_folder: Path, llm_folder: Path
) -> tuple[Path, int]:
    """The replayed hybrid of a classifier run and an LLM run of one split (module docstring)."""
    git = io.git()
    c_info, l_info = read_run(classifier_folder), read_run(llm_folder)
    if (c_info.kind, l_info.kind) != ("classifier", "llm"):
        raise Refused("--classifier takes a classifier folder and --llm an LLM folder")
    if c_info.status != "complete" or l_info.status == "failed":
        raise Refused("a hybrid replays a complete classifier run and an LLM run that did not fail")
    if any(line.decided_by == "not_run" for line in read_results(llm_folder)):
        raise Refused("the LLM run stopped before every row: a hybrid cannot replay unreached rows")
    loaded = load(dataset, git, io.head_file, live=False)
    _matching(loaded, c_info)
    _matching(loaded, l_info)
    if c_info.split != l_info.split or c_info.split is None:
        raise Refused("both folders must be runs of the same split")
    split = c_info.split
    real = not c_info.dry_run and not l_info.dry_run
    if split == "test":
        _check_hybrid_test(
            io, git, loaded, ((classifier_folder, c_info), (llm_folder, l_info)), real
        )
    fp_c, fp_l = c_info.fingerprint, l_info.fingerprint
    if fp_c.sigma is None:
        # sigma is always set where D8's thresholds are; tau None with it is "no confidence band".
        raise Refused(f"{c_info.run_id} has no sigma in its fingerprint")
    rows = {r.id: r for r in loaded.split_rows(split)}
    c_lines, l_lines = read_results(classifier_folder), read_results(llm_folder)
    try:
        lines = asyncio.run(
            hybrid_rows(
                c_lines,
                l_lines,
                rows,
                loaded.policies,
                RouterThresholds(tau=fp_c.tau, sigma=fp_c.sigma),
                io.router,
            )
        )
    except NotImplementedError:
        raise Refused(H1_MISSING) from None
    except ReplayError as error:
        raise Refused(f"the replay cannot answer: {error}") from None
    fingerprint = Fingerprint(
        freeze_sha256=fp_c.freeze_sha256,
        artifact_sha256=fp_c.artifact_sha256,
        temperature=fp_c.temperature,
        tau=fp_c.tau,
        sigma=fp_c.sigma,
        router_prompt_version=fp_l.router_prompt_version,
        catalog_version=fp_l.catalog_version,
        router_model=fp_l.router_model,
    )
    started = io.now()
    name = folder_name("hybrid", real=real, started=started, split=split)
    out = run_dir(git.root, loaded.version, name, real=real)
    out.mkdir(parents=True, exist_ok=False)
    write_results(out, lines)
    dry_reason = None if real else (c_info.dry_reason or l_info.dry_reason or "dry input")
    status = status_on_split(LlmRun(status="complete", stop=None), lines, split)
    info = c_info.model_copy(
        update={
            "kind": "hybrid",
            "status": status.status,
            "stop": status.stop,
            "run_id": name,
            "dry_run": not real,
            "dry_reason": dry_reason,
            "commit": git.commit,
            "uncommitted_changes": git.uncommitted_changes,
            "started_at": started,
            "ended_at": io.now(),
            "fingerprint": fingerprint,
            "fingerprint_sha256": fingerprint.sha256(),
            "inputs": (relative(git.root, classifier_folder), relative(git.root, llm_folder)),
            "llm_provider": l_info.llm_provider,
            "router_alias": l_info.router_alias,
            "cost": zero_cost(),
            "test_rerun_reason": None,
            "test_runs_before": 0,
        }
    )
    info = IntentRunInfo.model_validate(info.model_dump())
    write_json(out / RUN_FILE, info)
    embed_folder = _embed_of(io, git.root, c_info, loaded)
    modes = {"classifier": c_lines, "llm": l_lines, "hybrid": lines}
    summarized = write_summary(out, info, lines, summary_context(loaded, io, embed_folder), modes)
    print(f"Written to {out} ({'real' if real else 'DRY RUN: git ignores it'}).")
    return out, exit_code(info, summarized)


def _check_hybrid_test(
    io: Io,
    git: GitInfo,
    loaded: Loaded,
    inputs: tuple[tuple[Path, IntentRunInfo], tuple[Path, IntentRunInfo]],
    real: bool,
) -> None:
    """The hybrid on test replays only the pinned, ledgered test runs, byte for byte as committed
    in HEAD (module docstring). The clean-tree rule cannot see report folders (`GitInfo` ignores
    `evals/reports/`), so each input's run.json and results.jsonl is checked against HEAD here."""
    (c_folder, c_info), (l_folder, l_info) = inputs
    root = git.root
    if not real:
        raise Refused("dry runs are refused on the test split")
    if git.uncommitted_changes:
        raise Refused(
            "a hybrid on test needs a clean working tree: commit first, so HEAD names the code"
        )
    for folder, info in ((c_folder, c_info), (l_folder, l_info)):
        for name in (RUN_FILE, RESULTS_FILE):
            if not in_head(root, folder / name, io.head_file):
                raise Refused(f"{info.run_id}/{name} is not committed in HEAD as on disk")
    entries = committed_ledger(io, root)
    freeze = require_frozen(loaded, "a hybrid on test")
    pin = pin_for(entries, loaded.dataset.manifest.dataset, loaded.version, freeze)
    if pin is None:
        raise Refused(f"no pin in {LEDGER_PATH}")
    fp = c_info.fingerprint
    if (fp.artifact_sha256, fp.temperature, fp.tau, fp.sigma) != (
        pin.artifact_sha256,
        pin.temperature,
        pin.tau,
        pin.sigma,
    ):
        raise Refused("the classifier folder is not the pinned artifact, T, tau and sigma")
    if l_info.fingerprint.router_prompt_version != pin.router_prompt_version:
        raise Refused("the LLM folder is not the pinned prompt version")
    ledgered = {e.run_id for e in entries if isinstance(e, RunEntry)}
    for info in (c_info, l_info):
        if info.run_id not in ledgered:
            raise Refused(f"{info.run_id} is not a ledgered test run in {LEDGER_PATH}")
    if not confirm_test(io, "test hybrid"):
        raise Refused("not confirmed")


# ── dry-run ──────────────────────────────────────────────────────────────────────────────────


def dry_run(io: Io, *, dataset: Path | None) -> int:
    """Every step on dev, dry, in `evals/reports/tmp/` (module docstring). Refused before any
    step on a dataset that is not frozen: train, calibrate and thresholds need dev."""
    require_frozen(load(dataset, io.git(), io.head_file, live=False), "dry-run")
    embed_folder = embed(io, dataset=dataset, live=False, max_usd=None, cap_text=None)
    model_folder = train(io, dataset=dataset, embed_folder=embed_folder)
    calibrate(io, dataset=dataset, model_folder=model_folder)
    thresholds(io, dataset=dataset, model_folder=model_folder)
    classifier, code_c = run_classifier(
        io, dataset=dataset, model_folder=model_folder, split="dev", reason=None
    )
    llm, code_l = run_llm(
        io, dataset=dataset, split="dev", live=False, max_usd=None, cap_text=None, reason=None
    )
    _, code_h = summarize_hybrid(io, dataset=dataset, classifier_folder=classifier, llm_folder=llm)
    return max(code_c, code_l, code_h)
