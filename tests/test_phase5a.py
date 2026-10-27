"""Faza 5A: burime freelance & product-lead, dedup L3, import URL, profilet."""
import httpx
import pytest
from fastapi.testclient import TestClient

import connectors
import repository as repo
import search_apis


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


@pytest.fixture(scope="module", autouse=True)
def _db():
    repo.init()
    yield


def _fake(payload):
    """httpx.Response i mock-uar për search_apis._get (ka raise_for_status/json)."""
    return httpx.Response(200, json=payload, request=httpx.Request("GET", "https://mock"))


def _hn_search_payload():
    return {"hits": [{"objectID": "999", "title": "Ask HN: Freelancer? Seeking freelancer?"}]}


def _hn_item_payload(comments):
    return {"children": comments}


def _hn_comment(cid, text, created="2026-09-01T10:00:00Z"):
    return {"id": cid, "text": text, "created_at": created}


# ------------------------------------------ normalizimi i konektorëve

def test_hackernews_freelance_normalization(monkeypatch):
    def fake_get(url, **kw):
        if "search_by_date" in url:
            return _fake(_hn_search_payload())
        return _fake(_hn_item_payload([
            _hn_comment(101, "SEEKING FREELANCER: fullstack developer for a React + Node project. Remote EU only."),
            _hn_comment(102, "<i>Short</i>"),
        ]))
    monkeypatch.setattr(search_apis.httpx, "get", fake_get)
    items = search_apis.hackernews_freelance()
    assert len(items) == 1  # komenti i shkurtër hidhet
    it = items[0]
    assert it["kind"] == "freelance"
    assert it["source"] == "hackernews"
    assert it["external_id"] == "hn-101"
    assert it["url"].endswith("item?id=101")
    assert "react" in it["tags"] and "node" in it["tags"]
    assert it["remote"] is True
    assert it["posted_at"].startswith("2026-09-01")


def test_hackernews_leads_seek_filter(monkeypatch):
    def fake_get(url, **kw):
        if "search_by_date" in url:
            return _fake(_hn_search_payload())
        return _fake(_hn_item_payload([
            _hn_comment(201, "Freelancer here: fullstack dev available for work."),
            _hn_comment(202, "We are looking for a developer to build a booking website for our dental clinic. Remote ok."),
            _hn_comment(203, "Need an AI chatbot integration for our support desk."),
        ]))
    monkeypatch.setattr(search_apis.httpx, "get", fake_get)
    items = search_apis.hackernews_leads()
    assert len(items) == 2
    assert all(i["kind"] == "product_lead" for i in items)
    assert all(i["source"] == "hn_leads" for i in items)
    lead = [i for i in items if "booking" in i["description"]][0]
    assert lead["opportunity_type"] == "Website"
    assert lead["kind_data"]["detected_need"]


def test_remotive_contract_filter(monkeypatch):
    payload = {"jobs": [
        {"id": 1, "url": "https://r.example/1", "title": "Contract Dev", "company_name": "A",
         "candidate_required_location": "EMEA", "job_type": "contract", "salary": "",
         "description": "<p>d1</p>", "tags": ["go"], "publication_date": "2026-09-01T00:00:00"},
        {"id": 2, "url": "https://r.example/2", "title": "Fulltime Dev", "company_name": "B",
         "candidate_required_location": "EU", "job_type": "full_time", "salary": "",
         "description": "d2", "tags": [], "publication_date": "2026-09-01T00:00:00"},
    ]}
    monkeypatch.setattr(search_apis.httpx, "get",
                        lambda url, **kw: _fake(payload) if "remote-jobs" in url else _fake({"jobs": []}))
    items = search_apis.remotive_contract()
    assert len(items) == 1
    assert items[0]["kind"] == "freelance"
    assert items[0]["title"] == "Contract Dev"
    # legacy remotive pa filter: të gjitha, kind jo freelance
    legacy = search_apis.remotive()
    assert len(legacy) == 2 and "kind" not in legacy[0]


def test_jobicy_contract_filter(monkeypatch):
    payload = {"jobs": [
        {"id": 5, "url": "https://j.example/5", "jobTitle": "Contract PM", "companyName": "J",
         "jobGeo": "Worldwide", "jobType": ["Contract"], "salaryMin": None, "salaryMax": None,
         "jobDescription": "pm work", "pubDate": "2026-09-02T00:00:00Z"},
        {"id": 6, "url": "https://j.example/6", "jobTitle": "FT Dev", "companyName": "K",
         "jobGeo": "EU", "jobType": ["Full-Time"], "salaryMin": None, "salaryMax": None,
         "jobDescription": "dev", "pubDate": "2026-09-02T00:00:00Z"},
    ]}
    monkeypatch.setattr(search_apis.httpx, "get",
                        lambda url, **kw: _fake(payload) if "remote-jobs" in url else _fake({"jobs": []}))
    items = search_apis.jobicy(contract_only=True)
    assert len(items) == 1 and items[0]["kind"] == "freelance"


# ------------------------------------- izolimi & profilet e reja

def test_source_failure_isolation(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(search_apis.httpx, "get", boom)
    monkeypatch.setattr(search_apis.httpx, "post", boom)
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["hackernews_freelance","jobicy_contract"]'})
    prof = repo.get_search_profile(prof["id"])
    results = connectors.run_profile_search(prof)
    by_src = {r["source"]: r for r in results}
    assert by_src["hackernews_freelance"]["ok"] is False
    assert by_src["jobicy_contract"]["ok"] is False  # të dyja këtu vetëm mock boom
    # dhe run-i regjistrohet me gabim pa rrëzuar procesin
    runs = repo.recent_search_runs(limit=4)
    assert any(r["status"] == "error" for r in runs)


def test_freelance_profile_run_stores_kind(client, monkeypatch):
    def hn_dispatch(url, **kw):
        if "search_by_date" in url:
            return _fake(_hn_search_payload())
        return _fake(_hn_item_payload([
            _hn_comment(301, "Looking for a Django developer for a 3-month project. Remote.")]))
    monkeypatch.setattr(search_apis.httpx, "get", hn_dispatch)
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["hackernews_freelance"]'})
    prof = repo.get_search_profile(prof["id"])
    res = connectors.run_profile_search(prof)
    assert res[0]["ok"] is True and res[0]["new"] >= 1
    rows = repo.list_opportunities(kind="freelance", source="hackernews")
    assert rows and rows[0]["kind"] == "freelance"


def test_product_lead_profile_run(client, monkeypatch):
    def hn_dispatch(url, **kw):
        if "search_by_date" in url:
            return _fake(_hn_search_payload())
        return _fake(_hn_item_payload([
            _hn_comment(401, "We need an automation for our invoicing workflow.")]))
    monkeypatch.setattr(search_apis.httpx, "get", hn_dispatch)
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Product Leads")
    repo.update_search_profile(prof["id"], {"sources": '["hn_leads"]'})
    prof = repo.get_search_profile(prof["id"])
    res = connectors.run_profile_search(prof)
    assert res[0]["ok"] is True and res[0]["new"] >= 1
    rows = repo.list_opportunities(kind="product_lead", source="hn_leads")
    assert rows and all(r["kind"] == "product_lead" for r in rows)


def test_no_mutation_of_apps_scores_profile(client, monkeypatch):
    def hn_dispatch(url, **kw):
        if "search_by_date" in url:
            return _fake(_hn_search_payload())
        return _fake(_hn_item_payload([
            _hn_comment(501, "Looking for a React developer.")]))
    monkeypatch.setattr(search_apis.httpx, "get", hn_dispatch)
    with repo.db() as con:
        before = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"])
                  for r in con.execute("SELECT id, rule_score, ai_score, final_score FROM opportunities")}
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["hackernews_freelance"]'})
    connectors.run_profile_search(repo.get_search_profile(prof["id"]))
    with repo.db() as con:
        after = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"])
                 for r in con.execute("SELECT id, rule_score, ai_score, final_score FROM opportunities")}
    # rule/ai/final të pandryshuara (5A s'preket pikët; rreshtat e re janë të reja)
    assert {k: v for k, v in before.items()} == {k: v for k, v in after.items() if k in before}


# ------------------------------------------------- import URL

def test_manual_url_import(client, monkeypatch):
    html = ("<html><head><title>Old Title</title>"
            "<meta property='og:description' content='Klienti kërkon platformë rezervimesh'>"
            "</head><body></body></html>")
    def fake_fetch_fail_if_used(**kw):
        raise AssertionError(" shouldn't be called")
    captured = {}
    class FakeAsyncClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            captured["url"] = url
            class R:
                text = html
                def raise_for_status(self): pass
            return R()
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    r = client.post("/api/import/url", json={
        "url": "https://client.example/booking-need", "kind": "product_lead",
        "company": "Dental Clinic X", "location": "Essen"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "product_lead" and body["source"] == "url_import"
    rows = repo.list_opportunities(kind="product_lead", source="url_import")
    row = [x for x in rows if x["source_url"] == "https://client.example/booking-need"][0]
    assert row["title"] == "Old Title"  # og:title mungon → <title>
    assert "rezervimesh" in row["description"]


def test_import_url_validation(client):
    assert client.post("/api/import/url", json={"url": "ftp://x"}).status_code == 400
    assert client.post("/api/import/url", json={"url": "https://ok.example/x", "kind": "nope"}).status_code == 400


# ------------------------------------------------- dedup L2/L3

def test_cross_source_dedup_same_canonical_url():
    a = {"source": "remotive", "external_id": "x1", "url": "https://same.example/j1?utm=a",
         "title": "Go Dev", "company": "G", "location": "Berlin", "remote": True,
         "description": "d", "tags": [], "kind": "job"}
    b = {"source": "jobicy", "external_id": "x2", "url": "https://same.example/j1",
         "title": "Go Dev (jobicy)", "company": "G", "location": "Berlin", "remote": True,
         "description": "d më i pasur", "tags": ["go"], "kind": "job"}
    assert repo.upsert_opportunity(a) == "new"
    assert repo.upsert_opportunity(b) == "dup"
    rows = [r for r in repo.list_opportunities() if "same.example" in r["canonical_url"]]
    assert len(rows) == 1  # një mundësi
    import sqlite3
    with repo.db() as con:
        n = con.execute("SELECT COUNT(*) FROM opportunity_sources WHERE url LIKE 'https://same.example%'").fetchone()[0]
    assert n == 2  # dy identitete burimi të ruajtura


def test_l3_dedup_same_title_org_location_and_desc():
    same_desc = "_identik për repost test_ " * 3
    a = {"source": "s1", "url": "https://l3.example/a", "title": "Senior Python Developer",
         "company": "Müller GmbH", "location": "München", "description": same_desc,
         "tags": [], "kind": "job"}
    b = {"source": "s2", "url": "https://other.example/b", "title": "senior  python  developer!!",
         "company": "müller gmbh", "location": "München", "description": same_desc,
         "tags": [], "kind": "job"}
    assert repo.upsert_opportunity(a) == "new"
    res = repo.upsert_opportunity(b)
    assert res == "dup"  # L3 safe: titull+org+vend+përshkrim identik → repost
    assert len(repo.list_opportunities(source="s1")) == 1


def test_l3_same_title_but_diff_desc_stays_new():
    a = {"source": "s5", "url": "https://l3c.example/a", "title": "Developer",
         "company": "Alfa", "location": "Bonn", "description": "varianti A unik",
         "tags": [], "kind": "job"}
    b = {"source": "s6", "url": "https://l3c.example/b", "title": "Developer",
         "company": "Alfa", "location": "Bonn", "description": "varianti B krejt ndryshe",
         "tags": [], "kind": "job"}
    assert repo.upsert_opportunity(a) == "new"
    assert repo.upsert_opportunity(b) == "new"  # përshkrim i ndryshëm → jo merge


def test_l3_does_not_merge_different_orgs():
    a = {"source": "s3", "url": "https://l3b.example/a", "title": "Developer", "company": "Alfa",
         "location": "Bonn", "description": "d", "tags": [], "kind": "job"}
    b = {"source": "s4", "url": "https://l3b.example/b", "title": "Developer", "company": "Beta",
         "location": "Bonn", "description": "d", "tags": [], "kind": "job"}
    assert repo.upsert_opportunity(a) == "new"
    assert repo.upsert_opportunity(b) == "new"  # org e ndryshme → jo L3 merge


# -------------------------------------- pa-mutimi & legacy


def test_registry_has_eight_sources_with_new_metadata():
    assert set(connectors.REGISTRY) >= {
        "arbeitnow", "remotive", "jobicy", "adzuna",
        "hackernews_freelance", "remotive_contract", "jobicy_contract", "hn_leads",
        "local_feed"}
    for cid in ("hackernews_freelance", "remotive_contract", "jobicy_contract", "hn_leads"):
        c = connectors.REGISTRY[cid]
        h = c.health()
        assert h["ok"] is True and h["requires_key"] is False


def test_legacy_job_connectors_still_work(client, monkeypatch):
    import search_apis
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [{
        "source": "jobicy", "external_id": "legacy1", "url": "https://jobicy.com/jobs/legacy1",
        "title": "Legacy Job", "company": "L", "location": "Remote (remote)",
        "remote": True, "salary_text": "", "description": "d", "tags": [],
        "posted_at": ""}])
    r = client.post("/api/search/jobicy", json={})
    assert r.status_code == 200 and r.json()["new"] >= 1
    rows = [x for x in client.get("/api/jobs").json() if x["url"] == "https://jobicy.com/jobs/legacy1"]
    assert rows[0]["kind"] == "job"  # legacy = job kind, jo freelance
