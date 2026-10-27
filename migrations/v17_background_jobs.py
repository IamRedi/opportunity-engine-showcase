"""Explicit additive S16 -> S17 background-job migration.

This module never opens the pilot database.  The rollout operator supplies a
verified SQLite/libSQL-compatible connection after taking a backup or branch.
"""
import background_job_schema


def migrate_connection(con) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT value FROM settings WHERE key='schema_version'"
        ).fetchone()
        if not row or str(row[0]) not in ("16", "17"):
            raise RuntimeError("Background-job migration requires schema 16 or 17.")
        for statement in background_job_schema.DDL:
            con.execute(statement)
        background_job_schema.ensure_additive(con)
        con.execute(
            """INSERT INTO settings(key,value) VALUES('schema_version',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(background_job_schema.SCHEMA_VERSION),),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
