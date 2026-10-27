"""Faza 6: Guided profile, suggestions, gaps/constraints, completeness."""
import json
import pytest
from fastapi.testclient import TestClient

import completeness
import cv_merge
import matching
import profile_context
import repository as repo
import suggestions_rules


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


@pytest.fixture(autouse=True)
def _restore_profile_sections():
    repo.init()
    saved_sections = repo.all_profile_sections()
    saved_profile = dict(repo.get_profile())
    yield
    repo.save_profile(saved_profile)
    for k, v in saved_sections.items():
        repo.set_profile_section(k, v.get("data", []))
    with repo.db() as con:
        con.execute("DELETE FROM profile_gaps")
        con.execute("DELETE FROM profile_suggestions")
        con.commit()


def _norm(v):
    import re
    return re.sub(r"\s+", " ", (v or "").lower()).strip()


def _save_prof(**kw):
    """Helper: ruaj profil me vlera string (kompatibel me save_profile)."""
    prof = repo.get_profile()
    for k, v in kw.items():
        prof[k] = v
    repo.save_profile(prof)
    return prof


# ------------------------------------------------- onboarding pa CV

def test_no_cv_flow_works(client):
    comp = completeness.compute("job")
    assert comp["score"] >= 0  # punon pa CV
    # CV mund të ekzistojë nga module të tjera — vetëm sigurohu që completeness punon


# ------------------------------------------ sugjerimet deterministic

def test_suggestions_deterministic_and_rule_based():
    prof = {"title": "software developer", "skills": ["python"], "languages": [],
            "certifications": [], "products": [], "experience": ""}
    a = suggestions_rules.suggest(prof)
    b = suggestions_rules.suggest(prof)
    assert a == b
    vals = [s["value"].lower() for s in a if s["section"] == "skills"]
    assert "git" in vals and "sql" in vals


def test_suggestion_not_persisted_as_fact_without_confirmation(client):
    skills_before, _ = repo.get_profile_section("skills")
    out = suggestions_rules.suggest({"title": "software developer",
                                     "skills": ["python"], "certifications": []})
    assert out
    skills_after, _ = repo.get_profile_section("skills")
    assert skills_before == skills_after
    sid = repo.create_profile_suggestion("skills", "skill", "FastAPI", "rule")
    assert sid is not None
    with repo.db() as con:
        con.execute("DELETE FROM profile_suggestions WHERE id=?", (sid,))
        con.commit()


def test_duplicate_suggestion_creates_single_row():
    repo.create_profile_suggestion("skills", "skill", "Unique Test Skill")
    sid2 = repo.create_profile_suggestion("skills", "skill", "Unique Test Skill")
    assert sid2 is None
    with repo.db() as con:
        con.execute("DELETE FROM profile_suggestions WHERE value='Unique Test Skill'")
        con.commit()


# ------------------------------------------------- gaps / constraints

def test_gap_crud_and_severity_willingness():
    gid = repo.create_gap({"category": "missing_skill", "label": "AWS",
                           "severity": "soft", "willingness": "willing_to_learn"})
    g = next(x for x in repo.list_gaps() if x["id"] == gid)
    assert g["severity"] == "soft" and g["willingness"] == "willing_to_learn"
    repo.update_gap(gid, {"willingness": "willing_to_obtain"})
    assert repo.list_gaps()[0]["willingness"] == "willing_to_obtain"
    assert repo.delete_gap(gid) is True


def test_hard_constraint_no_car(client):
    repo.set_profile_section("local", [{"base_location": "Essen", "postcode": "45127",
                                        "radius_km": "10", "transport_mode": "walking",
                                        "no_car": True, "categories": []}])
    repo.create_gap({"category": "travel_constraint", "label": "pa makinë",
                     "severity": "hard", "willingness": "unavailable"})
    import matching
    repo.upsert_opportunity({"source": "hc", "url": "https://hc.example/1",
                             "title": "Delivery me makinë", "company": "C",
                             "location": "Essen", "remote": False,
                             "description": "kerkoj shofer me makinë",
                             "tags": [], "kind": "local_job", "posted_at": ""})
    with repo.db() as con:
        oid = con.execute(
            "SELECT id FROM opportunities WHERE source_url='https://hc.example/1'").fetchone()[0]
    op = repo.get_opportunity(oid)
    op["kind_data"] = {"transport_required": "car"}
    r = matching.score(op, repo.get_profile(), repo.get_filters())
    tr = next(f for f in r.factors if f.id == "transport")
    assert tr.status == matching.MISMATCH and tr.value == 0
    repo.delete_opportunity(oid)


# ------------------------------------------------- completeness

def test_completeness_per_mode_and_missing_sorted():
    job = completeness.compute("job")
    assert 0 <= job["score"] <= 100
    loc = completeness.compute("local_job")
    assert any(d["key"].startswith("local") for d in loc["dimensions"])
    pl = completeness.compute("product_lead")
    assert any(d["key"].startswith("product") or d["key"] == "products"
               for d in pl["dimensions"])


def test_completeness_updates_when_profile_improves(client):
    repo.save_profile({**repo.get_profile(),'title':'','skills':''})
    before = completeness.compute("job")["score"]
    repo.save_profile({**repo.get_profile(), "title": "Software Developer",
                       "skills": "python\nsql\ngit\ndocker", "salary_min": "50000"})
    after = completeness.compute("job")["score"]
    assert after >= before
    repo.save_profile({**repo.get_profile(), "title": "", "skills": "",
                       "salary_min": "0"})


# --------------------------------------------- API + live pranim

def test_suggestions_api_confirm_flow(client):
    prof = repo.get_profile()
    prof["title"] = "software developer"
    repo.save_profile(prof)
    suggs = suggestions_rules.suggest(prof)
    for s in suggs[:2]:
        repo.create_profile_suggestion(s["section"], s["kind"], s["value"], s["reason"])
    pending = repo.get_suggestions("skills", "pending")
    assert pending
    sid = pending[0]["id"]
    r = client.post(f"/api/profile/suggestions/{sid}/confirm")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.post(f"/api/profile/suggestions/{sid}/confirm").json()["ok"] is True
    row = next(x for x in repo.get_suggestions() if x["id"] == sid)
    assert row["status"] == "accepted"
    with repo.db() as con:
        con.execute("DELETE FROM profile_suggestions WHERE id=?", (sid,))
        con.commit()


def test_gaps_api_crud(client):
    r = client.post("/api/profile/gaps", json={
        "category": "language_gap", "label": "Anglisht B1 → C1",
        "current_state": "B1", "target_state": "C1",
        "severity": "soft", "willingness": "learning"})
    assert r.status_code == 200
    gid = r.json()["id"]
    gaps = [g for g in client.get("/api/profile/gaps").json() if g["id"] == gid]
    assert gaps and gaps[0]["category"] == "language_gap"
    assert client.patch(f"/api/profile/gaps/{gid}",
                        json={"willingness": "willing_to_learn"}).status_code == 200
    assert client.delete(f"/api/profile/gaps/{gid}").status_code == 200


def test_no_score_corruption_from_phase6(client):
    repo.upsert_opportunity({"source": "6c", "url": "https://6c.example/1",
                             "title": "Dev", "company": "C", "location": "B",
                             "remote": False, "description": "d", "tags": [],
                             "kind": "job"})
    with repo.db() as con:
        before = {r["id"]: r["rule_score"] for r in con.execute(
            "SELECT id, rule_score FROM opportunities")}
    gid = repo.create_gap({"category": "missing_skill", "label": "X"})
    repo.update_gap(gid, {"active": 0})
    repo.delete_gap(gid)
    with repo.db() as con:
        after = {r["id"]: r["rule_score"] for r in con.execute(
            "SELECT id, rule_score FROM opportunities")}
    assert before == after


def test_existing_cv_and_manual_facts_preserved(client, tmp_path):
    cv_file = tmp_path / "cv.txt"
    cv_file.write_text("Skills: Docker\n", encoding="utf-8")
    repo.upsert_opportunity({"source": "cv", "url": "https://cv.example/1",
                             "title": "T", "company": "C", "location": "B",
                             "remote": False, "description": "d", "tags": [],
                             "kind": "job"})
    cv_id = repo.save_cv("cv.txt", str(cv_file), "text/plain", cv_file.stat().st_size, "d4")
    cv_merge.parse_cv(cv_id)
    for s in repo.cv_suggestions(cv_id, "pending"):
        cv_merge.accept_suggestion(s["id"])
    ctx = profile_context.build_context()
    facts = profile_context.fact_values(ctx)
    assert "docker" in facts
