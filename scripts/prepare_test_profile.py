"""Create an empty local profile with provider configuration only.

Existing test data is preserved on restart. Never copy candidate data or jobs.
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
import repository as repo


def main():
    source = ROOT / "jobs.db"
    target = config.DB_PATH.resolve()
    expected = ROOT / "tmp" / "cv-test-profile" / "jobs.db"
    if config.APP_ENV != "local" or target != expected.resolve():
        raise SystemExit("Use run-test-profile.sh to select the isolated test database.")
    os.umask(0o077)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        print("Existing test profile preserved.")
        return
    repo.init()
    allowed = (
        "ai_provider", "ai_model", "ai_base_url", "ai_timeout", "ai_retries",
        "ai_max_input_chars", "adzuna_app_id", "adzuna_app_key",
    )
    if source.exists():
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            rows = src.execute(
                "SELECT key,value FROM settings WHERE key IN ("
                + ",".join("?" for _ in allowed) + ")", allowed
            ).fetchall()
        with sqlite3.connect(target) as dst:
            dst.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", rows)
    print("Empty test profile ready. Original profile preserved.")


if __name__ == "__main__":
    main()
