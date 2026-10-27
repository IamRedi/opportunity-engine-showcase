"""Faza 8A: konfig environments, auth boundary, storage, backup/restore, health."""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import config
import repository as repo
import storage as storage_mod

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------- config / fail-fast

def test_local_env_default_no_secrets_required():
    # APP_ENV lokal → pa sekrete të detyrueshme
    assert config.APP_ENV in ("local", "test", "production")


def test_production_fail_fast_missing_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "")
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD_SHA256", raising=False)
    errors = config.validate_production()
    assert any("SECRET_KEY" in e for e in errors)
    assert any("AUTH_USERNAME" in e for e in errors)


def test_production_fail_fast_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("AUTH_USERNAME", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", "pw")
    errors = config.validate_production(db_path=tmp_path / "missing.db")
    assert any("DB" in e for e in errors)


def test_production_ok_when_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("AUTH_USERNAME", "admin")
    monkeypatch.setenv("AUTH_PASSWORD", "pw")
    dbf = tmp_path / "jobs.db"
    con = sqlite3.connect(dbf)
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
    con.commit(); con.close()
    monkeypatch.setattr(config, "DB_PATH", dbf)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert config.validate_production() == []


# ------------------------------------------------- storage provider

def test_local_workspaces_isolate_same_cv_and_portrait_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_ENV", "local")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "original")
    original = storage_mod.get_storage()
    for key in ("cv-same.pdf", "portraits/1/same.jpg"):
        original.save(key, b"original")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "test-profile")
    other = storage_mod.get_storage()
    for key in ("cv-same.pdf", "portraits/1/same.jpg"):
        assert not other.exists(key)
        other.save(key, b"test")
        other.delete(key)
        assert original.open(key) == b"original"


def test_storage_local_provider_roundtrip(tmp_path):
    st = storage_mod.LocalStorage(tmp_path / "uploads")
    st.save("cv-abc.pdf", b"data")
    assert st.exists("cv-abc.pdf")
    assert st.open("cv-abc.pdf") == b"data"
    meta = st.metadata("cv-abc.pdf")
    assert meta["exists"] and meta["size"] == 4
    assert st.delete("cv-abc.pdf") is True
    assert st.exists("cv-abc.pdf") is False


def test_storage_path_traversal_rejected(tmp_path):
    st = storage_mod.LocalStorage(tmp_path / "uploads")
    for bad in ("../secret.txt", "a/../../b", "a:b"):
        with pytest.raises(ValueError):
            st.save(bad, b"x")
    # absolute → ruhet BRENDA base (pa arritje jashtë)
    st.save("/etc/passwd", b"x")
    resolved = st.metadata("/etc/passwd")["path"]
    assert str(tmp_path) in resolved
    st.delete("/etc/passwd")


# ------------------------------------------------- backup → restore drill

def test_backup_restore_drill_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "source.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "source-data")
    repo.init()
    repo.upsert_opportunity({"url": "https://example.test/backup-job", "title": "Backup test"})
    storage_mod.get_storage().save("cv-test.txt", b"isolated CV")
    from scripts.backup import main as backup_main
    out = tmp_path / "backups"
    backup_main(out)
    backup_dir = sorted(out.iterdir())[-1]
    # restore në target të izoluar
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    restore = importlib.import_module("restore") if (ROOT / "scripts" / "restore.py").exists() else None
    assert restore is not None
    target = tmp_path / "restored"
    restore.main(backup_dir, target)
    # verifikim
    restored_db = target / "jobs.db"
    con = sqlite3.connect(restored_db)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] > 0
    con.close()
    assert (target / "uploads" / "cv-test.txt").read_bytes() == b"isolated CV"
    # manifest i verifikueshëm
    manifest = json.loads((backup_dir / "manifest.json").read_text())
    assert manifest["files"]


def test_restore_never_touches_live(tmp_path):
    from scripts.restore import main as restore_main
    live_before = Path(config.DB_PATH).read_bytes()
    backup_dir = tmp_path / "b"
    backup_dir.mkdir()
    # backup minimal i pavlefshëm → drill duhet të dështojë pa prekur live
    (backup_dir / "manifest.json").write_text("{}")
    with pytest.raises(SystemExit):
        restore_main(backup_dir, tmp_path / "restored")
    assert Path(config.DB_PATH).read_bytes() == live_before


# ------------------------------------------------- scheduler me të njëjtin config

def test_scheduler_once_same_config(tmp_path, monkeypatch):
    """scheduler_loop --once me të njëjtin DB përmes env → ekzekutohet pa gabim."""
    env = dict(os.environ)
    env["OPPORTUNITY_DB"] = str(config.DB_PATH)
    env["APP_ENV"] = "local"
    res = subprocess.run(
        [sys.executable, str(ROOT / "scheduler_loop.py"), "--once"],
        capture_output=True, text=True, env=env, timeout=120)
    assert res.returncode == 0
    assert "due=" in res.stdout


# ------------------------------------------------- sqlite compatibitet

def test_search_works_without_fts(monkeypatch):
    import matching
    _seed = lambda url, title: repo.upsert_opportunity({
        "source": "pc", "url": url, "title": title, "company": "C",
        "location": "B", "remote": False, "description": "python backend",
        "tags": [], "kind": "job"})
    _seed("https://pc.example/fts1", "FastAPI Specialist")
    # FTS jo-disponueshëm → fallback LIKE
    monkeypatch.setattr(__import__("repository").__name__ and __import__("repository"), "_fts_available", lambda: False, raising=True) if False else None
    import repository as r
    old = r._fts_available
    r._fts_available = lambda: False
    try:
        rows = repo.list_opportunities(q="FastAPI")
        assert any("FastAPI" in x["title"] for x in rows)
    finally:
        r._fts_available = old


def test_no_sqlite_only_pragmas_in_business_logic():
    # pragma-t lejohen VETËM te repository (adapter) — jo te services/orchestrator
    import pathlib
    for fname in ("orchestrator.py", "scheduler.py", "product_fit.py",
                  "classifier.py", "cv_merge.py", "cv_parser.py"):
        src = (ROOT / fname).read_text()
        assert "sqlite3" not in src, f"{fname} varet nga sqlite direkt"
        assert "PRAGMA" not in src, f"{fname} ka PRAGMA (SQLite-specifik)"
