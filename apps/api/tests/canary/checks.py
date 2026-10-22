"""The canary suite's decisions, without a database (ADR 0009; docs/plans/m2-pr-checks.md, PR 2).

Each function takes plain catalog facts (read from `pg_catalog` by `tests/canary/catalog.py`) or a
read path's output and returns problems as text; empty means safe. Keeping the decisions here, away
from the queries, lets tests/test_canary_checks.py prove without Postgres that each one turns red
(FORCE dropped, a key without `business_id`, an owner role, a leaked canary). The same functions
then run on the real catalog in CI (tests/canary/test_leakage.py).

Anything these checks do not understand is a problem, never a pass: fail closed.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

BUSINESS_COLUMN: Final = "business_id"

CURRENT_BUSINESS: Final = (
    "business_id = nullif(current_setting('app.business_id'::text, true), ''::text)"
)
"""ADR 0009's policy expression as Postgres prints it back (`pg_get_expr`), lower-cased."""

TABLE_KINDS: Final = frozenset({"r", "p"})  # pg_class.relkind: table, partitioned table
_TRUE_OPTIONS: Final = frozenset({"true", "on", "yes", "1"})


@dataclass(frozen=True)
class Relation:
    """A relation with a `business_id` column. `name` is `oid::regclass::text` (quoted)."""

    name: str
    kind: str
    rls_enabled: bool
    rls_forced: bool
    options: tuple[str, ...] = ()  # pg_class.reloptions, e.g. "security_invoker=true"


@dataclass(frozen=True)
class Policy:
    table: str
    name: str
    permissive: bool
    using: str | None
    with_check: str | None


@dataclass(frozen=True)
class KeyIndex:
    """A unique or exclusion index (both enforce a rule across every row, ignoring RLS).

    A partial one (`predicate`, its WHERE) is checked like any other: the predicate only narrows
    which rows take part, and the rows that do are still compared across every business.
    """

    table: str
    name: str
    primary: bool
    columns: tuple[str | None, ...]  # key columns in order; None = an expression
    first_type: str | None = None  # format_type() of the first key column
    first_default: str | None = None  # its default, as pg_get_expr prints it
    predicate: str | None = None  # a partial index's WHERE, as pg_get_expr prints it


@dataclass(frozen=True)
class ForeignKey:
    table: str
    name: str
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True)
class RoleFacts:
    superuser: bool
    bypassrls: bool
    createrole: bool
    member_of_privileged: bool  # can SET ROLE to a superuser or BYPASSRLS role
    owned_objects: int  # relations, schemas, functions, types, databases it (or a role it is a
    # member of) owns


@dataclass(frozen=True)
class Planted:
    """One fictional business and the canary string planted in its rows."""

    business_id: str
    canary: str


def relation_problems(relations: Iterable[Relation]) -> list[str]:
    """Every business table has RLS enabled and forced; views and the rest are flagged."""
    found = list(relations)
    if not found:
        return ["no relation has a business_id column: the catalog query found nothing to check"]
    problems: list[str] = []
    for relation in found:
        if relation.kind in TABLE_KINDS:
            if not relation.rls_enabled:
                problems.append(f"{relation.name}: row-level security is not enabled")
            if not relation.rls_forced:
                problems.append(
                    f"{relation.name}: row-level security is not forced (its owner skips it)"
                )
        elif relation.kind == "v":
            if not _security_invoker(relation.options):
                problems.append(
                    f"{relation.name}: view runs with its owner's rights; an owner that skips RLS"
                    " leaks through it (needs security_invoker = true)"
                )
        elif relation.kind == "m":
            problems.append(f"{relation.name}: materialized view, row-level security cannot apply")
        else:
            problems.append(
                f"{relation.name}: relation kind {relation.kind!r} is not covered by these checks"
            )
    return problems


def policy_problems(tables: Iterable[str], policies: Iterable[Policy]) -> list[str]:
    """Each table has a permissive policy, and every permissive one is exactly ADR 0009's.

    Permissive policies are OR'ed together, so one extra policy with a weaker rule (`OR true`, a
    different setting) opens the table; containment would accept that, so this compares exactly.
    A missing `WITH CHECK` is fine: Postgres then checks writes with `USING`.
    """
    by_table: dict[str, list[Policy]] = {}
    for policy in policies:
        by_table.setdefault(policy.table, []).append(policy)
    expected = _normal(CURRENT_BUSINESS)
    problems: list[str] = []
    for table in tables:
        permissive = [policy for policy in by_table.get(table, []) if policy.permissive]
        if not permissive:
            problems.append(f"{table}: no permissive policy (ADR 0009's policy is missing)")
        for policy in permissive:
            expressions = [e for e in (policy.using, policy.with_check) if e is not None]
            if not expressions or any(_normal(e) != expected for e in expressions):
                problems.append(
                    f"{table}: policy {policy.name} is not exactly"
                    f" `{CURRENT_BUSINESS}` (USING {policy.using!r}, WITH CHECK"
                    f" {policy.with_check!r})"
                )
    return problems


def key_problems(indexes: Iterable[KeyIndex]) -> list[str]:
    """Every unique or exclusion index on a business table starts with `business_id`.

    Postgres enforces these across all rows, ignoring RLS: without `business_id` first, an insert
    by one business fails when another's row exists, and the error tells it so. The one exception
    is a primary key on a single uuid column filled by `gen_random_uuid()`: a caller cannot
    collide with a value it cannot guess. That holds only while no API accepts a client-chosen id.
    A partial index (migration 0004's one approved version per source) follows the same rule; its
    predicate never excuses a missing `business_id`, nor makes the uuid exception apply.
    """
    problems: list[str] = []
    for index in indexes:
        if index.columns[:1] == (BUSINESS_COLUMN,) or _random_uuid_key(index):
            continue
        shown = ", ".join(column or "<expression>" for column in index.columns)
        where = "" if index.predicate is None else f" WHERE {index.predicate}"
        problems.append(
            f"{index.table}: unique/exclusion index {index.name} ({shown}){where} does not start"
            f" with {BUSINESS_COLUMN}, so its errors reveal other businesses' rows"
        )
    return problems


def foreign_key_problems(keys: Iterable[ForeignKey], business_tables: Iterable[str]) -> list[str]:
    """A foreign key into a business table pairs `business_id` with `business_id`.

    Foreign-key checks ignore RLS too: a key on `document_id` alone would let one business point at
    (and probe for) another's document. The composite key keeps both rows in one business.
    """
    targets = set(business_tables)
    problems: list[str] = []
    for key in keys:
        if key.referenced_table not in targets:
            continue
        if (BUSINESS_COLUMN, BUSINESS_COLUMN) not in zip(
            key.columns, key.referenced_columns, strict=True
        ):
            problems.append(
                f"{key.table}: foreign key {key.name} into {key.referenced_table} does not pair"
                f" {BUSINESS_COLUMN} with {BUSINESS_COLUMN}"
            )
    return problems


def app_role_problems(facts: RoleFacts | None) -> list[str]:
    """The app role cannot skip RLS, switch it off, or make itself a role that can."""
    if facts is None:
        return ["the app role does not exist"]
    problems: list[str] = []
    if facts.superuser:
        problems.append("is a superuser")
    if facts.bypassrls:
        problems.append("has BYPASSRLS")
    if facts.member_of_privileged:
        problems.append("is a member of a superuser or BYPASSRLS role")
    if facts.createrole:
        problems.append("has CREATEROLE")
    if facts.owned_objects:
        problems.append(f"owns {facts.owned_objects} object(s), or is a member of their owner")
    return problems


def canary_leaks(outputs: Mapping[str, str], mine: Planted, other: Planted) -> list[str]:
    """Acting as `mine`, no read path shows anything of `other`, and each shows `mine`.

    The second half is the positive control: a path that returns nothing at all would pass the
    first half for the wrong reason (unseeded table, broken query).
    """
    if not outputs:
        return ["no read path produced output: nothing was checked"]
    problems: list[str] = []
    for label, text in outputs.items():
        for marker in (other.business_id, other.canary):
            if marker in text:
                problems.append(f"{label}: shows {other.business_id}'s data ({marker})")
        if mine.business_id not in text and mine.canary not in text:
            problems.append(
                f"{label}: shows nothing of {mine.business_id} (seeded? the check is"
                " vacuous without it)"
            )
    return problems


def _security_invoker(options: Iterable[str]) -> bool:
    for option in options:
        key, _, value = option.partition("=")
        if key.strip().lower() == "security_invoker":
            return value.strip().lower() in _TRUE_OPTIONS
    return False


def _random_uuid_key(index: KeyIndex) -> bool:
    return (
        index.primary
        and index.predicate is None  # Postgres has no partial primary key: fail closed anyway
        and len(index.columns) == 1
        and index.first_type == "uuid"
        and index.first_default == "gen_random_uuid()"
    )


def _normal(expression: str) -> str:
    """Lower-cased, whitespace collapsed, redundant outer parentheses removed."""
    text = " ".join(expression.lower().split())
    while text.startswith("(") and _closes_at_end(text):
        text = text[1:-1].strip()
    return text


def _closes_at_end(text: str) -> bool:
    """Whether the "(" at index 0 is matched by the ")" at the very end (outside quotes)."""
    depth = 0
    quoted = False
    for index, char in enumerate(text):
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth == 0:
                return index == len(text) - 1
    return False
