"""OE-007C — higjena e të dhënave legale HN + integriteti i renditjes.

Fshirje idempotente e rreshtave të importuar para portës aktuale të
"Hacker News" që portea kanonike i refuzon SOT — identifikuar NUK sipas
pikëve, por vetëm me portën kanonike për thread-in përkatës.

Autoriteti i portës është sipas thread-it (si konektorët aktualë):
- thread-i "Who is hiring?" (source 'hn_hiring', kind job): rregull vetëm
  negative — hidh poshtë postimin që nis me "seeking work"/"for hire"
  (search_apis/extended_sources.HNHiring). Çdo postim tjetër i thread-it
  është punësim sipas përkufizimit, prandaj nuk preket kurrë.
- thread-i "Freelancer? Seeking Freelancer?" (source 'hackernews_freelance',
  'hn_leads' dhe alias-i legacy 'hackernews'): porta e plotë
  search_apis._hn_is_hiring — refuzon vetë-promovimin dhe pranon vetëm
  intent demand-i.

Semantika e pastrimit: FSHIRJE FIZIKE (jo archive/excluded), sepse:
- rreshtat janë artefakte të importit të pavlefshëm — portea aktuale i
  refuzon dhe s'mund të rikthehen ligjërisht përmes importit;
- fshirja është konventë e first-class e repository (`delete_opportunity`,
  buton "Fshi" në UI) dhe FK-të e opportunity_sources / opportunity_lenses /
  status_events / applications kanë ON DELETE CASCADE me PRAGMA
  foreign_keys = ON;
- notifications NUK ka FK — rreshtat që i referohen id-ve të fshira
  fshihen eksplikatisht (praesentis: s'ka asnjë);
- provenjenca mbetet: backup-i i DB-së para pastrimit + tabela e dëshmive
  (docs/reviews/OE-007C_CLEANUP.md) me id/titull/url/identitete burimi.

Përdorim:
    python scripts/cleanup_legacy_hn.py            # dry-run, tabela e dëshmive
    python scripts/cleanup_legacy_hn.py --apply    # pastrim real
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import search_apis  # noqa: E402

# Burimet e thread-eve HN sipas konektorëve aktualë. 'hackernews' është
# vlera e vjetër e burimit të importuar para portës — trajtohet si thread
# i Freelancer-it (kreu i vetëm si burim i pavarur sot).
FREELANCER_THREAD_SOURCES = frozenset(
    {"hackernews", "hackernews_freelance", "hn_leads"})
HIRING_THREAD_SOURCES = frozenset({"hn_hiring"})

# E njëjta rregull negative si HNHiring (extended_sources.py).
_SEEKING_PREFIX = re.compile(r"(?i)^[\W_]*(seeking work|for hire)")


def reject_reason(source: str, kind: str, title: str, description: str) -> str:
    """Arsyeja e refuzimit sipas portës kanonike të thread-it, ose ''."""
    title_l = (title or "").strip()
    desc = (description or "")
    if source in FREELANCER_THREAD_SOURCES:
        post = f"{title_l}\n{desc}"
        if search_apis._hn_is_hiring(post):
            return ""
        if _SEEKING_PREFIX.match(title_l) or _SEEKING_PREFIX.match(desc):
            return "self-promotion header (SEEKING WORK / for hire)"
        return "no hiring-intent pattern (freelancer demand gate)"
    if source in HIRING_THREAD_SOURCES:
        if _SEEKING_PREFIX.match(title_l) or _SEEKING_PREFIX.match(desc):
            return "seeking-work post inside Who-is-hiring thread"
        return ""
    return ""


def collect(con) -> list[dict]:
    """Të gjithë rreshtat HN që porta kanonike aktuale i refuzon — me dëshmi."""
    placeholders = ",".join("?" * len(FREELANCER_THREAD_SOURCES | HIRING_THREAD_SOURCES))
    rows = con.execute(
        f"""SELECT o.id, o.user_id, o.tenant_id, o.kind, o.source, o.title,
                   o.description, o.final_score, o.status,
                   o.source_url, o.canonical_url, o.external_id
            FROM opportunities o
            WHERE o.source IN ({placeholders})
            ORDER BY o.source, o.id""",
        tuple(sorted(FREELANCER_THREAD_SOURCES | HIRING_THREAD_SOURCES)),
    ).fetchall()
    out = []
    for r in rows:
        reason = reject_reason(r["source"], r["kind"], r["title"], r["description"])
        if not reason:
            continue
        sources = [dict(s) for s in con.execute(
            "SELECT source, external_id, url FROM opportunity_sources WHERE opportunity_id=?",
            (r["id"],))]
        lenses = [l["lens"] for l in con.execute(
            "SELECT lens FROM opportunity_lenses WHERE opportunity_id=?", (r["id"],))]
        dependents = {
            "applications": con.execute(
                "SELECT COUNT(*) FROM applications WHERE opportunity_id=?", (r["id"],)).fetchone()[0],
            "notifications": con.execute(
                "SELECT COUNT(*) FROM notifications WHERE opportunity_id=?", (r["id"],)).fetchone()[0],
            "status_events": con.execute(
                "SELECT COUNT(*) FROM status_events WHERE opportunity_id=?", (r["id"],)).fetchone()[0],
        }
        out.append({**dict(r), "reason": reason,
                    "source_identities": sources, "lenses": lenses,
                    "dependents": dependents})
    return out


def cleanup(con, dry_run: bool = True) -> tuple[list[dict], list[int]]:
    """Mbledh dhe — me dry_run=False — fshin rreshtat e kontaminuar.

    Kthen (tabela e dëshmive, id-të e fshira). Idempotent: pa kushte
    hyrëse, kontrollon vetë strukturën referuese para çdo fshirjeje."""
    evidence = collect(con)
    deleted_ids: list[int] = []
    if dry_run:
        return evidence, deleted_ids
    for row in evidence:
        oid = row["id"]
        if row["dependents"]["applications"]:
            raise RuntimeError(
                f"opportunity {oid} ka {row['dependents']['applications']} aplikime —"
                " ndalo: fshirja nuk është semantikë e sigurt për këtë rresht")
        # notifications s'ka FK — pastrohet eksplicikisht (zakonisht 0).
        con.execute("DELETE FROM notifications WHERE opportunity_id=?", (oid,))
        # opportunity_sources / lenses / status_events kaskadojnë
        # (PRAGMA foreign_keys=ON te repository.db()).
        con.execute("DELETE FROM opportunities WHERE id=?", (oid,))
        deleted_ids.append(oid)
    return evidence, deleted_ids


def main() -> int:
    import repository as repo
    apply = "--apply" in sys.argv
    with repo.db() as con:  # commit-on-exit për rrugën --apply
        evidence, deleted = cleanup(con, dry_run=not apply)
    print(f"OE-007C — rreshta HN të refuzuar nga porta kanonike: {len(evidence)}")
    for row in evidence:
        print(f"  id={row['id']} user={row['user_id']} kind={row['kind']} "
              f"source={row['source']} score={row['final_score']}")
        print(f"    title  : {(row['title'] or '')[:88]}")
        print(f"    reason : {row['reason']}")
        identities = "; ".join(
            f"{s['source']}|{s['url']}" for s in row["source_identities"])
        print(f"    sources: {identities}")
        print(f"    lenses : {','.join(row['lenses']) or '-'}  "
              f"dependents: {row['dependents']}")
    if apply:
        print(f"\nAPPLY: u fshinë {len(deleted)} rreshta: {deleted}")
        with repo.db() as con:
            print(f"integrity: {con.execute('PRAGMA integrity_check').fetchone()[0]}")
    else:
        print("\nDRY-RUN: asgjë nuk u ndryshua. Përdor --apply për pastrim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
