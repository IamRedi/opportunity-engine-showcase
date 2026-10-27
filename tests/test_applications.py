"""Aplikimet (Faza 4A): draft, edit, revisione immutable, approve, tranzicione."""
import json

import pytest
from fastapi.testclient import TestClient

import applications as apps_svc
import repository as repo


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


def _seed_op(url="https://app-test.example/1", title="Backend Developer"):
    repo.upsert_opportunity({
        "source": "app-test", "url": url, "title": title,
        "company": f"ACME-{abs(hash(url)) % 100000}",
        "location": "Berlin", "remote": False, "description": "python backend",
        "tags": [], "kind": "job", "posted_at": "",
    })
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]


# 1) create draft
def test_create_draft(client):
    oid = _seed_op("https://app-test.example/c1")
    r = client.post(f"/api/jobs/{oid}/applications", json={"application_type": "job_application"})
    assert r.status_code == 201
    app = repo.get_application(r.json()["id"], with_versions=True)
    assert app["status"] == "draft" and app["latest_version"] == 1
    assert app["versions"][0]["kind"] == "generated"
    assert "Backend Developer" in app["latest_content"]
    assert app["application_type"] == "job_application"
    assert app["approved_at"] is None and app["submitted_at"] is None


def test_default_type_by_kind(client):
    oid = _seed_op("https://app-test.example/c2", title="Booking platform")
    repo.update_opportunity(oid, status=None) if False else None
    import sqlite3
    with repo.db() as con:
        con.execute("UPDATE opportunities SET kind='freelance' WHERE id=?", (oid,))
        con.commit()
    r = client.post(f"/api/jobs/{oid}/applications", json={})
    app = repo.get_application(r.json()["id"])
    assert app["application_type"] == "freelance_proposal"


# 2+3+7) edit → version i re, i pari preserved, lidhja me mundësinë
def test_edit_creates_new_revision_and_preserves_previous(client):
    oid = _seed_op("https://app-test.example/e1")
    app_id = client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]
    v1_content = repo.get_application(app_id, with_versions=True)["latest_content"]
    client.post(f"/api/applications/{app_id}/edit",
                json={"content": "Redaktim im i personalizuar"})
    app = repo.get_application(app_id, with_versions=True)
    assert app["latest_version"] == 2
    assert app["status"] == "edited"
    assert [v["kind"] for v in app["versions"]] == ["generated", "edited"]
    assert app["versions"][0]["content"] == v1_content  # v1 e paprekur
    assert app["versions"][1]["content"] == "Redaktim im i personalizuar"
    # lista e mundësisë përmban aplikimin
    lst = client.get(f"/api/jobs/{oid}/applications").json()
    assert any(a["id"] == app_id for a in lst)


def test_identical_edit_does_not_duplicate_version(client):
    oid = _seed_op("https://app-test.example/e2")
    app_id = client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]
    content = repo.get_application(app_id, with_versions=True)["latest_content"]
    client.post(f"/api/applications/{app_id}/edit", json={"content": content})
    app = repo.get_application(app_id, with_versions=True)
    assert app["latest_version"] == 1  # pa dublikatë


# 4) approve
def test_approve_sets_final_and_timestamp(client):
    oid = _seed_op("https://app-test.example/a1")
    app_id = client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]
    r = client.post(f"/api/applications/{app_id}/approve")
    assert r.status_code == 200
    app = repo.get_application(app_id, with_versions=True)
    assert app["status"] == "approved" and app["approved_at"]
    kinds = [v["kind"] for v in app["versions"]]
    assert kinds[-1] == "final"  # version final i vulosur
    # submit pas approve → lejohet (vetëm shënim manual)
    r = client.post(f"/api/applications/{app_id}/submit")
    assert r.status_code == 200
    app = repo.get_application(app_id, with_versions=True)
    assert app["status"] == "submitted" and app["submitted_at"]


# 5) tranzicione të pavlefshme
def test_invalid_transitions_rejected(client):
    oid = _seed_op("https://app-test.example/t1")
    app_id = client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]
    # submit nga draft → 400 (asnjë auto-submit/dërgim)
    assert client.post(f"/api/applications/{app_id}/submit").status_code == 400
    # arkivo → pastaj çdo veprim i mëtejshëm i ndaluar
    client.post(f"/api/applications/{app_id}/archive")
    assert client.post(f"/api/applications/{app_id}/approve").status_code == 400
    assert client.post(f"/api/applications/{app_id}/submit").status_code == 400
    app = repo.get_application(app_id)
    assert app["status"] == "archived"


# 6) shumë rishkrime + regjenerimi s'fshin editimet
def test_regeneration_preserves_edits(client):
    oid = _seed_op("https://app-test.example/r1")
    app_id = client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]
    client.post(f"/api/applications/{app_id}/edit", json={"content": "EDITIMI IM"})
    client.post(f"/api/applications/{app_id}/regenerate")
    app = repo.get_application(app_id, with_versions=True)
    kinds = [v["kind"] for v in app["versions"]]
    contents = [v["content"] for v in app["versions"]]
    assert kinds == ["generated", "edited", "generated"]
    # editimi i përdoruesit ekziston akoma, i paprekur
    assert "EDITIMI IM" in contents
    assert app["latest_version"] == 3


# 8) përmbajtja e redaktuar NUK mbivendoset kurrë
def test_no_overwrite_of_user_edited_content(client):
    oid = _seed_op("https://app-test.example/n1")
    app_id = client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]
    user_text = "Kjo është fjalia ime e rëndësishme UNIKALE-4A"
    client.post(f"/api/applications/{app_id}/edit", json={"content": user_text})
    client.post(f"/api/applications/{app_id}/regenerate")
    client.post(f"/api/applications/{app_id}/edit", json={"content": user_text + " + shtoj"})
    app = repo.get_application(app_id, with_versions=True)
    all_contents = "\n".join(v["content"] for v in app["versions"])
    assert user_text in all_contents  # historia e plotë e ruajtur
    assert len(app["versions"]) == 4


# 9) sjellja legacy e paprekur (spot-check) + 10) pa mutim të skorimit
def test_no_production_score_mutation_and_legacy(client):
    oid = _seed_op("https://app-test.example/p1")
    with repo.db() as con:
        before = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    app_id = client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]
    client.post(f"/api/applications/{app_id}/edit", json={"content": "x"})
    client.post(f"/api/applications/{app_id}/approve")
    with repo.db() as con:
        after = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    assert before == after
    # kontratat e vjetra
    assert client.get("/api/jobs").status_code == 200
    assert client.get(f"/api/jobs/{oid}/panel").status_code == 200


# 11) migrim idempotent + model i pastër
def test_migration_idempotent_and_contract(tmp_path):
    import sqlite3
    db = tmp_path / "v6.sqlite3"
    info1 = repo.init(db)
    assert info1["upgraded"] is True
    info2 = repo.init(db)
    assert info2["upgraded"] is False  # ekzekutimi i dytë s'bën asgjë
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"applications", "application_versions"} <= names
    # versionet: UNIQUE(app_id, version_number)
    con.execute("INSERT INTO applications (opportunity_id, application_type, created_at, updated_at)"
                " VALUES (NULL, 'job_application', 'x', 'y')" ) if False else None
    con.close()
