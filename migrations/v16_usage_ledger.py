"""Explicit append-only S15 -> S16 usage-ledger migration.

This module does not connect to the pilot database by itself. A caller must supply
an isolated SQLite/libSQL-compatible connection after its own verified backup.
"""
import usage_schema


def migrate_connection(con) -> None:
    con.execute('BEGIN IMMEDIATE')
    try:
        row=con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        if not row or str(row[0]) not in ('15','16'):
            raise RuntimeError('Usage ledger migration requires schema 15 or 16.')
        for statement in usage_schema.DDL:
            con.execute(statement)
        con.execute("""INSERT INTO settings(key,value) VALUES('schema_version',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(str(usage_schema.SCHEMA_VERSION),))
        con.commit()
    except Exception:
        con.rollback()
        raise
