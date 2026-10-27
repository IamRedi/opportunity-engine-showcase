"""Faza 5B: lentë shumë-fish, product-fit deterministik, profilet sipas lens."""
import pytest
from fastapi.testclient import TestClient

import classifier
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


def _seed(url, title="Booking platform for clinics", kind="freelance",
          description=None, **kw):
    if description is None:
        description = ("We are looking for a developer to build a booking website with "
                       "appointments and a simple CRM integration. Ref: " + url)
    repo.upsert_opportunity({
        "source": "lens-test", "url": url, "title": title, "company": "Klinikat A",
        "location": "Berlin", "remote": True, "description": description,
        "tags": [], "kind": kind, **kw,
    })
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]


def _products():
    return [
        {"name": "BusinessSite RC", "kind": "white-label",
         "capabilities": ["website", "booking", "appointments", "social media", "customizable"],
         "link": ""},
        {"name": "FlowAI Toolkit", "kind": "toolkit",
         "capabilities": ["automation", "ai", "chatbot", "workflow"],
         "link": ""},
        {"name": "Ledger CRM", "kind": "saas",
         "capabilities": ["crm", "customer management", "workflow"],
         "link": ""},
    ]


# --------------------------------------------- lentë shumë-fish, pa dup

def test_one_opportunity_multiple_lenses_no_duplication():
    oid = _seed("https://lens.example/1")
    lenses = classifier.lenses_for_ids([oid])[oid]
    assert "product_lead" in lenses          # sinjalet: booking, website, crm
    # lentë "freelance" është implicit te baza kind (s'ruhet dyfish)
    base = repo.get_opportunity(oid)
    assert base["kind"] == "freelance"       # baza e pandryshuar
    # një rresht i vetëm në DB për këtë URL
    rows = [r for r in repo.list_opportunities() if r["source_url"] == "https://lens.example/1"]
    assert len(rows) == 1


def test_reupsert_does_not_duplicate_lens_rows():
    oid = _seed("https://lens.example/2")
    import sqlite3
    with repo.db() as con:
        n1 = con.execute("SELECT COUNT(*) FROM opportunity_lenses WHERE opportunity_id=?", (oid,)).fetchone()[0]
    # ri-upsert (dup) s'shton rreshta lens-i
    repo.upsert_opportunity({"source": "lens-test", "url": "https://lens.example/2",
                             "title": "Booking platform for clinics", "company": "Klinikat A",
                             "location": "Berlin", "remote": True,
                             "description": "We are looking for a developer to build a booking website.",
                             "tags": [], "kind": "freelance"})
    with repo.db() as con:
        n2 = con.execute("SELECT COUNT(*) FROM opportunity_lenses WHERE opportunity_id=?", (oid,)).fetchone()[0]
    assert n1 == n2


# --------------------------------- profilet filter me lens (pa duplikim)

def test_product_lead_profile_finds_by_lens(client):
    oid = _seed("https://lens.example/3")
    rows = repo.list_opportunities(kind="product_lead", source="lens-test")
    ids = [r["id"] for r in rows]
    assert oid in ids  # gjetur përmes lentës, megjithëse kind=freelance


def test_freelance_profile_still_finds_same_opportunity(client):
    oid = _seed("https://lens.example/4")
    rows = repo.list_opportunities(kind="freelance", source="lens-test")
    assert oid in [r["id"] for r in rows]


# ------------------------------------------------- product fit deterministik

def _fit(oid):
    op = repo.get_opportunity(oid)
    return product_fit.evaluate(op, products=_products())


def test_product_fit_deterministic():
    oid = _seed("https://lens.example/5")
    r1 = _fit(oid)
    r2 = _fit(oid)
    assert r1 == r2


def test_multiple_products_ranked():
    oid = _seed("https://lens.example/6")
    res = _fit(oid)
    cands = res["candidates"]
    assert len(cands) == 3  # tre produkte në katalog
    assert cands == sorted(cands, key=lambda c: (-c["fit_score"], c["product_name"]))
    top = cands[0]
    assert top["product_name"] == "BusinessSite RC"  # booking+website+crm → fit më i lartë
    assert 0 <= top["fit_score"] <= 100


def test_matched_missing_unknown_and_customization():
    oid = _seed("https://lens.example/7")
    res = _fit(oid)
    top = res["candidates"][0]
    assert top["matched"]                          # kategori të përbashkëta
    assert isinstance(top["missing"], list)
    # 5C: unknown tregon aspekte të padrejtuara (gjuhët/deployment/industritë)
    assert all(isinstance(u, str) and u for u in top["unknown"])
    assert top["customization_level"] in ("low", "medium", "high")
    assert top["evidence"] and all("category" in e for e in top["evidence"])


def test_hard_mismatch_flag():
    # nevojë që asnjë produkt nuk e mbulon → hard mismatch te kandidatët e dobët
    oid = _seed("https://lens.example/8",
                description="We need payroll automation for 200 employees.")
    res = _fit(oid)
    assert res["abstain"] is False
    weak = [c for c in res["candidates"] if c["hard_mismatch"]]
    assert weak  # ekziston të paktën një produkt pa mbulim
    for c in weak:
        assert c["fit_score"] == 0 and c["missing"]


def test_abstain_when_no_need_signals():
    oid = _seed("https://lens.example/9", title="Simple listing",
                description="A generic posting without any specific capability mention.")
    res = _fit(oid)
    assert res["abstain"] is True
    assert res["candidates"] == []                 # UNKNOWN ≠ negative


def test_no_invented_product_capabilities():
    oid = _seed("https://lens.example/10",
                description="We need a CRM with lead management.")
    res = _fit(oid)
    for c in res["candidates"]:
        for e in c["evidence"]:
            # çdo dëshmi i përket një kategorie të deklaruar të produktit
            assert e["product_capability_hits"], "aftësi e shpikur!"
        # Ledger CRM ka crm; BusinessSite jo → nuk mund të ketë crm te matched
    bs = [c for c in res["candidates"] if c["product_name"] == "BusinessSite RC"][0]
    assert "crm" not in bs["matched"]


# --------------------------------------- backfill & kompatibiteti & pa-mutim

def test_backfill_attaches_lenses_to_existing_hn_rows():
    # simulo rresht HN pa lentë (gjendja para 5B)
    repo.upsert_opportunity({
        "source": "hackernews", "external_id": "bf1",
        "url": "https://news.ycombinator.com/item?id=bf1",
        "title": "We need a white-label booking website", "company": "",
        "location": "Remote", "remote": True,
        "description": "We need a white-label booking website with automation for our clinics.",
        "tags": [], "kind": "freelance",
    })
    with repo.db() as con:
        oid = con.execute(
            "SELECT id FROM opportunities WHERE source_url='https://news.ycombinator.com/item?id=bf1'"
        ).fetchone()[0]
        con.execute("DELETE FROM opportunity_lenses WHERE opportunity_id=?", (oid,))
    n = classifier.backfill_all()
    lenses = classifier.lenses_for_ids([oid])[oid]
    assert "product_lead" in lenses           # white-label + booking + automation
    # lentë "freelance" = baza kind (implicit)
    assert n >= 0


def test_lens_backfill_idempotent():
    before = repo.get_profile()  # pa ndryshim
    n1 = classifier.backfill_all()
    n2 = classifier.backfill_all()
    assert n2 == 0  # i dyti s'shton asgjë
    assert repo.get_profile() == before


def test_no_mutation_of_applications_scores(client):
    oid = _seed("https://lens.example/11")
    with repo.db() as con:
        before = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    apps_before = repo.list_applications(oid)
    # vlerëso fit + backfill — asnjëri nuk prek prodhimin
    _fit(oid)
    classifier.backfill_all()
    with repo.db() as con:
        after = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    assert before == after
    assert repo.list_applications(oid) == apps_before


def test_panel_shows_product_fit_block(client):
    repo.set_profile_section("products", [
        {"name": "BusinessSite RC", "kind": "white-label",
         "capabilities": ["website", "booking", "crm", "customizable"], "link": ""}])
    oid = _seed("https://lens.example/12")
    r = client.get(f"/api/jobs/{oid}/panel")
    assert r.status_code == 200
    assert "Mundësi produkti" in r.text
    assert "BusinessSite RC" in r.text


def test_workspace_lens_badge_renders(client):
    oid = _seed("https://lens.example/13")
    r = client.get("/?mode=all")
    assert r.status_code == 200
    assert "Leads" in r.text  # badge i lentës product_lead te rreshti freelance
