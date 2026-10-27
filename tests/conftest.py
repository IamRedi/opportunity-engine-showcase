"""Përgatit DB të testit para se të importohet app/repository (env në import)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = ROOT / "tests" / "_test_db.sqlite3"

if TEST_DB.exists():
    TEST_DB.unlink()

os.environ["OPPORTUNITY_DB"] = str(TEST_DB)
os.environ["APP_ENV"] = "test"


import pytest

from demo_safety import install_network_guard
install_network_guard()


@pytest.fixture(autouse=True)
def isolated_public_source_cache(monkeypatch, tmp_path):
    import source_http
    monkeypatch.setattr(source_http, 'CACHE_DIR', tmp_path / 'public-sources')
