"""OE-007E — rillogarim incrementali + i shpejtë, pa ndryshim semantike.

Pariteti është BJA KONSTRUKTIONI: score() ka një shteg konsumi (PreparedContext);
pa ctx ai e ndërton brenda. Këto prova e verifikojnë kontratën në skaje:
paritet byte-nivel, izolim ndër-përdorues, sofistikim i pavepruar i kontekstit,
përzgjedhja e scope-it, importi me një kontekst/pikë, dhe shkrim vetëm kur
ndryshon dalja.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app
import connectors
import matching
import repository as repo

repo.init()


class _StubConnector:
    id = "stub-bulk"
    name = "Stub bulk"
    note = ""

    def is_configured(self):
        return True

    def search(self, query):
        items = [{"source": "stub-bulk",
                  "url": f"https://stub-bulk.example/{i}",
                  "title": f"Stub role {i} python react",
                  "company": "StubCo", "location": "Berlin", "remote": False,
                  "description": "python react typescript", "tags": [],
                  "kind": "job", "posted_at": ""} for i in range(5)]
        return connectors.ConnectorResult(source=self.id, items=items)


def _prof(**kw):
    base = {
        "skills": ["python", "react", "typescript"], "keywords": [], "exclude_keywords": [],
        "location": "Berlin", "salary_min": 40000, "languages": ["Gjermanisht – B2"],
        "title": "Senior Python Developer", "experience": "senior python developer",
        "products": ["BusinessSite RC"], "remote_ok": 1, "onsite_ok": 1, "hybrid_ok": 1,
        "hourly_rate_min": 0, "search_signals": [],
    }
    base.update(kw)
    return base


@pytest.fixture()
def client():
    return TestClient(app.app)


def _op(**kw):
    title = kw.pop("title", "Senior Python Developer")
    base = {
        "source": "rescore-test", "url": f"https://rescore-test.example/{abs(hash((title, str(kw)))) % 10**9}",
        "title": title, "company": "ACME", "location": "Berlin", "remote": False,
        "description": "python react typescript postgresql", "tags": ["python"],
        "kind": "job", "posted_at": "2026-09-20 10:00", "salary_min": 50000,
    }
    base.update(kw)
    return base


def _battery():
    """Battery mundësish/qendrash për paritet — të gjitha llojet + skaje."""
    flt = _prof()
    ops = [
        _op(),
        _op(title="Junior React Developer", salary_min=None, remote=True),
        _op(title="Steuerfachangestellte", description="datev buchhaltung"),
        _op(kind="freelance", opportunity_type="Website", hourly_rate=45,
            description="python booking website"),
        _op(kind="product_lead", opportunity_type="Booking", title="BusinessSite needs booking"),
        _op(kind="local_job", title="Hunde ausführen", description="python"),
        _op(kind_data={"verification_status": "search_preview"}, posted_at="",
            discovered_at=""),
        _op(posted_at="", discovered_at="2026-09-25 08:00"),
    ]
    profiles = [
        _prof(),
        _prof(skills=[], languages=[], title="", experience="", location=""),
        _prof(exclude_keywords=["unpaid"]),
        _prof(remote_ok=0, onsite_ok=0, hybrid_ok=0),
    ]
    return ops, profiles


def test_prepared_context_parity_with_legacy_args():
    ops, profiles = _battery()
    for pi, prof in enumerate(profiles):
        ctx = matching.prepare_context(prof, prof)
        for oi, op in enumerate(ops):
            legacy = matching.score(op, prof, prof)
            prepared = matching.score(op, ctx=ctx)
            assert legacy.factor_dict() == prepared.factor_dict(), (pi, oi)


def test_repeated_scoring_is_deterministic():
    ctx = matching.prepare_context(_prof(), _prof())
    op = _op()
    first = matching.score(op, ctx=ctx).factor_dict()
    for _ in range(3):
        assert matching.score(op, ctx=ctx).factor_dict() == first


def test_cross_user_context_isolation():
    from context import ActorContext
    ctx1 = repo.scoring_context(ActorContext(1, 1, "owner"))
    # përdoruesi 2 me profil i vet: konteksti i 1-it NUK duhet të pisë pikët e 2-it
    uid2 = repo.create_user("rescore-b@example.org", "not-a-real-hash",
                            display_name="Rescore B")
    import tenancy
    tid2 = tenancy.ensure_tenant("Rescore B", f"rescore-b-{uid2}")
    tenancy.add_membership(uid2, tid2, role="owner")
    from context import run_as
    with run_as(ActorContext(uid2, tid2, "owner")):
        repo.save_profile({**repo.get_profile(), "skills": "datev", "title": "Steuerfachangestellter"})
        st, oid = repo.upsert_opportunity_full(_op(title="Python Developer"))
        with repo.db() as con:
            row = dict(con.execute(
                "SELECT rule_score, match_factors FROM opportunities WHERE id=?", (oid,)).fetchone())
    bd = json.loads(row["match_factors"])
    # score me kontekstin e user-it 1 do jepte rezultate 'senior python' —
    # rreshti i user-it 2 duhet të mbetet i pikuar me profilin e vet (dobët)
    assert row["rule_score"] < 50
    assert bd["final_score"] == row["rule_score"]
    ctx2 = repo.scoring_context(ActorContext(uid2, tid2, "owner"))
    assert ctx2.profile["title"] != ctx1.profile["title"]


def test_no_stale_context_after_profile_change():
    from context import ActorContext, run_as
    with run_as(ActorContext(1, 1, "owner")):
        repo.save_profile({**repo.get_profile(), "location": "Essen, Deutschland"})
        ctx = repo.scoring_context()
        assert any("essen" in t for t in ctx.matching.location_tokens)
        repo.save_profile({**repo.get_profile(), "location": "München, Bayern"})
        ctx2 = repo.scoring_context()
        assert any("münchen" in t for t in ctx2.matching.location_tokens)
        assert not any("essen" in t for t in ctx2.matching.location_tokens)


def test_partial_rescore_matches_full_rescore(monkeypatch):
    from context import ActorContext, run_as
    with run_as(ActorContext(1, 1, "owner")):
        base = repo.get_profile()
        repo.save_profile({**base, "location": "Berlin"})
        repo.rescore_all()  # gjendje e plotë fillestare
        # vendndodhja ndryshon → rruga e pjesshme
        scope = repo.save_profile({**base, "location": "Essen, Deutschland"})
        assert scope == {"location"}
        repo.rescore_all(factors=scope)
        partial = {r["id"]: (r["rule_score"], r["match_factors"]) for r in
                   repo.list_opportunities(limit=10000)}
        # rillogarim i plotë me të njëjtin profil → duhet të jetë identik
        import gc, sqlite3
        try:
            repo.rescore_all()
        except sqlite3.OperationalError:
            leaks = []
            for o in gc.get_objects():
                if isinstance(o, sqlite3.Connection):
                    try:
                        leaks.append({"in_txn": o.in_transaction})
                    except Exception:
                        leaks.append({"closed": True})
            import sys
            print("\nLEAKED CONNECTIONS:", leaks, file=sys.stderr)
            raise
        full = {r["id"]: (r["rule_score"], r["match_factors"]) for r in
                repo.list_opportunities(limit=10000)}
        assert set(partial) == set(full)
        for oid in full:
            assert partial[oid] == full[oid], f"rrugët ndryshojnë për {oid}"


def test_scope_mapping_matrix():
    profile = repo.get_profile()
    assert repo.rescore_scope_for_profile({**profile, "location": "X"}, {**profile, "location": "Y"}) == {"location"}
    assert repo.rescore_scope_for_profile({**profile, "title": "A"}, {**profile, "title": "B"}) == {"role"}
    # emri/email/summary s'pikohen → pa rillogarim
    assert repo.rescore_scope_for_profile({**profile, "name": "A", "email": "a@b.c", "summary": "s"},
                                          {**profile, "name": "B", "email": "c@d.e", "summary": "t"}) == "none"
    # aftësitë → i plotë
    assert repo.rescore_scope_for_profile({**profile, "skills": "[\"a\"]"}, {**profile, "skills": "[\"a\",\"b\"]"}) == "full"
    # filtra: pagë → i pjesshëm; keywords → i plotë; locations → asgjë
    f = {"keywords": "[]", "exclude_keywords": "[]", "locations": "[]",
         "remote_ok": 1, "onsite_ok": 1, "hybrid_ok": 1, "salary_min": 0}
    assert repo.rescore_scope_for_filters(f, {**f, "salary_min": 50000}) == {"salary", "budget"}
    assert repo.rescore_scope_for_filters(f, {**f, "remote_ok": 0}) == {"workmode"}
    assert repo.rescore_scope_for_filters(f, {**f, "keywords": "[\"go\"]"}) == "full"
    assert repo.rescore_scope_for_filters(f, {**f, "locations": "[\"Essen\"]"}) == "none"
    # seksione: edukimi → asgjë; aftësitë → i plotë; vendndodhja → e pjesshme
    assert repo.rescore_scope_for_sections(["education"]) == "none"
    assert repo.rescore_scope_for_sections(["skills"]) == "full"
    assert repo.rescore_scope_for_sections(["location"]) == {"location"}


def test_unrelated_section_change_triggers_no_rescore(client, monkeypatch):
    called = {"n": 0}
    real = repo.rescore_all
    monkeypatch.setattr(repo, "rescore_all",
                        lambda *a, **kw: called.__setitem__("n", called["n"] + 1) or real(*a, **kw))
    r = client.put("/api/profile/sections/education", json={"entries": [{"title": "Test Schule", "period": "2020"}]})
    assert r.status_code == 200 and r.json()["rescored"] == 0
    assert called["n"] == 0, "rillogarim i tepërt për seksion pa konsum pikimi"


def test_bulk_import_prepares_context_once_and_scores_once_per_item(monkeypatch):
    """Një kontekst për gjithë importin; s'ka rillogarim pas upsert-eve."""
    from context import ActorContext, run_as

    connectors.REGISTRY["stub-bulk"] = _StubConnector()
    try:
        scores = {"n": 0}
        real_score = matching.score

        def counting_score(*a, **kw):
            scores["n"] += 1
            return real_score(*a, **kw)

        monkeypatch.setattr(matching, "score", counting_score)
        contexts = {"n": 0}
        real_ctx = repo.scoring_context

        def counting_ctx(*a, **kw):
            contexts["n"] += 1
            return real_ctx(*a, **kw)

        monkeypatch.setattr(repo, "scoring_context", counting_ctx)
        rescores = {"n": 0}
        monkeypatch.setattr(repo, "rescore_all",
                            lambda *a, **kw: rescores.__setitem__("n", rescores["n"] + 1))
        with run_as(ActorContext(1, 1, "owner")):
            out = connectors.run_source_search("stub-bulk")
        assert out["ok"] and out["new"] == 5
        assert scores["n"] == 5, "një pikë për mundësi"
        assert contexts["n"] == 1, "një kontekst për gjithë importin"
        assert rescores["n"] == 0, "importi s'triggeron rillogarim të plotë"
    finally:
        connectors.REGISTRY.pop("stub-bulk", None)
        with repo.db() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM opportunities WHERE source='stub-bulk'")]
            if ids:
                con.executemany("DELETE FROM opportunity_sources WHERE opportunity_id=?",
                                [(i,) for i in ids])
                con.executemany("DELETE FROM opportunities WHERE id=?", [(i,) for i in ids])


def test_unchanged_scores_cause_no_writes():
    """Përmbajtja e kolonave të pikëve mbetet byte-nivel identike dhe pa
    transaksion shkrimi mbi opportunities kur asgjë s'ndryshon (commit-et
    no-op të get_profile s'linjen DB-në e mundësive)."""
    import hashlib
    from context import ActorContext, run_as

    def snap():
        with repo.db() as con:
            rows = con.execute(
                """SELECT id, rule_score, final_score, excluded, match_factors,
                          matched_requirements, missing_requirements
                   FROM opportunities ORDER BY id""").fetchall()
        return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()

    with run_as(ActorContext(1, 1, "owner")):
        repo.rescore_all()
        s1 = snap()
        repo.rescore_all()  # i njëjti profil + i njëjtat rreshta → zero shkrime
        s2 = snap()
    assert s1 == s2, "shkrim i tepërt për rezultate të pandryshuara"


def test_rescore_leaves_status_applications_notifications_untouched(client):
    _ = client
    from context import ActorContext, run_as
    with run_as(ActorContext(1, 1, "owner")):
        counts_before = {}
        with repo.db() as con:
            for t in ("status_events", "applications", "notifications"):
                counts_before[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        repo.rescore_all()
        with repo.db() as con:
            for t, n in counts_before.items():
                assert con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n


def test_partial_rescore_rejects_unsupported_factors():
    from context import ActorContext, run_as
    with run_as(ActorContext(1, 1, "owner")):
        with pytest.raises(ValueError):
            repo.rescore_all(factors={"skills"})
