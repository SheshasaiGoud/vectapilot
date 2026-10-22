"""Eval businesses: where a run's corpus lives in the database (M2 PR 8; decided 2026-10-22).

**Why not the dataset's own business ids.** Re-ingesting a stored `(source_id, version)` does not
re-chunk it (PR 5: same bytes, no work), and a business's chunks must all carry one embedding model
(ADR 0008). So a run at another chunk size, or with the real embedder after a dry run with the
fake one, would silently search the old chunks (or be refused as `reindex_required`). Deleting the
old chunks first would work, but a delete is the one step a mistake can make expensive.

**The rule.** Each run stores a dataset business's corpus under a derived id:

    <business_id>-ev-<first 10 hex of sha256(canonical JSON of business, embedding model,
                                             max_chars, overlap_chars, salt)>

so each (business, embedding model, chunk config) gets its own registered business, ingested
once, never deleted by the harness unless asked (`--fresh`, which deletes only ids derived
here). The id is a `BusinessId` (at most 40 characters), so a dataset business id of more than 26
characters is refused. Nothing is hard-coded: the ids come from the dataset manifest. `salt` is
for tests (each test gets its own namespace); the CLI never sets it.

The report keeps the dataset's business ids everywhere and lists the mapping in run.json. A hit's
business is read from its document row (an eval id) and mapped back with `dataset_business`; an id
that is not one of this run's eval ids maps to itself, so it can never equal a question's business
(the leak check, `scoring.py`).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Final

from vectapilot_api.db import InvalidBusinessId, parse_business_id
from vectapilot_api.ingestion.types import ChunkingConfig

EVAL_MARKER: Final = "-ev-"
DIGEST_CHARS: Final = 10
EVAL_ID_FORM: Final = re.compile(rf"^[a-z0-9-]+{re.escape(EVAL_MARKER)}[0-9a-f]{{{DIGEST_CHARS}}}$")
"""The form of every id `eval_business_id` derives; `--fresh` deletes nothing else."""


def is_eval_business_id(business_id: str) -> bool:
    """Whether `business_id` has the derived form (`<business>-ev-<10 hex>`)."""
    return EVAL_ID_FORM.fullmatch(business_id) is not None


class NamespaceError(ValueError):
    """A dataset business id cannot carry an eval namespace (too long for a `BusinessId`)."""


def eval_business_id(
    business_id: str, *, embedding_model: str, chunking: ChunkingConfig, salt: str = ""
) -> str:
    """The derived id for `business_id` at this model and chunk config (module docstring)."""
    payload = {
        "business_id": business_id,
        "embedding_model": embedding_model,
        "max_chars": chunking.max_chars,
        "overlap_chars": chunking.overlap_chars,
        "salt": salt,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
    try:
        return parse_business_id(f"{business_id}{EVAL_MARKER}{digest}")
    except InvalidBusinessId:
        raise NamespaceError(
            f"business {business_id} is too long for an eval namespace (a business id has at most"
            f" 40 characters, and the namespace adds {len(EVAL_MARKER) + DIGEST_CHARS})"
        ) from None


def namespaces(
    business_ids: Iterable[str], *, embedding_model: str, chunking: ChunkingConfig, salt: str = ""
) -> dict[str, str]:
    """{dataset business id: eval business id}, in the given order."""
    return {
        business: eval_business_id(
            business, embedding_model=embedding_model, chunking=chunking, salt=salt
        )
        for business in business_ids
    }


def dataset_business(row_business_id: str, mapping: Mapping[str, str]) -> str:
    """The dataset business a stored row belongs to: the inverse of `mapping`, or the row's own id
    when it is not one of this run's eval ids (so it matches no question's business)."""
    inverse = {eval_id: business for business, eval_id in mapping.items()}
    return inverse.get(row_business_id, row_business_id)
