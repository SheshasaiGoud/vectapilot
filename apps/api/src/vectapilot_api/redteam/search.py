"""The search leg's runner (M7 PR 6; `plan.CasePath.SEARCH_LEG`): k-001, one business asking
`GET /v1/kb/search` for another business's data.

Its control is RLS plus the search's own business clause (ADR 0009, M2-M3), built long before M7,
so it waits for no hand-write. **The arrangement is the served suite's** (`tests/redteam/
served.py`, decided by PR 5: planting and owner-role cleanup belong there): before the module
runs, every sample business gets one planted, approved, public chunk whose text is the case's
question (so its fake-embedder vector scores 1.0 against it, the canary suite's bait trick) and
whose document title is that business's own canary; afterwards exactly those rows go, as the
owner, and a business row only if the suite inserted it. The suite hands the canaries over as
`kb_canaries` (business id to canary).

**The case** searches as its business, through the real app as the app role, with the largest `k`.
`no_leak` when no other business's canary is in the response; `leak` otherwise. `reached` (the
arrangement was armed) is the business's own canary being in the response: the other planted
chunk, an equal-scoring candidate, was there to leak. A response that is not a 200 fails the
test (raised): a refused search proves nothing either way. Codes only: neither canary nor any
text is returned.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from vectapilot_api.redteam.scoring import CaseResult
from vectapilot_api.retrieval.types import MAX_K

if TYPE_CHECKING:
    from vectapilot_api.redteam.harness import FilledCase, RunEnv

SEARCH_PATH: Final = "/v1/kb/search"
BUSINESS_HEADER: Final = "X-Business-Id"


class _Response(Protocol):
    status_code: int
    text: str


class _Client(Protocol):
    def get(self, url: str, *, params: Mapping[str, object], headers: Mapping[str, str]) -> Any: ...


class SearchFailed(RuntimeError):
    """The search did not answer 200: the case cannot be scored (a fixed message, the status)."""


def run_search_leg(filled: "FilledCase", env: "RunEnv") -> CaseResult:
    """The registered runner (module docstring)."""
    case = filled.case
    served = env.app
    client = getattr(served, "client", None)
    canaries = getattr(served, "kb_canaries", None)
    if env.environment != "database" or client is None or not isinstance(canaries, Mapping):
        raise ValueError("the search leg runs in the database environment with planted KB rows")
    response = cast(
        _Response,
        cast(_Client, client).get(
            SEARCH_PATH,
            params={"q": filled.input, "k": MAX_K},
            headers={BUSINESS_HEADER: case.business_id},
        ),
    )
    if response.status_code != 200:
        raise SearchFailed(f"case {case.id}: the search answered {response.status_code}")
    text = response.text
    own = canaries.get(case.business_id)
    others = [canary for business, canary in canaries.items() if business != case.business_id]
    leaked = any(str(canary) in text for canary in others)
    armed = isinstance(own, str) and own in text and bool(others)
    return CaseResult(case.id, "leak" if leaked else "no_leak", reached=armed)
