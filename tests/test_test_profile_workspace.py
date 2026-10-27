import sqlite3

import config
from scripts import prepare_test_profile


def test_empty_workspace_preserves_original_and_resumes_without_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare_test_profile, "ROOT", tmp_path)
    monkeypatch.setattr(config, "APP_ENV", "local")
    target = tmp_path / "tmp/cv-test-profile/jobs.db"
    monkeypatch.setattr(config, "DB_PATH", target)
    source = tmp_path / "jobs.db"
    with sqlite3.connect(source) as con:
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)")
        con.executemany("INSERT INTO settings VALUES (?,?)", [
            ("ai_provider", "openai-compatible"),
            ("adzuna_app_key", "test-key"),
            ("adzuna_what", "old search"),
            ("email_to", "old@example.test"),
        ])
    original = source.read_bytes()
    prepare_test_profile.main()
    with sqlite3.connect(target) as con:
        assert con.execute("SELECT name FROM profile WHERE user_id=1").fetchone()[0] == ""
        assert con.execute("SELECT count(*) FROM profile_cv").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM opportunities").fetchone()[0] == 0
        assert con.execute("SELECT value FROM settings WHERE key='adzuna_app_key'").fetchone()[0] == "test-key"
        assert not con.execute("SELECT key FROM settings WHERE key IN ('email_to','adzuna_what')").fetchall()
        con.execute("UPDATE profile SET name='New CV owner' WHERE user_id=1")
    prepare_test_profile.main()
    with sqlite3.connect(target) as con:
        assert con.execute("SELECT name FROM profile WHERE user_id=1").fetchone()[0] == "New CV owner"
    assert source.read_bytes() == original
