"""Ingest the eval corpus through the product, then prove the stored chunks are this config's.

**Ingestion is the product's path.** Every document listed in a business's corpus manifest
(`data/sample/<business_id>/manifest.json`) is uploaded with `KbService.ingest`, exactly as
`POST /v1/kb/documents` does (the same loader, chunker, embedding runner and store), into that
business's eval namespace (`namespace.py`), in manifest order, with the manifest's upload
parameters. `KbService` is built with the run's `ChunkingConfig`. Re-uploading a stored version
with the same bytes and status costs nothing and changes nothing (PR 5), so `run` can always
ingest first.

**Then the stored chunks are checked against the corpus at the run's config** (`verify_stored`):
for every listed document, the stored status and audience are the manifest's, and the stored
chunks are, in order and character for character, what the real chunker makes of the document at
this `ChunkingConfig`; no other document is stored; every chunk carries the run's embedding model.
This catches what idempotent ingestion cannot: a namespace filled by another chunker version, a
document edited under an old version, or a leftover document. The way out is the same command
with `--fresh`.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from vectapilot_api.evalrun.store import StoredChunk
from vectapilot_api.evalset.corpus import Corpus
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.types import ApprovalConflict, StatusConflict, VersionConflict

FRESH_HINT = (
    "rerun the same command with --fresh (it deletes only this config's eval businesses'"
    " documents, then ingests again)"
)


class IngestRefused(Exception):
    """The corpus cannot be ingested into an existing namespace as it stands. Ids only."""


class StaleChunks(Exception):
    """The stored chunks are not the corpus at this config: `problems` says where (ids only)."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} stored-chunk problem(s); {FRESH_HINT}")
        self.problems = tuple(problems)


@dataclass(frozen=True)
class IngestCount:
    """What one business's ingest did."""

    business_id: str
    eval_business_id: str
    documents: int
    """Documents uploaded (all listed ones unless the run's stop rule cut the ingest short)."""
    created: int
    chunks_written: int
    retired_versions: int


async def ingest_corpus(
    kb: KbService,
    corpus: Corpus,
    mapping: Mapping[str, str],
    read_file: Callable[[str, str], bytes],
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> tuple[IngestCount, ...]:
    """Upload every listed document of each business in `mapping` into its eval business.

    `read_file(business_id, file name)` returns the file's bytes. `should_stop()` is asked before
    each document (the run's cap stop rule, set by a paid call, M1's "stop right after the call
    that tripped it"): once true, nothing more is uploaded and the counts so far are returned, the
    business in progress with the documents it uploaded. Raises `IngestRefused` when a stored
    version holds other bytes or another status (the corpus changed under an old version, or the
    namespace was filled differently), and lets every other error propagate (embedding, database,
    `evalrun.budget.CapReached`).
    """
    counts: list[IngestCount] = []
    for business_id, eval_id in mapping.items():
        if should_stop():
            break
        manifest = corpus.manifests[business_id]
        created = chunks = retired = documents = 0
        stopped = False
        for source in manifest.sources:
            if should_stop():
                stopped = True
                break
            data = read_file(business_id, source.file)
            refused: str | None = None
            try:
                result = await kb.ingest(eval_id, data, source.format, source.ingest_params())
            except (VersionConflict, StatusConflict, ApprovalConflict) as conflict:
                refused = type(conflict).__name__
            if refused is not None:  # outside the `except`: nothing chained
                raise IngestRefused(
                    f"{business_id}/{source.file} ({source.source_id} {source.version}): the eval"
                    f" business already stores this version differently ({refused}); {FRESH_HINT}"
                )
            documents += 1
            created += result.created
            chunks += result.chunks_written
            retired += result.retired_version is not None
        counts.append(
            IngestCount(
                business_id=business_id,
                eval_business_id=eval_id,
                documents=documents,
                created=created,
                chunks_written=chunks,
                retired_versions=retired,
            )
        )
        if stopped:
            break
    return tuple(counts)


def verify_stored(
    corpus: Corpus,
    business_id: str,
    config: ChunkingConfig,
    stored: Sequence[StoredChunk],
    embedding_model: str,
) -> list[str]:
    """Every way the business's stored chunks differ from its corpus at `config` (empty: none).
    Problems name the business, source and version, never text."""
    problems: list[str] = []
    by_document: dict[tuple[str, str], list[StoredChunk]] = {}
    for chunk in stored:
        by_document.setdefault((chunk.source_id, chunk.version), []).append(chunk)
    listed = {document.source.key for document in corpus.documents(business_id)}
    problems += [
        f"{business_id}: {source_id} {version} is stored but not in the corpus manifest"
        for source_id, version in sorted(by_document.keys() - listed)
    ]
    models = sorted({chunk.embedding_model for chunk in stored} - {embedding_model})
    if models:
        problems.append(
            f"{business_id}: {len(models)} other embedding model id(s) are stored next to"
            f" {embedding_model}"
        )
    for document in corpus.documents(business_id):
        source = document.source
        where = f"{business_id}: {source.source_id} {source.version}"
        rows = sorted(by_document.get(source.key, []), key=lambda chunk: chunk.chunk_index)
        if not rows:
            problems.append(f"{where} is not stored")
            continue
        if {(row.status, row.audience) for row in rows} != {(source.status, source.audience)}:
            problems.append(f"{where}: stored status or audience differs from the manifest")
        expected = [chunk.content for chunk in corpus.chunks(document, config)]
        if [row.content for row in rows] != expected or [row.chunk_index for row in rows] != list(
            range(len(expected))
        ):
            problems.append(
                f"{where}: stored chunks differ from the corpus chunked at max_chars"
                f" {config.max_chars}, overlap {config.overlap_chars}"
            )
    return problems
