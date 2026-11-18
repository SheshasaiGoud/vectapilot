"""A scriptable in-memory `Limiter` for tests (M7 PR 5; G9's test seam, `create_app(...,
limiter=...)`), as `FakeProvider` is for the gateway.

Unit tests run with a dead Redis (`tests/conftest.py`), so they never reach `RedisLimiter`. Each
operation follows a mode: `allow`, `refuse` (a full window, an exhausted budget) or `raise` (what
`RedisLimiter` raises when Redis is down: `GuardRefused(UNAVAILABLE)` with `ConnectionError`).
`limit` and `budget` each take one mode, or a sequence consumed one call at a time (then the last
mode repeats). Every call is recorded by operation and business, never by text.

It counts reservations in memory (`spent`), so a test can read what a turn reserved and settled,
but it is not a model of Redis: windows and the day's cap are the integration tests' job.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from vectapilot_api.guards.limits import GuardRefused, RefusalKind, Reserved
from vectapilot_api.guards.types import RouteSlug

Mode = Literal["allow", "refuse", "raise"]
REDIS_DOWN: Final = "ConnectionError"
"""The class name a `raise` mode reports, as `RedisLimiter` would for a refused connection."""


@dataclass
class _Script:
    modes: list[Mode]

    def next(self) -> Mode:
        return self.modes.pop(0) if len(self.modes) > 1 else self.modes[0]

    def peek(self) -> Mode:
        return self.modes[0]


def _script(modes: Mode | Sequence[Mode]) -> _Script:
    listed: list[Mode] = [modes] if isinstance(modes, str) else list(modes)
    if not listed:
        raise ValueError("a script has at least one mode")
    return _Script(listed)


@dataclass(frozen=True)
class LimiterCall:
    operation: Literal["admit", "reserve", "settle", "remaining"]
    business_id: str
    route: RouteSlug | None = None
    customered: bool = False
    """Whether `admit` named a customer (never the reference itself)."""


@dataclass
class FakeLimiter:
    """The fake (module docstring). `retry_after_s` is a refused limit's `Retry-After`;
    `remaining_micro_usd` what `remaining` answers while the budget allows."""

    limit: Mode | Sequence[Mode] = "allow"
    budget: Mode | Sequence[Mode] = "allow"
    retry_after_s: int = 30
    remaining_micro_usd: int = 500_000
    calls: list[LimiterCall] = field(default_factory=list)
    spent: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    """Micro-USD counted per reservation key (reserved, then adjusted by each settle)."""

    def __post_init__(self) -> None:
        self._limit = _script(self.limit)
        self._budget = _script(self.budget)

    def _unavailable(self) -> GuardRefused:
        return GuardRefused(RefusalKind.UNAVAILABLE, error_type=REDIS_DOWN)

    async def admit(
        self, business_id: str, route: RouteSlug, customer_ref: str | None = None
    ) -> None:
        self.calls.append(LimiterCall("admit", business_id, route, customer_ref is not None))
        mode = self._limit.next()
        if mode == "raise":
            raise self._unavailable()
        if mode == "refuse":
            raise GuardRefused(RefusalKind.LIMIT, retry_after_s=self.retry_after_s)

    async def reserve(self, business_id: str, amount_micro_usd: int) -> Reserved:
        self.calls.append(LimiterCall("reserve", business_id))
        mode = self._budget.next()
        if mode == "raise":
            raise self._unavailable()
        if mode == "refuse":
            raise GuardRefused(RefusalKind.BUDGET)
        key = f"fake:{business_id}"
        self.spent[key] += amount_micro_usd
        return Reserved(key=key, micro_usd=amount_micro_usd)

    async def settle(self, reserved: Reserved, spent_micro_usd: int) -> None:
        self.calls.append(LimiterCall("settle", reserved.key.removeprefix("fake:")))
        if self._budget.peek() == "raise":  # follows the budget's next mode, never consumes it
            raise self._unavailable()
        self.spent[reserved.key] += spent_micro_usd - reserved.micro_usd

    async def remaining(self, business_id: str) -> int:
        self.calls.append(LimiterCall("remaining", business_id))
        mode = self._budget.next()
        if mode == "raise":
            raise self._unavailable()
        return 0 if mode == "refuse" else self.remaining_micro_usd
