"""Kontratat e API/routave — ngrirja e sjelljes së tanishme (DoD §34.2)."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    import app  # conftest ka vendosur OPPORTUNITY_DB para importit
    return TestClient(app.app)


def _add_via_api(client, url, title="API Job", **kw):
    payload = {"title": title, "url": url,
               "company": f"ACME-{abs(hash(url)) % 100000}",
               "location": "Berlin", "remote": False, "description": "desc", **kw}
    r = client.post("/api/jobs", json=payload)
    assert r.status_code == 200
    return r.json()


def test_get_home_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    from config import APP_NAME
    assert APP_NAME in r.text  # configurable product branding


def test_api_jobs_contract_keys(client):
    _add_via_api(client, "https://api-test.com/1")
    r = client.get("/api/jobs")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body
    expected = {"id", "title", "company", "url", "source", "location", "remote",
                "salary_text", "score", "status", "status_label", "kind", "first_seen"}
    assert expected <= set(body[0].keys())


def test_api_post_legacy_kind_maps_to_freelance(client):
    res = _add_via_api(client, "https://api-test.com/2", kind="opportunity",
                       opportunity_type="White-label")
    assert res["result"] == "new"
    row = [j for j in client.get("/api/jobs").json() if j["url"] == "https://api-test.com/2"][0]
    assert row["kind"] == "freelance"


def test_api_status_filter_accepts_legacy_value(client):
    _add_via_api(client, "https://api-test.com/3")
    # vlera legacy "e re" → new
    rows = client.get("/api/jobs", params={"status": "e re"}).json()
    assert any(j["url"] == "https://api-test.com/3" for j in rows)
    rows_canon = client.get("/api/jobs", params={"status": "new"}).json()
    assert {j["url"] for j in rows} == {j["url"] for j in rows_canon}


def test_api_update_accepts_legacy_status(client):
    _add_via_api(client, "https://api-test.com/4")
    j = [x for x in client.get("/api/jobs").json() if x["url"] == "https://api-test.com/4"][0]
    r = client.post(f"/api/jobs/{j['id']}/update", json={"status": "aplikuar"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "applied"
    assert body["status_label"] == "aplikuar"


def test_panel_partial_renders(client):
    _add_via_api(client, "https://api-test.com/5", title="Panel Target")
    j = [x for x in client.get("/api/jobs").json() if x["url"] == "https://api-test.com/5"][0]
    r = client.get(f"/api/jobs/{j['id']}/panel")
    assert r.status_code == 200
    assert "Panel Target" in r.text
    assert "Përse përputhet" in r.text


def test_legacy_form_search_route(client, monkeypatch):
    import search_apis
    monkeypatch.setattr(search_apis, "jobicy", lambda **kw: [{
        "source": "jobicy", "external_id": "t1", "url": "https://jobicy.com/jobs/t1",
        "title": "Mocked Job", "company": "M", "location": "Remote (remote)",
        "remote": True, "salary_text": "", "description": "d", "tags": [],
        "posted_at": "",
    }])
    r = client.post("/search/jobicy", data={}, follow_redirects=False)
    assert r.status_code == 303
    assert "/?msg=" in r.headers["location"]
    rows = [x for x in client.get("/api/jobs").json() if x["url"] == "https://jobicy.com/jobs/t1"]
    assert rows and rows[0]["source"] == "jobicy"
    # rifillimi = duplikat
    r2 = client.post("/api/search/jobicy", json={})
    assert r2.status_code == 200
    assert r2.json()["new"] == 0


def test_workspace_filters_still_work(client):
    _add_via_api(client, "https://api-test.com/6", title="Hamburg Rolle")
    assert client.get("/", params={"q": "Hamburg"}).status_code == 200
    assert client.get("/", params={"remote": "remote"}).status_code == 200
    assert client.get("/", params={"mins": 70}).status_code == 200
    assert client.get("/", params={"status": "new", "mode": "freelance"}).status_code == 200
    assert client.get("/", params={"sort": "new"}).status_code == 200


def test_manual_add_and_delete(client):
    r = client.post("/jobs/add", data={
        "title": "Manual Op", "url": "https://api-test.com/7", "company": "K",
        "kind": "freelance", "opportunity_type": "SaaS", "remote": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    j = [x for x in client.get("/api/jobs").json() if x["url"] == "https://api-test.com/7"][0]
    assert j["kind"] == "freelance"
    assert client.post(f"/jobs/{j['id']}/delete", follow_redirects=False).status_code == 303
    assert [x for x in client.get("/api/jobs").json() if x["url"] == "https://api-test.com/7"] == []


def test_old_detail_page_still_renders(client):
    _add_via_api(client, "https://api-test.com/8", title="Full Page Job")
    j = [x for x in client.get("/api/jobs").json() if x["url"] == "https://api-test.com/8"][0]
    r = client.get(f"/jobs/{j['id']}")
    assert r.status_code == 200
    assert "Full Page Job" in r.text
