"""Restore i Opportunity Engine NË NJË TARGET TË IZOLUAR (kurrë live).

Përdorim:
    .venv/bin/python scripts/restore.py --backup backups/backup-<stamp> --target /tmp/oe_restore

Verifikon manifest.json (sha256), rikthen jobs.db + uploads/, ekzekuton
PRAGMA integrity_check. NUK prek asnjëherë databazën live.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
import shutil
from pathlib import Path


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main(backup_dir: Path, target: Path) -> None:
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        print("GABIM: manifest.json mungon në backup")
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if "files" not in manifest:
        print("GABIM: manifest pa 'files' — backup i pavlefshëm")
        sys.exit(1)

    target.mkdir(parents=True, exist_ok=True)
    errors = []
    for rel, expected in manifest["files"].items():
        src = backup_dir / rel
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            shutil.copy2(src, dst)
            if sha256(dst) != expected:
                errors.append(f"sha256 nuk përputhet: {rel}")
        else:
            errors.append(f"mungon në backup: {rel}")

    db_path = target / "jobs.db"
    if not db_path.exists():
        print("GABIM: jobs.db mungon në backup")
        sys.exit(1)
    check = sqlite3.connect(db_path)
    integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    version = None
    try:
        version = check.execute(
            "SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]
    except sqlite3.OperationalError:
        pass
    check.close()
    if integrity != "ok":
        errors.append(f"integrity: {integrity}")

    if errors:
        print("RESTORE DËSHTOI:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print(f"restore OK → {target}")
    print(f"  schema_version: {version} | integrity: {integrity}")
    print("  KUJDES: kjo është kopje e izoluar — mos e mbishkruaj live pa copëz 2/3.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True)
    ap.add_argument("--target", required=True)
    args = ap.parse_args()
    main(Path(args.backup), Path(args.target))
