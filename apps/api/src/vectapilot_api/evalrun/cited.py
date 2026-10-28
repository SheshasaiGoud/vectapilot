"""The label sheet's cited text (M3 PR 7): each shown answer's cited chunks, rebuilt from the
corpus at the run's chunk config, and only once the corpus is shown to be the one the run used.

A run folder keeps no chunk text (ADR 0013 decision 6): `answers.jsonl` names each cited chunk
by source, version, chunk index and page (`scoring.SourceRef`). A faithfulness label needs the
text the answer cites, so `label-sheet` rebuilds it with the real loader and chunker at run.json's
chunk config (`Corpus.chunks`), the code `ingest.verify_stored` compared the stored chunks with
before the run's first question (ADR 0013 decision 3).

**Refused unless it is the run's corpus** (`LabelSheetRefused`; ids, hashes and rules, never
text, and every value from a run file escaped with `terminal_text`), in this order, before
anything is printed:

1. the dataset folder is the run's (run.json's dataset and version), and it lists every business
   the run used;
2. freeze.json's bytes hash to run.json's `freeze_sha256`: the same freeze;
3. every question, corpus file and corpus manifest matches that freeze.json (`content_hash`,
   `Corpus.pins`), so the questions and the documents are the ones frozen with it;
4. per cited chunk: it is a chunk the prompt held (results.jsonl's `evidence_chunk_ids`, ADR
   0011) and one of its question's hits in results.jsonl with the same source, version, chunk
   index and page; the corpus has that document, and it is one a customer may see (approved and
   public, `answers_customers`); the corpus has that chunk index at the config; and the rebuilt
   chunk has that page and finds the evidence groups the run recorded for it
   (`relevance.matched_groups`).

Step 4's audience check repeats the run's own: a run stops on a hit no customer may see
(`scoring.AudienceLeak`), so a shown answer citing a draft, retired or staff-only document means
an edited folder or a harness bug. It is refused as a leak signal (a hard gate), never shown as
evidence to label. Step 4's last check is a tripwire for a loader or chunker changed since the
run, not a proof: a change that keeps every anchor in its chunk and the page as it was passes it.

A run on an unfrozen dataset (only a dry run can be one) has no freeze to check against, so its
cited text is not shown and the sheet says why. Step 4's first check, a chunk the prompt held,
needs no corpus and runs for it too (decided 2026-10-28, shesha). **Terminal only:** the text
goes back to the CLI, which prints it; nothing here writes a file or logs.
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Final

from vectapilot_api.evalrun.report import CitedText, RunInfo, terminal_text
from vectapilot_api.evalrun.scoring import AnswerLine, HitRef, QuestionResult, SourceRef
from vectapilot_api.evalset.files import Dataset
from vectapilot_api.evalset.questions import FREEZE_FILE, EvalQuestion
from vectapilot_api.evalset.relevance import RetrievedChunk, matched_groups
from vectapilot_api.evalset.split import FreezeManifest, content_hash
from vectapilot_api.ingestion.types import ChunkingConfig

NOT_FROZEN: Final = (
    "text not shown: run.json has no freeze_sha256 (an unfrozen dataset, so a dry run), and an"
    " unfrozen corpus cannot be checked against the one the run used"
)
"""Each cited chunk's line in the sheet of a run without a freeze."""


class LabelSheetRefused(ValueError):
    """The label sheet cannot show the cited text the run saw. Ids, hashes and rules only."""


def _id(value: str) -> str:
    """A value from a run or dataset file, as a refusal names it: on one line, with every control
    and bidi character escaped (`report.terminal_text`), since the CLI prints it to stderr."""
    return terminal_text(value, multiline=False)


def _config(run: RunInfo) -> ChunkingConfig:
    return ChunkingConfig(max_chars=run.config.max_chars, overlap_chars=run.config.overlap_chars)


def _config_text(run: RunInfo) -> str:
    return f"chunks {run.config.max_chars}/{run.config.overlap_chars}"


def freeze_differences(dataset: Dataset, freeze: FreezeManifest) -> list[str]:
    """Every way the dataset's questions, corpus files and corpus manifests differ from `freeze`
    (empty: none). Problems name ids, never text."""
    problems: list[str] = []
    pins = dataset.corpus.pins()
    for business_id in dataset.manifest.businesses:
        if dict(pins.files.get(business_id, {})) != freeze.corpus.get(business_id):
            problems.append(f"the corpus files of {business_id} differ from {FREEZE_FILE}")
        if pins.manifests.get(business_id) != freeze.corpus_manifests.get(business_id):
            problems.append(f"the corpus manifest of {business_id} differs from {FREEZE_FILE}")
    present = {question.id for question in dataset.questions}
    for question in dataset.questions:
        frozen = freeze.questions.get(question.id)
        if frozen is None or frozen.sha256 != content_hash(question):
            problems.append(f"question {question.id} is not as frozen")
    problems += [
        f"question {question_id} was frozen and is gone"
        for question_id in sorted(set(freeze.questions) - present)
    ]
    return problems


def check_corpus(run: RunInfo, dataset: Dataset, freeze_bytes: bytes | None) -> str:
    """Refuse (`LabelSheetRefused`) unless the dataset folder, its freeze.json, its questions and
    its corpus are the run's (module docstring, steps 1-3); return the sheet's provenance line.
    `freeze_bytes` is freeze.json as on disk (None: no file)."""
    manifest = dataset.manifest
    if (manifest.dataset, manifest.version) != (run.dataset, run.dataset_version):
        raise LabelSheetRefused(
            f"the dataset folder holds {_id(manifest.dataset)} {_id(manifest.version)}, the run"
            f" used {_id(run.dataset)} {_id(run.dataset_version)}: pass --dataset with the run's"
            " version folder"
        )
    missing = sorted(set(run.namespaces) - set(manifest.businesses))
    if missing:
        raise LabelSheetRefused(
            "the run used business(es) the dataset manifest does not list:"
            f" {', '.join(map(_id, missing))}"
        )
    if run.freeze_sha256 is None:
        return (
            "Cited text: not shown. run.json has no freeze_sha256 (an unfrozen dataset, so a dry"
            " run): the corpus cannot be checked against the one the run used."
        )
    if freeze_bytes is None or dataset.freeze is None:
        raise LabelSheetRefused(
            f"{FREEZE_FILE} is missing; the run's has sha256 {_id(run.freeze_sha256[:12])}"
        )
    found = hashlib.sha256(freeze_bytes).hexdigest()
    if found != run.freeze_sha256:
        raise LabelSheetRefused(
            f"{FREEZE_FILE} has sha256 {found[:12]}, the run's {_id(run.freeze_sha256[:12])}: not"
            " the freeze the run used"
        )
    problems = freeze_differences(dataset, dataset.freeze)
    if problems:
        raise LabelSheetRefused(
            f"the dataset or corpus differs from the run's {FREEZE_FILE} (restore the files of"
            f" the run's commit {_id(run.commit[:12])}): {'; '.join(problems)}"
        )
    return (
        f"Cited text: rebuilt from the corpus at {_config_text(run)} (run.json's config) with this"
        f" checkout's loader and chunker. Checked first: {FREEZE_FILE} is the run's (sha256"
        f" {run.freeze_sha256[:12]}), and the questions, corpus files and corpus manifests match"
        " it; each cited chunk matches the run's record of it (source, chunk index, page,"
        " evidence groups)."
    )


def cited_texts(
    run: RunInfo,
    results: Sequence[QuestionResult],
    answers: Sequence[AnswerLine],
    dataset: Dataset,
) -> dict[str, tuple[CitedText, ...]]:
    """Each shown answer's cited chunks with their rebuilt text, by question id (module
    docstring, step 4). Call `check_corpus` first: this trusts the corpus to be the run's."""
    by_id = {result.question_id: result for result in results}
    questions = {question.id: question for question in dataset.questions}
    cited: dict[str, tuple[CitedText, ...]] = {}
    for number, answer in enumerate(answers, start=1):
        result = by_id.get(answer.question_id)
        if result is None:
            raise LabelSheetRefused(f"answers.jsonl entry {number}: not a question of the run")
        if (result.outcome, result.sources) != (answer.outcome, answer.sources):
            raise LabelSheetRefused(
                f"question {_id(result.question_id)}: answers.jsonl and results.jsonl disagree on"
                " its outcome or cited sources"
            )
        if answer.outcome != "answered":
            continue
        # Needs no corpus, so it runs for an unfrozen (dry) run too (decided 2026-10-28, shesha).
        held = set(result.evidence_chunk_ids or ())
        for citation, source in enumerate(answer.sources, start=1):
            if source.chunk_id not in held:
                raise LabelSheetRefused(
                    f"question {_id(result.question_id)}, citation {citation}: not a chunk the"
                    " prompt held (results.jsonl's evidence_chunk_ids)"
                )
        if run.freeze_sha256 is None:
            cited[result.question_id] = tuple(
                CitedText(source=source, text=None, why_not_shown=NOT_FROZEN)
                for source in answer.sources
            )
            continue
        question = questions.get(result.question_id)
        if question is None:
            raise LabelSheetRefused(f"question {_id(result.question_id)} is not in the dataset")
        hits = {hit.chunk_id: hit for hit in result.hits}
        cited[result.question_id] = tuple(
            _rebuilt(run, dataset, result, question, hits, source, citation)
            for citation, source in enumerate(answer.sources, start=1)
        )
    return cited


def _rebuilt(
    run: RunInfo,
    dataset: Dataset,
    result: QuestionResult,
    question: EvalQuestion,
    hits: Mapping[str, HitRef],
    source: SourceRef,
    citation: int,
) -> CitedText:
    """One cited chunk's text, rebuilt and checked against the run's record (step 4)."""
    where = f"question {_id(result.question_id)}, citation {citation}"
    named = f"{_id(source.source_id)} {_id(source.version)}"
    hit = hits.get(source.chunk_id)
    if hit is None or (hit.source_id, hit.version, hit.chunk_index, hit.page) != (
        source.source_id,
        source.version,
        source.chunk_index,
        source.page,
    ):
        raise LabelSheetRefused(
            f"{where}: not one of the question's hits in results.jsonl with the same source,"
            " version, chunk index and page"
        )
    document = dataset.corpus.document(result.business_id, source.source_id, source.version)
    if document is None:
        raise LabelSheetRefused(f"{where}: {named} is not in {_id(result.business_id)}'s corpus")
    if not document.source.answers_customers:
        raise LabelSheetRefused(
            f"{where}: cites a document no customer may see ({named}: status"
            f" {document.source.status}, audience {document.source.audience}): a leak signal, hard"
            " gate"
        )
    chunks = dataset.corpus.chunks(document, _config(run))
    chunk = next((c for c in chunks if c.chunk_index == source.chunk_index), None)
    if chunk is None:
        raise LabelSheetRefused(
            f"{where}: {named} has no chunk {source.chunk_index} at {_config_text(run)}"
        )
    rebuilt = RetrievedChunk(
        business_id=result.business_id,
        source_id=source.source_id,
        version=source.version,
        content=chunk.content,
    )
    if chunk.page != source.page or matched_groups(rebuilt, question) != hit.groups:
        raise LabelSheetRefused(
            f"{where}: the chunk rebuilt at {_config_text(run)} is not the one the run cited (its"
            " page or the evidence groups it finds differ): has the loader or chunker changed"
            f" since the run's commit {_id(run.commit[:12])}?"
        )
    return CitedText(source=source, text=chunk.content)
