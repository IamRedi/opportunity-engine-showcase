"""Migrimi legacy `jobs` → `opportunities` (spec §12, DoD §34.1).

Skema legacy riprodhohet saktësisht si në DB-në reale para Fazës 1.
"""
import json
import sqlite3
from pathlib import Path

import pytest

import repository as repo

LEGACY_DDL = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER NOT NULL DEFAULT 0,
    salary_text TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    score INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'e re',
    notes TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'job',
    posted_at TEXT NOT NULL DEFAULT '',
    salary_min INTEGER,
    salary_max INTEGER,
    opportunity_type TEXT NOT NULL DEFAULT ''
);
"""

ROWS = [
    # (id, source, url, title, company, status, kind, notes, remote, score, opp_type, salary)
    (1, "arbeitnow", "https://www.arbeitnow.com/jobs/dev-1", "Dev One", "GmbH A",
     "e re", "job", "", 1, 80, "", ""),
    (2, "arbeitnow", "https://www.arbeitnow.com/jobs/dev-2", "Dev Two", "GmbH B",
     "e ruajtur", "job", "shënim i rëndësishëm", 0, 55, "", "50.000–60.000 €/vit"),
    (3, "remotive", "https://remotive.com/remote-jobs/x-3", "Remote Lead", "Corp C",
     "aplikuar", "job", "aplikova 01.09", 1, 70, "", ""),
    (4, "remotive", "https://remotive.com/remote-jobs/x-4", "Ops", "Corp D",
     "intervistë", "job", "", 1, 40, "", ""),
    (5, "jobicy", "https://jobicy.com/jobs/5-manager", "Manager", "E5",
     "ofertë", "job", "", 0, 90, "", ""),
    (6, "jobicy", "https://jobicy.com/jobs/6-analyst", "Analyst", "E6",
     "refuzuar", "job", "", 0, 30, "", ""),
    (7, "manual", "https://example.com/7", "Old Op", "Client G",
     "lead", "opportunity", "lead ngaLinkedIn", 1, 20, "Website", "2.000–5.000 €"),
    (8, "manual", "https://example.com/8", "Second Op", "Client H",
     "kontaktuar", "opportunity", "", 0, 10, "SaaS", ""),
    (9, "manual", "https://example.com/9", "Third", "Client I",
     "propozim", "opportunity", "", 0, 0, "Automation", ""),
    (10, "manual", "https://example.com/10", "Fourth", "Client J",
     "fituar", "opportunity", "fituam", 0, 0, "White-label", ""),
    (11, "manual", "https://example.com/11", "Fifth", "Client K",
     "humbur", "opportunity", "", 0, 0, "", ""),
    (12, "manual", "https://example.com/12", "Sixth", "Client L",
     "arkivuar", "job", "", 0, 0, "", ""),
]


def _make_legacy_db(path: Path):
    con = sqlite3.connect(path)
    con.execute(LEGACY_DDL)
    for (i, source, url, title, company, status, kind, notes, remote, score, opp, sal) in ROWS:
        con.execute(
            """INSERT INTO jobs (id, source, url, title, company, location, remote,
               salary_text, description, tags, score, excluded, status, notes,
               first_seen, last_seen, kind, posted_at, salary_min, salary_max,
               opportunity_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (i, source, url, title, company, "Berlin", remote, sal,
             f"Përshkrimi për {title}", json.dumps(["python", "sql"]), score,
             0, status, notes, "2026-09-01 10:00", "2026-09-02 11:00", kind,
             "2026-08-30 08:00", 50000 if i == 2 else None, None, opp),
        )
    con.commit()
    con.close()


EXPECTED_STATUS = {
    1: "new", 2: "saved", 3: "applied", 4: "interview", 5: "offer",
    6: "rejected", 7: "lead_saved", 8: "contacted", 9: "proposal_sent",
    10: "won", 11: "lost", 12: "archived",
}


def test_migration_preserves_everything(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    _make_legacy_db(db)
    info = repo.init(db)
    assert info["migrated"] is True
    assert info["rows"] == len(ROWS)
    assert Path(info["backup"]).exists()  # backup file para migrimit

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # tabela legacy nuk ekziston më
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='jobs'").fetchone()[0] == 0
    rows = con.execute("SELECT * FROM opportunities ORDER BY id").fetchall()
    assert len(rows) == len(ROWS)
    by_id = {r["id"]: dict(r) for r in rows}
    for (i, source, url, title, company, status, kind, notes, *_rest) in ROWS:
        o = by_id[i]
        assert o["title"] == title
        assert o["org_name"] == company
        assert o["notes"] == notes
        assert o["source"] == source
        assert o["source_url"] == url
        assert o["status"] == EXPECTED_STATUS[i]
        assert o["kind"] == ("freelance" if kind == "opportunity" else kind)
        assert o["discovered_at"] == "2026-09-01 10:00"
        assert o["updated_at"] == "2026-09-02 11:00"
        assert o["posted_at"] == "2026-08-30 08:00"
        assert o["rule_score"] == o["final_score"]
        assert o["dedup_key"] == repo.canonical_url(url)
    assert by_id[2]["salary_text"] == "50.000–60.000 €/vit"
    assert by_id[7]["opportunity_type"] == "Website"
    # saved_at vetëm për të ruajturat
    assert by_id[2]["saved_at"] == "2026-09-01 10:00"
    assert by_id[1]["saved_at"] is None
    # një rresht opportunity_sources për çdo rekord
    n_sources = con.execute("SELECT COUNT(*) FROM opportunity_sources").fetchone()[0]
    assert n_sources == len(ROWS)
    # migrimi legacy të çon deri te skema aktuale (v2+: profilet e kërkimit)
    assert con.execute(
        "SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == str(repo.SCHEMA_VERSION)
    v2_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"search_profiles", "search_runs"} <= v2_tables
    # remote_type i migruar
    assert by_id[1]["remote_type"] == "remote"
    assert by_id[2]["remote_type"] == ""
    con.close()


def test_migration_merges_canonical_duplicates(tmp_path):
    db = tmp_path / "legacy-dup.sqlite3"
    _make_legacy_db(db)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO jobs (source, url, title, company, status, notes, first_seen, last_seen, kind)"
        " VALUES ('remotive', 'https://remotive.com/remote-jobs/x-3?utm_source=mail',"
        " 'Remote Lead (dup)', 'Corp C', 'e re', '', '2026-09-03 09:00', '2026-09-03 09:00', 'job')")
    con.commit()
    con.close()
    info = repo.init(db)
    assert info["rows"] == len(ROWS) + 1
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    # duplikati kanonik (utm_source) mergesohet: 13 rreshta legacy → 12 mundësi
    assert n == len(ROWS)
    # ...dhe 13 rreshte në opportunity_sources (metadatura e dytit nuk humbet)
    ns = con.execute("SELECT COUNT(*) FROM opportunity_sources").fetchone()[0]
    assert ns == len(ROWS) + 1
    con.close()


def test_migration_idempotent(tmp_path):
    db = tmp_path / "legacy2.sqlite3"
    _make_legacy_db(db)
    repo.init(db)
    first = repo.init(db)  # ekzekutimi i dytë s'bën asgjë
    assert first["migrated"] is False
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == len(ROWS)
    con.close()


def test_fresh_install_creates_v1(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    info = repo.init(db)
    assert info["migrated"] is False
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"opportunities", "opportunity_sources", "status_events", "opportunities_fts"} <= names
    con.close()
