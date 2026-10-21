"""Read the facts `checks.py` decides on, from `pg_catalog` (ADR 0009). No decisions here.

The business tables are found, never listed: every queryable relation outside the system schemas
with a live `business_id` column. `pg_catalog` (not `information_schema`, which hides objects the
current role has no privilege on) so nothing is missed whoever runs the query.

Each loader takes an open connection, so a test can change the schema inside a transaction, read
the catalog in that same transaction, and roll the change back (Postgres DDL is transactional).
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.canary.checks import ForeignKey, KeyIndex, Policy, Relation, RoleFacts

# `pg_*` schemas (catalog, toast, temporary) and information_schema are Postgres', not ours.
# relkind: r table, p partitioned table, v view, m materialized view, f foreign table.
_HAS_BUSINESS_ID: Final = """
    c.relkind IN ('r', 'p', 'v', 'm', 'f')
    AND NOT starts_with(n.nspname, 'pg_') AND n.nspname <> 'information_schema'
    AND EXISTS (SELECT 1 FROM pg_catalog.pg_attribute a
                WHERE a.attrelid = c.oid AND a.attname = 'business_id'
                  AND a.attnum > 0 AND NOT a.attisdropped)
"""

_RELATIONS: Final = text(
    f"""
    SELECT c.oid::regclass::text AS name, c.relkind::text AS kind,
           c.relrowsecurity, c.relforcerowsecurity,
           coalesce(c.reloptions, '{{}}')::text[] AS options
    FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    WHERE {_HAS_BUSINESS_ID}
    ORDER BY 1
    """  # noqa: S608 (a constant, no input)
)

_POLICIES: Final = text(
    """
    SELECT p.polrelid::regclass::text AS table_name, p.polname::text AS name, p.polpermissive,
           pg_get_expr(p.polqual, p.polrelid) AS using_expr,
           pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
    FROM pg_catalog.pg_policy p
    ORDER BY 1, 2
    """
)

# Key columns only (`indnkeyatts`): INCLUDE columns take no part in uniqueness. attnum 0 is an
# expression, so the LEFT JOIN yields NULL for it.
_KEY_INDEXES: Final = text(
    f"""
    SELECT c.oid::regclass::text AS table_name, i.indexrelid::regclass::text AS name,
           i.indisprimary,
           array(SELECT a.attname::text
                 FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                 LEFT JOIN pg_catalog.pg_attribute a
                        ON a.attrelid = c.oid AND a.attnum = k.attnum
                 WHERE k.ord <= i.indnkeyatts
                 ORDER BY k.ord) AS columns,
           format_type(k1.atttypid, k1.atttypmod) AS first_type,
           pg_get_expr(d.adbin, d.adrelid) AS first_default
    FROM pg_catalog.pg_index i
    JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_catalog.pg_attribute k1
           ON k1.attrelid = c.oid AND k1.attnum = i.indkey[0]
    LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = i.indkey[0]
    WHERE (i.indisunique OR i.indisexclusion) AND {_HAS_BUSINESS_ID}
    ORDER BY 1, 2
    """  # noqa: S608 (a constant, no input)
)

_FOREIGN_KEYS: Final = text(
    """
    SELECT k.conrelid::regclass::text AS table_name, k.conname::text AS name,
           array(SELECT a.attname::text FROM unnest(k.conkey) WITH ORDINALITY AS u(attnum, ord)
                 JOIN pg_catalog.pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.attnum
                 ORDER BY u.ord) AS columns,
           k.confrelid::regclass::text AS referenced_table,
           array(SELECT a.attname::text FROM unnest(k.confkey) WITH ORDINALITY AS u(attnum, ord)
                 JOIN pg_catalog.pg_attribute a ON a.attrelid = k.confrelid AND a.attnum = u.attnum
                 ORDER BY u.ord) AS referenced_columns
    FROM pg_catalog.pg_constraint k
    WHERE k.contype = 'f'
    ORDER BY 1, 2
    """
)

# `pg_has_role(..., 'MEMBER')`: the role itself, or any role it could SET ROLE to (as db.py).
_ROLE_FACTS: Final = text(
    """
    SELECT r.rolsuper, r.rolbypassrls, r.rolcreaterole,
           EXISTS (SELECT 1 FROM pg_catalog.pg_roles p
                   WHERE (p.rolsuper OR p.rolbypassrls) AND p.oid <> r.oid
                     AND pg_has_role(r.oid, p.oid, 'MEMBER')) AS member_of_privileged,
           (SELECT count(*) FROM pg_catalog.pg_class o
              WHERE pg_has_role(r.oid, o.relowner, 'MEMBER'))
           + (SELECT count(*) FROM pg_catalog.pg_namespace o
              WHERE pg_has_role(r.oid, o.nspowner, 'MEMBER'))
           + (SELECT count(*) FROM pg_catalog.pg_proc o
              WHERE pg_has_role(r.oid, o.proowner, 'MEMBER'))
           + (SELECT count(*) FROM pg_catalog.pg_type o
              WHERE pg_has_role(r.oid, o.typowner, 'MEMBER'))
           + (SELECT count(*) FROM pg_catalog.pg_database o
              WHERE pg_has_role(r.oid, o.datdba, 'MEMBER')) AS owned_objects
    FROM pg_catalog.pg_roles r
    WHERE r.rolname = :role
    """
)


async def load_relations(connection: AsyncConnection) -> list[Relation]:
    rows = (await connection.execute(_RELATIONS)).all()
    return [
        Relation(r.name, r.kind, r.relrowsecurity, r.relforcerowsecurity, tuple(r.options))
        for r in rows
    ]


async def business_tables(connection: AsyncConnection) -> list[str]:
    """The names (quoted, ready for SQL) of the business tables that hold rows."""
    return [r.name for r in await load_relations(connection) if r.kind in ("r", "p")]


async def load_policies(connection: AsyncConnection) -> list[Policy]:
    rows = (await connection.execute(_POLICIES)).all()
    return [Policy(r.table_name, r.name, r.polpermissive, r.using_expr, r.check_expr) for r in rows]


async def load_key_indexes(connection: AsyncConnection) -> list[KeyIndex]:
    rows = (await connection.execute(_KEY_INDEXES)).all()
    return [
        KeyIndex(
            r.table_name, r.name, r.indisprimary, tuple(r.columns), r.first_type, r.first_default
        )
        for r in rows
    ]


async def load_foreign_keys(connection: AsyncConnection) -> list[ForeignKey]:
    rows = (await connection.execute(_FOREIGN_KEYS)).all()
    return [
        ForeignKey(
            r.table_name, r.name, tuple(r.columns), r.referenced_table, tuple(r.referenced_columns)
        )
        for r in rows
    ]


async def load_role_facts(connection: AsyncConnection, role: str) -> RoleFacts | None:
    row = (await connection.execute(_ROLE_FACTS, {"role": role})).one_or_none()
    if row is None:
        return None
    return RoleFacts(
        row.rolsuper,
        row.rolbypassrls,
        row.rolcreaterole,
        row.member_of_privileged,
        int(row.owned_objects),
    )
