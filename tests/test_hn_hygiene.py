"""OE-007C — higjena e të dhënave legale HN: prova regresioni.

Porta kanonike është sipas thread-it: "Who is hiring?" përdor vetëm rregullin
negative (hidh "seeking work"/"for hire"), thread-i i Freelancer-it përdor
search_apis._hn_is_hiring (demand gate). Rreshtat legacy `hackernews` janë
artefakte para portës — duhet të zhduken nga feed-i, njoftimet dhe numërimet,
pa prekur gjendjen e përdoruesve, profilet, aplikimet dhe dedup-in ndër-burime.
"""
import json

import pytest

import repository as repo
from scripts.cleanup_legacy_hn import cleanup, collect, reject_reason

repo.init()


def _seed_legacy_hn(user_id: int, tenant_id: int, url: str, title: str,
                    score: int = 95, kind: str = "freelance") -> int:
    """Rresht legacy artificial: i futur me SQL direkt (para portës), ashtu
    si artefaktet reale të importuara përpara `_hn_is_hiring`."""
    with repo.db() as con:
        cur = con.execute(
            """INSERT INTO opportunities
               (kind, opportunity_type, source, external_id, source_url, canonical_url,
                dedup_key, title, org_name, description, location, remote_type, tags,
                salary_text, posted_at, discovered_at, updated_at,
                rule_score, final_score, excluded, status, kind_data, match_factors,
                l3_key, user_id, tenant_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, "", "hackernews", "", url, url, url, title, "", "Node.js, TypeScript",
             "", "", "[]", "", "2026-09-05 10:00", "2026-09-05 10:00", "2026-09-05 10:00",
             score, score, 0, "new", "{}", "{}", "", user_id, tenant_id))
        oid = cur.lastrowid
        con.execute(
            "INSERT INTO opportunity_sources (opportunity_id, source, external_id, url,"
            " first_seen, last_seen) VALUES (?,?,?,?,?,?)",
            (oid, "hackernews", "", url, "2026-09-05 10:00", "2026-09-05 10:00"))
        con.execute(
            "INSERT INTO notifications (profile_id, opportunity_id, type, title, summary,"
            " dedup_key, created_at, user_id, tenant_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (1, oid, "new_high_match", title, "", f"sched:9:{oid}",
             "2026-09-05 10:00", user_id, tenant_id))
    return oid


def _seed_legit_hiring(user_id: int, tenant_id: int, url: str, title: str) -> int:
    ctx = repo._actor(__import__("context").ActorContext(user_id, tenant_id, "owner"))
    status, oid = repo.upsert_opportunity_full(
        {"source": "hn_hiring", "url": url, "title": title, "company": "Smarkets",
         "location": "London", "remote": False,
         "description": "Full Time | Hybrid - Onsite (London, UK) python backend team",
         "tags": [], "kind": "job"}, ctx=ctx)
    assert status == "new"
    return oid


# 1) Porta aktuale refuzon vetë-promovimin — variantet e kokës.
def test_current_gate_rejects_self_promotion():
    variants = [
        "SEEKING WORK | France | Node.js expert",
        "Seeking Work, Europe / Asia, remote OK",
        "seeking work — full-stack dev",
        "For hire | backend developer",
    ]
    for title in variants:
        assert reject_reason("hackernews", "freelance", title, "tech stack") != "", title
        import search_apis
        assert search_apis._hn_is_hiring(f"{title}\nNode.js, TypeScript") is False, title


# 2) Rregulli kanonik i thread-it "Who is hiring?" është vetëm negative.
def test_who_is_hiring_uses_negative_only_rule():
    legit = "Smarkets ( https://www.smarkets.com ) | Full Time | Hybrid - Onsite (London, UK)"
    assert reject_reason("hn_hiring", "job", legit, "python backend") == ""
    contaminated = "SEEKING WORK | Node.js dev"
    assert reject_reason("hn_hiring", "job", contaminated, "") != ""


# 3) Rreshtat e kontaminuar nuk dalin më në feed / numërime / pragje.
def test_legacy_contaminated_rows_cannot_surface_in_ranked_feed():
    uid, tid = 1, 1
    c1 = _seed_legacy_hn(uid, tid, "https://hn-hygiene.example/legacy-1",
                         "SEEKING WORK | Senior Node.js dev | score bait", score=99)
    c2 = _seed_legacy_hn(uid, tid, "https://hn-hygiene.example/legacy-2",
                         "SEEKING WORK, remote worldwide", score=98)
    legit = _seed_legit_hiring(uid, tid, "https://hn-hygiene.example/legit-1",
                               "LegitCo | Backend Engineer | London")
    with repo.db() as con:
        evidence, deleted = cleanup(con, dry_run=False)
    assert set(deleted) >= {c1, c2}
    jobs = repo.list_opportunities(ctx=repo._actor(
        __import__("context").ActorContext(uid, tid, "owner")))
    ids = {j["id"] for j in jobs}
    assert c1 not in ids and c2 not in ids and legit in ids
    # "SEEKING WORK" nuk shfaqet kund në rezultate
    assert all("seeking work" not in (j["title"] or "").lower() for j in jobs)
    # s'llogariten si përputhje të vlefshme ≥70
    with repo.db() as con:
        hi = con.execute(
            "SELECT COUNT(*) FROM opportunities WHERE user_id=? AND final_score>=70",
            (uid,)).fetchone()[0]
        assert hi == sum(1 for j in jobs if j["final_score"] >= 70)
    # numërimet e statusit nuk i përfshijnë
    counts = repo.status_counts(ctx=repo._actor(
        __import__("context").ActorContext(uid, tid, "owner")))
    assert sum(counts.values()) == len(jobs)


# 4) Nuk mund të njoftojnë: njoftimi i referuar rreshtit të fshirë hiqet,
#    dhe after-cleanup askund s'ka rreshta SEEKING WORK për t'u njoftuar.
def test_contaminated_rows_cannot_notify():
    uid, tid = 2, 2
    c = _seed_legacy_hn(uid, tid, "https://hn-hygiene.example/notify-1",
                        "SEEKING WORK | notification bait", score=97)
    with repo.db() as con:
        before = con.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=?", (uid,)).fetchone()[0]
        cleanup(con, dry_run=False)
        after = con.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=?", (uid,)).fetchone()[0]
        orphan = con.execute(
            "SELECT COUNT(*) FROM notifications n LEFT JOIN opportunities o"
            " ON o.id=n.opportunity_id WHERE n.opportunity_id IS NOT NULL AND o.id IS NULL"
        ).fetchone()[0]
    assert after == before - 1  # njoftimi i rreshtit të fshirë u hoq
    assert orphan == 0
    actor = repo._actor(__import__("context").ActorContext(uid, tid, "owner"))
    assert all("seeking work" not in (j["title"] or "").lower()
               for j in repo.list_opportunities(ctx=actor))


# 5) Postimet legjitime të punësimit mbeten të paprekura.
def test_legitimate_hn_hiring_posts_survive():
    uid, tid = 1, 1
    legit = _seed_legit_hiring(uid, tid, "https://hn-hygiene.example/legit-2",
                               "CyberAtlas | Software Engineer | REMOTE")
    with repo.db() as con:
        evidence, deleted = cleanup(con, dry_run=False)
        row = con.execute("SELECT id FROM opportunities WHERE id=?", (legit,)).fetchone()
    assert legit not in deleted and row is not None
    assert all(r["source"] != "hn_hiring" for r in evidence)


# 6) Dedup-i ndër-burime mbetet i paprekur pas pastrimit.
def test_cross_source_dedup_intact_after_cleanup():
    uid, tid = 1, 1
    ctx = repo._actor(__import__("context").ActorContext(uid, tid, "owner"))
    url = "https://hn-hygiene.example/dedup-1"
    first = repo.upsert_opportunity_full(
        {"source": "hn_hiring", "url": url, "title": "DedupCo | Platform Engineer",
         "company": "DedupCo", "location": "Berlin", "remote": False,
         "description": "python platform team", "tags": [], "kind": "job"}, ctx=ctx)
    second = repo.upsert_opportunity_full(
        {"source": "remoteok", "url": url + "?utm_source=x", "title": "Platform Engineer",
         "company": "DedupCo", "location": "Berlin", "remote": True,
         "description": "python platform team", "tags": [], "kind": "job"}, ctx=ctx)
    assert first[0] == "new" and second[0] == "dup" and second[1] == first[1]
    with repo.db() as con:
        cleanup(con, dry_run=False)
    # pas pastrimit, i njëjti URL akoma shkrihet (kurrë i dyfishtë)
    third = repo.upsert_opportunity_full(
        {"source": "arbeitnow", "url": url, "title": "Platform Engineer",
         "company": "DedupCo", "location": "Berlin", "remote": True,
         "description": "python platform team", "tags": [], "kind": "job"}, ctx=ctx)
    assert third[0] == "dup" and third[1] == first[1]


# 7) Ndalo: rresht i kontaminuar ME aplikim → fshirja refuzohet, asgjë s'preket.
def test_cleanup_refuses_rows_with_applications():
    uid, tid = 1, 1
    c = _seed_legacy_hn(uid, tid, "https://hn-hygiene.example/app-1",
                        "SEEKING WORK | has application", score=90)
    try:
        with repo.db() as con:
            con.execute(
                "INSERT INTO applications (opportunity_id, application_type, status,"
                " created_at, updated_at, user_id) VALUES (?,?,?,?,?,?)",
                (c, "job_application", "draft", "2026-09-05 10:00", "2026-09-05 10:00", uid))
            with pytest.raises(RuntimeError):
                cleanup(con, dry_run=False)
            still = con.execute(
                "SELECT COUNT(*) FROM opportunities WHERE id=?", (c,)).fetchone()[0]
        assert still == 1  # ndërveprimi i sigurt: s'u fshi asgjë
    finally:
        # Proba e sigurisë le pas vetes gjendje të papastër — pastroje që
        # testet e mëpasshme të punojnë mbi bazë të pastër.
        with repo.db() as con:
            con.execute("DELETE FROM applications WHERE opportunity_id=?", (c,))
            con.execute("DELETE FROM notifications WHERE opportunity_id=?", (c,))
            con.execute("DELETE FROM opportunities WHERE id=?", (c,))


# 8) Idempotente + gjendja e përdoruesit/profilet/aplikimet e paprekura.
def test_cleanup_idempotent_and_user_state_unchanged():
    uid, tid = 1, 1
    _seed_legacy_hn(uid, tid, "https://hn-hygiene.example/idem-1",
                    "SEEKING WORK | idempotency probe", score=88)
    before = {}
    with repo.db() as con:
        for t in ("users", "tenants", "tenant_memberships", "profile", "profile_sections",
                  "profile_cv", "cv_suggestions", "filters", "search_profiles"):
            before[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    with repo.db() as con:
        cleanup(con, dry_run=False)
    after1 = {}
    with repo.db() as con:
        for t in before:
            after1[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        evidence2, deleted2 = cleanup(con, dry_run=False)
    for t in before:
        assert after1[t] == before[t], f"{t} u ndryshua"
    assert deleted2 == [] and evidence2 == []
    with repo.db() as con:
        print("integrity:", con.execute("PRAGMA integrity_check").fetchone()[0])
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
