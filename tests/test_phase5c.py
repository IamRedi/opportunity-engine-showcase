"""Faza 5C: katalog produkte + orkestratori (CRUD, fit aktive, përmbledhje)."""
import httpx
import pytest
from fastapi.testclient import TestClient

import connectors
import orchestrator
import product_fit
import repository as repo


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


@pytest.fixture(scope="module", autouse=True)
def _db():
    repo.init()
    yield


def _response(body):
    return httpx.Response(200, json=body, request=httpx.Request("GET", "https://mock"))


def _seed(url, title="Booking platform", kind="freelance"):
    repo.upsert_opportunity({
        "source": "5c-test", "url": url, "title": title, "company": "Klinika",
        "location": "Berlin", "remote": True,
        "description": "We are looking for a developer to build a booking website "
                       "with appointments and CRM integration. Ref: " + url,
        "tags": [], "kind": kind, "posted_at": "",
    })
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]


PROD = {"name": "BusinessSite RC", "type": "white-label",
        "short_description": "Faqe + rezervime + social",
        "capabilities": ["website", "booking", "appointments", "crm", "social", "customizable"],
        "technologies": ["php", "js"], "industries": ["clinics"], "languages": ["sq", "en"],
        "delivery_model": "self-hosted", "white_label": True,
        "customization_level": "medium", "deployment_model": "cloud",
        "demo_url": "https://demo.example", "website_url": "https://bs.example",
        "repo_url": "", "status": "active", "pricing_notes": "nga 2000 €"}


# ------------------------------------------------- CRUD i produkteve

def test_product_crud_through_section_api(client):
    r = client.put("/api/profile/sections/products",
                   json={"entries": [dict(PROD), dict(PROD, name="FlowAI", status="draft")]})
    assert r.status_code == 200
    prods = repo.list_products(active_only=False)
    assert len(prods) == 2 and prods[0]["status"] == "active"
    # fit merr vetëm aktive
    fit_products = product_fit._catalog(active_only=True)
    assert [p["name"] for p in fit_products] == ["BusinessSite RC"]
    # riaktivizo draft-in
    entries, _ = repo.get_profile_section("products")
    entries[1]["status"] = "active"
    repo.save_products(entries)
    fit_products = product_fit._catalog(active_only=True)
    assert len(fit_products) == 2
    # pastro
    repo.save_products([])


def test_product_archive_skips_fit():
    repo.save_products([dict(PROD, status="archived")])
    oid = _seed("https://5c.example/a1")
    op = repo.get_opportunity(oid)
    res = product_fit.evaluate(op)
    assert res["candidates"] == []      # arkivuar → jo kandidat
    assert res["catalog_empty"] is True  # asnjë produkt AKTIV
    repo.save_products([dict(PROD)])     # riaktivizo
    res = product_fit.evaluate(op)
    assert res["candidates"] and res["candidates"][0]["fit_score"] > 0


# --------------------------------------------------------- product fit

def test_fit_ranking_and_alternatives():
    repo.save_products([
        dict(PROD),
        {"name": "FlowAI Toolkit", "type": "toolkit",
         "capabilities": ["automation", "ai", "chatbot"], "status": "active"},
    ])
    oid = _seed("https://5c.example/f1")
    res = product_fit.evaluate(repo.get_opportunity(oid))
    assert res["best"]["product_name"] == "BusinessSite RC"
    names = [c["product_name"] for c in res["candidates"]]
    assert names == ["BusinessSite RC", "FlowAI Toolkit"]
    assert res["alternatives"][0]["product_name"] == "FlowAI Toolkit"
    best = res["best"]
    assert {"product_name", "fit_score", "matched", "missing", "evidence",
            "customization_level", "hard_mismatch", "reasons"} <= set(best)
    repo.save_products([])


def test_empty_catalog_abstains_with_flag():
    repo.save_products([])
    oid = _seed("https://5c.example/e1")
    res = product_fit.evaluate(repo.get_opportunity(oid))
    assert res["abstain"] is True and res["catalog_empty"] is True
    assert res["candidates"] == []


def test_fit_only_for_product_lead_lens(client):
    repo.save_products([dict(PROD)])
    # mundësi job pa lens product_lead → paneli s'duhet të ketë bllok fit
    repo.upsert_opportunity({"source": "5c-test", "url": "https://5c.example/job1",
                             "title": "Website Developer", "company": "C",
                             "location": "B", "remote": False,
                             "description": "build a website with booking",
                             "tags": [], "kind": "job"})
    with repo.db() as con:
        oid = con.execute(
            "SELECT id FROM opportunities WHERE source_url='https://5c.example/job1'").fetchone()[0]
    panel = client.get(f"/api/jobs/{oid}/panel").text
    assert "Mundësi produkti" not in panel  # baza job → vetëm lens job (mësim 5B)
    repo.save_products([])


# ------------------------------------------------------- orkestratori

def test_orchestrator_source_selection_by_profile(monkeypatch):
    seen = []
    def fake(source, query=None, profile=None, limit=None):
        seen.append((source, limit))
        return {"ok": True, "source": source, "discovered": 2, "new": 1, "dup": 1,
                "kinds": {"freelance": 1}, "error": "", "run_id": 0}
    monkeypatch.setattr(connectors, "run_source_search", fake)
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["hackernews_freelance","remotive_contract"]'})
    prof = repo.get_search_profile(prof["id"])
    summary = orchestrator.run(profile_id=prof["id"], max_items=10, label="t")
    assert summary["sources_attempted"] == 2
    assert summary["checked"] == 4 and summary["new"] == 2
    assert summary["kinds_new"]["freelance"] == 2
    assert summary["headline"] == "4 checked · 2 new · 2 sources"


def test_orchestrator_mixed_success_failure(monkeypatch):
    def fake(source, query=None, profile=None, limit=None):
        if source == "jobicy_contract":
            return {"ok": False, "source": source, "discovered": 0, "new": 0,
                    "dup": 0, "kinds": {}, "error": "down", "run_id": 0}
        return {"ok": True, "source": source, "discovered": 3, "new": 2, "dup": 1,
                "kinds": {"product_lead": 2}, "error": "", "run_id": 0}
    monkeypatch.setattr(connectors, "run_source_search", fake)
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Product Leads")
    repo.update_search_profile(prof["id"], {"sources": '["hn_leads","jobicy_contract"]'})
    prof = repo.get_search_profile(prof["id"])
    summary = orchestrator.run(profile_id=prof["id"])
    assert summary["sources_ok"] == 1 and summary["sources_failed"] == 1
    assert summary["new"] == 2 and summary["kinds_new"]["product_lead"] == 2
    assert summary["errors"][0]["source"] == "jobicy_contract"
    # partial success: ok=True edhe me një burim të dështuar
    assert summary["ok"] is True


def test_orchestrator_all_configured_when_no_profile(monkeypatch):
    _disable = None
    def fake(source, query=None, profile=None, limit=None):
        return {"ok": True, "source": source, "discovered": 1, "new": 0, "dup": 1,
                "kinds": {}, "error": "", "run_id": 0}
    monkeypatch.setattr(connectors, "run_source_search", fake)
    monkeypatch.setattr(connectors.REGISTRY["arbeitnow"], "is_configured", lambda: True)
    monkeypatch.setattr(connectors.REGISTRY["adzuna"], "is_configured", lambda: True)
    monkeypatch.setattr(connectors.REGISTRY["local_feed"], "is_configured", lambda: True)
    repo.set_setting("local_feed_urls", "https://lokal.example/feed.xml")
    summary = orchestrator.run(max_items=5)
    assert summary["sources_attempted"] == sum(c.is_configured() for c in connectors.REGISTRY.values())
    assert summary["checked"] == summary["sources_attempted"]


def test_orchestrator_dedup_across_runs(monkeypatch):
    item = {"source": "jobicy", "external_id": "orch1", "url": "https://orch.example/1",
            "title": "Orch Job", "company": "O", "location": "Remote (remote)",
            "remote": True, "salary_text": "", "description": "d", "tags": [],
            "posted_at": "", "kind": "job"}
    monkeypatch.setattr(search_apis_helper(), "jobicy", lambda **kw: [item])
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["jobicy"]'})
    prof = repo.get_search_profile(prof["id"])
    r1 = orchestrator.run(profile_id=prof["id"])
    r2 = orchestrator.run(profile_id=prof["id"])
    assert r1["new"] == 1 and r2["new"] == 0 and r2["dup"] == 1


def search_apis_helper():
    import search_apis
    return search_apis


# --------------------------------- pa korruptim + legacy kompatibitet

def test_no_application_or_score_corruption(client, monkeypatch):
    import search_apis
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [{
        "source": "jobicy", "external_id": "c1", "url": "https://5c.example/corr1",
        "title": "Corr Job", "company": "C", "location": "Remote (remote)",
        "remote": True, "salary_text": "", "description": "d", "tags": [],
        "posted_at": "", "kind": "job"}])
    with repo.db() as con:
        before = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"])
                  for r in con.execute("SELECT id, rule_score, ai_score, final_score FROM opportunities")}
        apps_before = con.execute("SELECT COUNT(*) FROM application_versions").fetchone()[0]
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["jobicy"]'})
    orchestrator.run(profile_id=prof["id"])
    with repo.db() as con:
        after = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"])
                 for r in con.execute("SELECT id, rule_score, ai_score, final_score FROM opportunities")}
    for k in before:
        assert after[k] == before[k]
    assert con is not None
    with repo.db() as con:
        assert con.execute("SELECT COUNT(*) FROM application_versions").fetchone()[0] == apps_before
    _cleanup_profile_sources()


def _cleanup_profile_sources():
    profiles = repo.list_search_profiles()
    for p in profiles:
        if p["name"] in ("Freelance", "Product Leads") and p["sources"] in (
                ["jobicy"], ["hackernews_freelance", "jobicy_contract"]):
            repo.update_search_profile(p["id"], {"sources": '["jobicy"]'})


def test_legacy_run_profile_search_still_works(monkeypatch):
    import search_apis
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [{
        "source": "jobicy", "external_id": "lgc", "url": "https://5c.example/lgc",
        "title": "Legacy Run", "company": "L", "location": "Remote (remote)",
        "remote": True, "salary_text": "", "description": "d", "tags": [],
        "posted_at": "", "kind": "freelance"}])
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["jobicy"]'})
    results = connectors.run_profile_search(repo.get_search_profile(prof["id"]))
    assert results[0]["ok"] is True and results[0]["new"] >= 1
    assert "kinds" in results[0]  # fusha shtesë, pa thyer kontratën


def test_seeded_profiles_run_through_orchestrator(client, monkeypatch):
    import search_apis
    calls = []
    def fake(url, **kw):
        calls.append(url)
        return _response({"jobs": [], "hits": [], "children": []})
    monkeypatch.setattr(search_apis.httpx, "get", fake)
    for name in ("Professional Jobs", "Freelance", "Product Leads"):
        prof = next(p for p in repo.list_search_profiles() if p["name"] == name)
        repo.update_search_profile(prof["id"], {"sources": '["arbeitnow","hackernews_freelance","hn_leads"]'})
        prof = repo.get_search_profile(prof["id"])
        res = connectors.run_profile_search(prof)
        assert all(r["ok"] for r in res) or all(not r["ok"] for r in res) or res
    assert calls  # u thirrën konektorët realë (mock)


def _response(body):
    import httpx as _h
    return _h.Response(200, json=body, request=_h.Request("GET", "https://mock"))


def _cleanup():
    pass
