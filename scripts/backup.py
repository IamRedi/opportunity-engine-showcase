"""Backup i Opportunity Engine (Faza 8A): DB + uploads + config-metadata.

Përdorim:
    .venv/bin/python scripts/backup.py --out backups/2026-09-04
Prodhon: database (kopje konsistente përmes sqlite backup API), uploads/,
manifest.json (me sha256 për çdo skedar). Sekretet nga env NUK përfshihen.
"""
import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import config  # noqa: E402


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"backup-{stamp}"
    target.mkdir(parents=True)

    # 1) DB përmes backup API (konsistent edhe gjatë shkrimit)
    src = sqlite3.connect(config.DB_PATH)
    db_backup = target / "jobs.db"
    dst = sqlite3.connect(db_backup)
    src.backup(dst)
    dst.close()
    # verifikim integriteti menjëherë
    check = sqlite3.connect(db_backup)
    ok = check.execute("PRAGMA integrity_check").fetchone()[0]
    check.close()
    if ok != "ok":
        print("GABIM: integriteti i DB backup dështoi:", ok)
        sys.exit(1)

    # 2) uploads
    uploads = config.DATA_DIR / "uploads"
    if uploads.exists():
        shutil.copytree(uploads, target / "uploads")

    # 3) manifest (config metadata jo-sekrete)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "env": config.APP_ENV,
        "app_name": config.APP_NAME,
        "schema_hint": "jobs.db (SQLite, schema_version te settings)",
        "files": {},
    }
    for p in sorted(target.rglob("*")):
        if p.is_file():
            manifest["files"][str(p.relative_to(target))] = sha256(p)
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("backup u krijua:", target)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BASE / "backups"))
    args = ap.parse_args()
    main(Path(args.out))
