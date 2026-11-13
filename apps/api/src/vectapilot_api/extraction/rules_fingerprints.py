"""Each `EXTRACTION_RULES_VERSION`'s fingerprint (plan, "Scoring rules"; M6 PR 2).

The rules that turn a proposal into levels and statuses live in three files: `signals.py` (the
facts), `routing.py` (H1, the decisions) and `invariants.py` (the check after H1). An eval run's
fingerprint names the rules version, so a change to any of the three without a new version would
let two different rule sets share one name. tests/extraction/test_rules_version.py computes the
fingerprint (`rules_fingerprint`) and fails CI until the current version's entry matches, as
`PROMPT_FINGERPRINTS` does for a prompt.

This map lives in its own file, outside what it fingerprints, so adding an entry never changes
the fingerprint. Keep old entries.
"""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

RULES_FILES: Final = ("signals.py", "routing.py", "invariants.py")
"""The files the fingerprint covers, in this order."""

EXTRACTION_RULES_FINGERPRINTS: Final[Mapping[str, str]] = {
    "extraction-rules.1": "2ffaa3c976b8183d9e954aa9b0e8056bd9e30c4ac564a45b84cebda465888b5b",
    # M6 PR 3 review: `plain` drops control characters (NUL never reaches a stored value).
    "extraction-rules.2": "5c2bed12e0aa73ab3febe241c55b58a2b925c4647b942be56f3c63ed1f9b528d",
}


def rules_fingerprint(folder: Path | None = None) -> str:
    """The sha256 over the rules files' text (line ends normalised), each length-prefixed."""
    base = Path(__file__).parent if folder is None else folder
    digest = hashlib.sha256()
    for name in RULES_FILES:
        data = (base / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()
