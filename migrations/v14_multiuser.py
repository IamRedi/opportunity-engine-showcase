"""Multi-user + tenant foundation (Faza 8B).

Kontrata e pronësisë (spec §Data isolation):

- **users** — identitet, hash fjalëkalimi, status
- **tenants** — brand/config/white-label
- **tenant_memberships** — user↔tenant me role (owner|admin|member)
- të gjitha entitetet user-owned kanë `user_id` + opsionalisht `tenant_id`
- mundësitë raw (source listings) janë të ndara nga user-state:
  - `opportunities` ruan të dhënat kanonike (shared/derived)
  - `opportunity_user_state` ruan saved/notes/status NË NIVEL USER-I
  - `lenses` janë derived nga përmbajtja — pa user-scope

Migrimi v14:
1. krijon users/tenants/tenant_memberships
2. krijon default tenant + owner user nga profili ekzistues
3. i cakton të gjitha zërat ekzistues owner-it
4. shtON user_id në çdo tabelë user-owned
5. idempotent: ekzekutimi i dytë s'bën asgjë
"""
import repository as repo
import sqlite3
import json
import hashlib
import secrets
from pathlib import Path


def _hash_pw(pw: str) -> str:
    from werkzeug.security import generate_password_hash
    return generate_password_hash(pw)


V14_DDL = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL DEFAULT '',
        display_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_login_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'active',
        branding TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tenant_memberships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role TEXT NOT NULL DEFAULT 'member',
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id, user_id)
    )""",
]

# tabelat user-owned që duhen user_id
USER_OWNED_TABLES = [
    "opportunities", "profile_sections", "profile_cv", "cv_suggestions",
    "profile_gaps", "profile_suggestions", "applications", "application_versions",
    "schedule_configs", "notifications", "search_profiles", "search_runs",
    "email_deliveries", "digest_runs", "ai_cache", "eval_runs", "eval_items",
    "status_events", "opportunity_sources", "products",
]


def migrate_v14(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # 1) tabela e re
        for stmt in V14_DDL:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # already exists (idempotent)
        # 2) user_id columns (guarded)
        for table in USER_OWNED_TABLES:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in cols:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                except sqlite3.OperationalError:
                    pass
        # 3) default tenant
        con.execute("""INSERT OR IGNORE INTO tenants (id, name, slug, status, branding, created_at, updated_at)
                       VALUES (1, 'Default', 'default', 'active', '{}', datetime('now'), datetime('now'))""")
        # 4) owner user (nga profili ekzistues)
        prof = con.execute("SELECT name, email FROM profile WHERE id=1").fetchone()
        email = (prof["email"] or "owner@local").strip() if prof else "owner@local"
        name = (prof["name"] or "Owner").strip() if prof else "Owner"
        import os
        pw_hash = _hash_pw(os.environ.get("OWNER_PASSWORD", "changeme"))
        con.execute("""INSERT OR IGNORE INTO users (id, email, username, password_hash,
                       display_name, status, created_at, updated_at)
                       VALUES (1, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))""",
                    (email, email.split("@")[0], pw_hash, name))
        con.execute("""INSERT OR IGNORE INTO tenant_memberships (tenant_id, user_id, role, created_at)
                       VALUES (1, 1, 'owner', datetime('now'))""")
        # 5) cakto user_id=1 për të gjitha zërat ekzistues
        for table in USER_OWNED_TABLES:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if "user_id" in cols:
                con.execute(f"UPDATE {table} SET user_id=1 WHERE user_id IS NULL")
        con.commit()
    finally:
        con.close()
