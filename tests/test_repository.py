"""Repository: upsert/dedup, statuse + events, FTS, rescore (spec §12–§14, §24)."""
import json

import repository as repo


def _add(url, title="Titull", **kw):
    op = {
        "source": "test", "url": url, "title": title,
        "company": f"Org-{abs(hash(url)) % 100000}",
        "description": "përshkrim", "location": "Berlin", "remote": False,
        "tags": [], "kind": "job",
    }
    op.update(kw)
    return repo.upsert_opportunity(op)


def test_upsert_new_and_dup_same_url():
    assert _add("https://x.com/a/1") == "new"
    assert _add("https://x.com/a/1") == "dup"
    rows = repo.list_opportunities()
    assert len([r for r in rows if r["source_url"] == "https://x.com/a/1"]) == 1


def test_canonical_url_dedup_merges_and_keeps_sources():
    assert _add("https://x.com/b/2") == "new"
    assert _add("https://x.com/b/2?utm_source=newsletter") == "dup"
    rows = repo.list_opportunities(source="test")
    target = [r for r in rows if r["canonical_url"] == "https://x.com/b/2"]
    assert len(target) == 1
    # dy burime-rreshta me URL origjinale të ndryshme
    import sqlite3
    with repo.db() as con:
        n = con.execute(
            "SELECT COUNT(*) FROM opportunity_sources WHERE url LIKE 'https://x.com/b/2%'"
        ).fetchone()[0]
    assert n == 2


def test_legacy_kind_input_canonicalized():
    _add("https://x.com/c/3", kind="opportunity", opportunity_type="SaaS")
    j = [r for r in repo.list_opportunities(kind="freelance") if r["source_url"] == "https://x.com/c/3"][0]
    assert j["kind"] == "freelance"
    assert j["opportunity_type"] == "SaaS"


def test_status_update_creates_event_and_saved_at():
    _add("https://x.com/d/4")
    j = [r for r in repo.list_opportunities() if r["source_url"] == "https://x.com/d/4"][0]
    out = repo.update_opportunity(j["id"], status="saved", notes="ruajta")
    assert out["status"] == "saved"
    assert out["saved_at"] is not None
    import sqlite3
    with repo.db() as con:
        ev = con.execute(
            "SELECT from_status, to_status FROM status_events WHERE opportunity_id=?",
            (j["id"],),
        ).fetchone()
    assert ev["from_status"] == "new" and ev["to_status"] == "saved"
    # update-imi i dytë me të njëjtin status s'krijojn event të dytë
    repo.update_opportunity(j["id"], status="saved")
    with repo.db() as con:
        n = con.execute(
            "SELECT COUNT(*) FROM status_events WHERE opportunity_id=?", (j["id"],)
        ).fetchone()[0]
    assert n == 1


def test_fts_finds_description_terms():
    _add("https://x.com/e/5", title="Rol pa lidhje", description="kerkoj nje specialist xenomorphic pipeline")
    hits = repo.list_opportunities(q="xenomorphic")
    assert any(r["source_url"] == "https://x.com/e/5" for r in hits)
    assert repo.list_opportunities(q="zzzznosuchterm") == []


def test_rescore_all_reflects_profile_change():
    repo.save_profile({**repo.get_profile(),'skills':'python','languages':'','products':''})
    _add("https://x.com/f/6", title="Python Developer", description="python python python")
    before = [r for r in repo.list_opportunities() if r["source_url"] == "https://x.com/f/6"][0]["score"]
    prof = repo.get_profile()
    repo.save_profile({**prof, "skills": "kubernetes", "languages": "", "products": ""})
    n = repo.rescore_all()
    assert n > 0
    after = [r for r in repo.list_opportunities() if r["source_url"] == "https://x.com/f/6"][0]["score"]
    # pa 'python' në profil, titulli s'matchohet më
    assert after <= before


def test_delete_removes_row():
    _add("https://x.com/g/7")
    j = [r for r in repo.list_opportunities() if r["source_url"] == "https://x.com/g/7"][0]
    assert repo.delete_opportunity(j["id"]) is True
    assert repo.get_opportunity(j["id"]) is None
