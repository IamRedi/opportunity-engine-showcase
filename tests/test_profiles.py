"""Profilet e kërkimit: CRUD, API, ekzekutim, validim (spec §7)."""
import pytest
from fastapi.testclient import TestClient

import repository as repo


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


def test_seeded_profiles_exist():
    names = {p["name"] for p in repo.list_search_profiles()}
    assert {"Professional Jobs", "Freelance", "Product Leads", "Local / Mini Jobs"} <= names
    prof = next(p for p in repo.list_search_profiles() if p["name"] == "Freelance")
    assert prof["kinds"] == ["freelance"] and prof["active"] is True


def test_profile_crud_repository():
    pid = repo.create_search_profile({
        "name": "Test Profil", "kinds": '["job","freelance"]',
        "keywords": '["python"]', "salary_min": 50000, "min_match": 50,
        "sources": '["jobicy"]', "active": 1,
    })
    p = repo.get_search_profile(pid)
    assert p["name"] == "Test Profil"
    assert p["kinds"] == ["job", "freelance"]
    assert p["keywords"] == ["python"]
    assert p["salary_min"] == 50000 and p["min_match"] == 50
    assert p["sources"] == ["jobicy"]

    assert repo.update_search_profile(pid, {"active": 0}) is True
    assert repo.get_search_profile(pid)["active"] is False
    assert all(p["active"] for p in repo.list_search_profiles(active_only=True))
    assert repo.delete_search_profile(pid) is True
    assert repo.get_search_profile(pid) is None
    assert repo.delete_search_profile(pid) is False


def test_api_profiles_crud(client):
    r = client.post("/api/profiles", json={"name": "API Profil", "kinds": ["job"],
                                           "keywords": "rust\navtokad", "min_match": 70})
    assert r.status_code == 201
    pid = r.json()["id"]

    lst = client.get("/api/profiles").json()
    p = next(x for x in lst if x["id"] == pid)
    assert p["keywords"] == ["rust", "avtokad"]        # tekst shumë-rresht → listë
    assert p["min_match"] == 70

    r = client.patch(f"/api/profiles/{pid}", json={"active": False, "salary_min": 60000})
    assert r.status_code == 200
    p = next(x for x in client.get("/api/profiles").json() if x["id"] == pid)
    assert p["active"] is False and p["salary_min"] == 60000

    # vlera të pavlefshme refuzohen
    assert client.patch(f"/api/profiles/{pid}", json={"salary_min": "abc"}).status_code == 400
    assert client.post("/api/profiles", json={"name": ""}).status_code == 400
    assert client.post("/api/profiles", json={"kinds": ["job"]}).status_code == 400  # pa emër
    # lloje/burime të panjohur filtrohen, jo crash
    r = client.patch(f"/api/profiles/{pid}", json={"kinds": ["job", "hacker"], "sources": ["jobicy", "nsa"]})
    assert r.status_code == 200
    p = next(x for x in client.get("/api/profiles").json() if x["id"] == pid)
    assert p["kinds"] == ["job"] and p["sources"] == ["jobicy"]

    assert client.delete(f"/api/profiles/{pid}").status_code == 200
    assert client.delete(f"/api/profiles/{pid}").status_code == 404


def test_api_profile_run_and_runs_history(client, monkeypatch):
    import search_apis
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [{
        "source": "jobicy", "external_id": "pr1", "url": "https://prof-run.example/1",
        "title": "Profile Run Item", "company": "C", "location": "Remote (remote)",
        "remote": True, "salary_text": "", "description": "d", "tags": [], "posted_at": "",
    }])
    pid = repo.create_search_profile({"name": "Run Profil", "sources": '["jobicy"]'})
    r = client.post(f"/api/profiles/{pid}/run")
    assert r.status_code == 200
    body = r.json()
    assert body["ok_sources"] == 1 and body["new_total"] == 1
    assert body["results"][0]["source"] == "jobicy"

    runs = client.get("/api/runs", params={"limit": 10}).json()
    mine = [x for x in runs if x["profile_id"] == pid]
    assert mine and mine[0]["new_count"] == 1 and mine[0]["status"] == "ok"
    repo.delete_search_profile(pid)


def test_profile_panels_render(client):
    r = client.get("/api/profiles/new/panel")
    assert r.status_code == 200 and "Profil i ri" in r.text
    pid = repo.create_search_profile({"name": "PanelProvë"})
    r = client.get(f"/api/profiles/{pid}/panel")
    assert r.status_code == 200 and "PanelProvë" in r.text
    assert 'name="min_match"' in r.text and 'name="kinds"' in r.text
    repo.delete_search_profile(pid)


def test_workspace_includes_profiles_context(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="profiles-pop"' in r.text
    assert "Professional Jobs" in r.text
