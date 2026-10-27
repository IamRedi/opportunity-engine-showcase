"""Faza 5D: mundësitë lokale — prefs, kategori, geo, worth, feed, orkestrator."""
import httpx
import json
import pytest

import connectors
import matching
from fastapi.testclient import TestClient

import classifier
import config
import geo
import orchestrator
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


def _seed_local(url="https://local.example/1", title="Ecje qensh Essen",
                hourly="12", postcode="45127", kind="local_job", **kw):
    op = {"source": "local-test", "url": url, "title": title, "company": "",
          "location": "Essen", "remote": False,
          "description": "Ecje qensh çdo mëngjes. 12 €/h pranë stacionit.",
          "tags": [], "kind": kind, "opportunity_type": "dog_walking",
          "hourly_rate": hourly, "postcode": postcode,
          "kind_data": {"postcode": postcode, "schedule": "mëngjes",
                        "transport_required": ""}, "posted_at": ""}
    op.update(kw)
    repo.upsert_opportunity(op)
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]


def _seed(*a, **kw):
    return _seed_local(*a, **kw)


def _prefs(**kw):
    base = {"base_location": "Essen", "postcode": "45127", "radius_km": "10",
            "transport_mode": "walking", "max_travel_min": "30",
            "min_hourly_rate": "10", "categories": ["dog_walking"],
            "available_days": "fundjave", "no_car": False}
    base.update(kw)
    return base


# ------------------------------------------------- prefs & kategoritë

def test_local_prefs_section_roundtrip():
    repo.set_profile_section("local", [_prefs()])
    prefs = repo.get_local_prefs()
    assert prefs["postcode"] == "45127" and prefs["no_car"] is False
    assert "dog_walking" in prefs["categories"]


def test_category_normalization_from_text():
    oid = _seed("https://local.example/norm", title="Gartenhilfe gesucht",
                description="Rasen mähen und Heckenschnitt in Essen. 12 €/h.",
                opportunity_type="")
    row = repo.get_opportunity(oid)
    assert row["opportunity_type"] == "gardening"  # fjalë-kyçe gart/rasen
    assert row["opportunity_type"] in config.LOCAL_CATEGORIES


def test_local_category_labels_exist():
    for cat in config.LOCAL_CATEGORIES:
        assert config.LOCAL_CATEGORY_LABELS.get(cat)


# --------------------------------------------------------- geo / distanca

def test_haversine_and_radius():
    # Essen → Duisburg ~ 30 km
    km = geo.haversine_km(51.4556, 7.0116, 51.4344, 6.7623)
    assert 15 < km < 20
    info = geo.resolve_op_location({"latitude": 51.4344, "longitude": 6.7623},
                                   {"base_coords": (51.4556, 7.0116), "radius_km": 10})
    assert info["status"] == "known" and info["inside_radius"] is False
    info2 = geo.resolve_op_location({"latitude": 51.46, "longitude": 7.02},
                                    {"base_coords": (51.4556, 7.0116), "radius_km": 10})
    assert info2["inside_radius"] is True


def test_postcode_fallback_same_code_zero_distance():
    info = geo.resolve_op_location({"postcode": "45127"},
                                   {"postcode": "45127", "radius_km": 5})
    assert info["status"] == "known" and info["distance_km"] == 0.0


def test_unknown_postcode_is_unknown_not_negative():
    info = geo.resolve_op_location({"postcode": "99999"},
                                   {"postcode": "45127", "radius_km": 5})
    assert info["status"] == "unknown" and info["distance_km"] is None


def test_no_data_postcode_file_is_tolerated_if_missing(tmp_path):
    # abstraksioni: kartela e munguar → UNKNOWN, pa përjashtim
    import geo as g
    old = g.DATA_FILE
    g.DATA_FILE = tmp_path / "missing.json"
    assert g.coords_for("45127") is None
    g.DATA_FILE = old


# --------------------------------------------------------- scoring lokal

def test_local_score_radius_and_pay():
    _seed("https://local.example/s1")
    prefs = _prefs(radius_km="10", min_hourly_rate="10")
    repo.set_profile_section("local", [prefs])
    op = {k: v for k, v in repo.get_opportunity(
        _seed("https://local.example/s1")).items()}
    import matching
    op_full = repo.get_opportunity(_seed("https://local.example/s1"))
    r1 = matching.score(op_full, repo.get_profile(), repo.get_filters(),
                        local_prefs=prefs)
    d = next(f for f in r1.factors if f.id == "distance")
    p = next(f for f in r1.factors if f.id == "pay")
    assert d.status in (matching.MATCH, matching.UNKNOWN)
    assert p.status == matching.MATCH  # 12 €/h ≥ 10 €/h


def test_unknown_pay_is_not_negative():
    import matching
    _seed("https://local.example/s2", hourly="")
    prefs = _prefs(min_hourly_rate="15")
    repo.set_profile_section("local", [prefs])
    oid = _seed("https://local.example/s2b", hourly="")
    op = repo.get_opportunity(oid)
    r = matching.score(op, repo.get_profile(), repo.get_filters())
    p = next(f for f in r.factors if f.id == "pay")
    assert p.status == matching.UNKNOWN
    # me/rreth pagë të panjohur → te fushat e panjohura, jo mismatch
    assert "pay" in r.unknown or p.value is None


def test_no_car_constraint_hard_mismatch(monkeypatch):
    import matching
    prefs = _prefs(no_car=True, transport_mode="walking", min_hourly_rate="10",
                   categories=["dog_walking"], postcode="45127", radius_km="25")
    repo.set_profile_section("local", [prefs])
    op = dict(repo.get_opportunity(_seed("https://local.example/s3")))
    op["kind_data"] = {"transport_required": "car"}
    r = matching.score(op, repo.get_profile(), repo.get_filters())
    tr = next(f for f in r.factors if f.id == "transport")
    assert tr.status == matching.MISMATCH and tr.value == 0


def test_outside_radius_hard_mismatch():
    import matching
    prefs = _prefs(radius_km="5")
    repo.set_profile_section("local", [prefs])
    oid = _seed("https://local.example/s4", postcode="10115")  # Berlin-center ≠ Essen
    op = repo.get_opportunity(oid)
    op["latitude"], op["longitude"] = 52.52, 13.405  # Berlin koordinata
    prefs2 = _prefs(radius_km="5", base_coords=(51.4556, 7.0116))
    r = matching.score(op, repo.get_profile(), repo.get_filters(),
                       local_prefs=prefs2)
    d = next(f for f in r.factors if f.id == "distance")
    assert d.status == matching.MISMATCH and d.value == 0


# --------------------------------------------------------- worth the trip

def test_worth_known_vs_unknown():
    prefs = _prefs(min_hourly_rate="10", radius_km="15")
    op_known = {"hourly_rate": 20, "kind_data": {"travel_min": 10},
                "distance_km": 3}
    info = {"distance_km": 3, "status": "known"}
    w = geo.worth_the_trip(op_known, prefs, info)
    assert w["status"] == "known" and 0 <= w["worth_score"] <= 100
    assert any(e["factor"] == "pay" for e in w["evidence"])
    # pa pagë dhe pa distancë → UNKNOWN
    op_unknown = {"kind_data": {}}
    w2 = geo.worth_the_trip(op_unknown, prefs, {"status": "unknown", "distance_km": None})
    assert w2["worth_score"] is None and w2["status"] == "unknown"


def test_worth_penalizes_below_floor():
    prefs = _prefs(min_hourly_rate="20")
    low = geo.worth_the_trip({"hourly_rate": 8, "kind_data": {}},
                             prefs, {"distance_km": 1, "status": "known"})
    high = geo.worth_the_trip({"hourly_rate": 30, "kind_data": {}},
                              prefs, {"distance_km": 1, "status": "known"})
    assert low["worth_score"] < high["worth_score"]


# ------------------------------------------------- konektori local_feed

def test_local_feed_normalization(monkeypatch):
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Ecje qensh Essen — 12 €/h</title>
        <link>https://lokal.example/1</link>
        <description>Dog walking mëngjes në 45127. Orari: mëngjes.</description>
        <pubDate>Mon, 01 Sep 2026 08:00:00 GMT</pubDate></item>
      <item><title>Kujdes maces pasdite</title>
        <link>https://lokal.example/2</link>
        <description>Cat sitting 45131, 10 €/Std</description></item>
    </channel></rss>"""
    def fake_get(url, **kw):
        class R:
            text = rss
            def raise_for_status(self): pass
            def json(self): return {}
        return R()
    monkeypatch.setattr(search_apis.httpx, "get", fake_get)
    items = search_apis.local_feed(["https://lokal.example/feed.xml"])
    assert len(items) == 2
    it = items[0]
    assert it["kind"] == "local_job" and it["source"] == "local_feed"
    assert it["opportunity_type"] == "dog_walking"
    assert it["hourly_rate"] == 12.0 and it["postcode"] == "45127"
    assert it["kind_data"]["schedule"] == "mëngjes"
    assert items[1]["opportunity_type"] == "cat_sitting"


def test_local_feed_connector_unconfigured_fail_soft():
    repo.set_setting("local_feed_urls", "")
    c = connectors.REGISTRY["local_feed"]
    assert c.is_configured() is False
    res = c.search(None)
    assert res.error and res.items == []


# ------------------------------------------------- orchestrator + profil

def test_local_profile_run_via_orchestrator(monkeypatch):
    rss = ('<?xml version="1.0"?><rss version="2.0"><channel>'
           '<item><title>Kujdes kujdestarie — 14 €/h, 45127, pasdite</title>'
           '<link>https://lokal.example/orch1</link>'
           '<description>Household help, dog walking nearby.</description></item>'
           '</channel></rss>')
    def fake_get(url, **kw):
        class R:
            text = rss
            def raise_for_status(self): pass
            def json(self): return {}
        return R()
    monkeypatch.setattr(search_apis.httpx, "get", fake_get)
    repo.set_setting("local_feed_urls", "https://lokal.example/feed.xml")
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Local / Mini Jobs")
    res = orchestrator.run(profile_id=prof["id"], max_items=10)
    assert res["ok"] is True and res["sources_ok"] == 1
    assert res["kinds_new"].get("local_job", 0) >= 1
    rows = repo.list_opportunities(kind="local_job", source="local_feed")
    assert rows and rows[0]["opportunity_type"] in config.LOCAL_CATEGORIES


def test_local_profile_without_feeds_reports_cleanly(client):
    repo.set_setting("local_feed_urls", "")
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Local / Mini Jobs")
    r = client.post("/api/orchestrator/run", json={"profile_id": prof["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["sources_failed"] == 1
    assert any("feed" in e["error"].lower() for e in body["errors"])


def test_manual_local_import(client):
    r = client.post("/jobs/add", data={
        "title": "Ecje qensh në Rüttenscheid", "url": "https://manual.local/1",
        "kind": "local_job", "local_category": "dog_walking",
        "postcode": "45127", "hourly_rate": "14", "schedule": "mëngjes",
        "description": "Ecje për qenin Max.",
    }, follow_redirects=False)
    assert r.status_code == 303
    with repo.db() as con:
        row = con.execute(
            "SELECT * FROM opportunities WHERE source_url='https://manual.local/1'").fetchone()
    assert row["kind"] == "local_job" and row["opportunity_type"] == "dog_walking"
    kd = json.loads(row["kind_data"])
    assert kd["postcode"] == "45127" and kd["schedule"] == "mëngjes"
    # pa kompani → L3 anashkalohet në mënyrë të sigurt (dedup me URL); l3_key bosh
    # pastrim
    con = repo.db()
    con.execute("DELETE FROM opportunities WHERE source_url='https://manual.local/1'")
    con.commit(); con.close()


def test_import_url_local_kind(client):
    html = "<html><head><title>Kujdes maces Dortmund</title></head></html>"
    class FakeAC:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            class R:
                text = html
                def raise_for_status(self): pass
            return R()
    import httpx as _h
    old = _h.AsyncClient
    _h.AsyncClient = FakeAC
    try:
        r = client.post("/api/import/url",
                        json={"url": "https://local-import.example/1", "kind": "local_job"})
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "local_job"
    finally:
        _h.AsyncClient = old


# --------------------------------- pa mutim & legacy

def test_no_mutation_of_apps_products_scores(client, monkeypatch):
    oid = _seed("https://local.example/m1")
    with repo.db() as con:
        before = dict(con.execute(
            "SELECT rule_score, ai_score, final_score FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    prods_before = repo.list_products(active_only=False)
    apps_before = repo.list_applications(oid)
    monkeypatch.setattr(connectors, "run_source_search",
                        lambda sid, q=None, profile=None, limit=None:
                        {"ok": True, "source": sid, "discovered": 0, "new": 0,
                         "dup": 0, "kinds": {}, "error": "", "run_id": 0})
    res = orchestrator.run(max_items=5)  # stub — zero thirrje reale
    with repo.db() as con:
        after = dict(con.execute(
            "SELECT rule_score, ai_score, final_score FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    assert before == after
    assert repo.list_products(active_only=False) == prods_before
    assert repo.list_applications(oid) == apps_before


def test_legacy_jobs_freelance_leads_behavior(client, monkeypatch):
    import search_apis
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [{
        "source": "jobicy", "external_id": "5d1", "url": "https://5d.example/1",
        "title": "Legacy Check", "company": "L", "location": "Remote (remote)",
        "remote": True, "salary_text": "", "description": "d", "tags": [],
        "posted_at": "", "kind": "job"}])
    r = client.post("/api/search/jobicy", json={})
    assert r.status_code == 200 and r.json()["new"] >= 1
    row = [x for x in client.get("/api/jobs").json() if x["url"] == "https://5d.example/1"][0]
    assert row["kind"] == "job"


def test_local_mode_feed_renders(client):
    oid = _seed("https://local.example/ui1")
    r = client.get("/", params={"mode": "local"})
    assert r.status_code == 200
    assert "Lokale" in r.text
    panel = client.get(f"/api/jobs/{oid}/panel").text
    assert "Të dhënat lokale" in panel
