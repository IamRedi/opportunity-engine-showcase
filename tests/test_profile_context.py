"""ProfileContext + CV (Faza 4C): seksione, fakte me dëshmi, garda, migrim."""
import httpx
import json

import pytest
from fastapi.testclient import TestClient

import matching
import profile_context
import repository as repo


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


@pytest.fixture(scope="module", autouse=True)
def _db():
    repo.init()
    # izolim: 4D-testet lënë zëra CV të aprovuar te seksionet — i rikthejmë pas
    with repo.db() as con:
        con.execute("DELETE FROM cv_suggestions")  # izolim nga 4D leftovers
        con.commit()
    saved_sections = repo.all_profile_sections()
    yield
    for k, v in saved_sections.items():
        repo.set_profile_section(k, v.get("data", []))
    with repo.db() as con:
        con.execute("DELETE FROM cv_suggestions")
        con.commit()
    for k, v in saved_sections.items():
        repo.set_profile_section(k, v.get("data", []))
    with repo.db() as con:
        con.execute("DELETE FROM cv_suggestions")
        con.commit()
    for k, v in saved_sections.items():
        repo.set_profile_section(k, v.get("data", []))
    with repo.db() as con:
        con.execute("DELETE FROM cv_suggestions")
        con.commit()


def _entry(name, **kw):
    e = {"name": name}
    e.update(kw)
    return e


# ------------------------------------------------- migrimi legacy → seksione

def test_legacy_profile_seeded_into_sections():
    # vendos fusha legacy dhe rivetit seksionet (simulim migrimi)
    prof = repo.get_profile() | {"skills": "python\nsql",
                                 "languages": "gjermanisht – B2\nanglisht – C1",
                                 "products": "BusinessSite RC"}
    repo.save_profile(prof)
    with repo.db() as con:
        con.execute("DELETE FROM profile_sections")
        con.commit()
    n = repo._seed_profile_sections
    with repo.db() as con:
        count = repo._seed_profile_sections(con)
    assert count >= 3
    skills, _ = repo.get_profile_section("skills")
    assert {"name": "python", "level": ""} in skills
    langs, _ = repo.get_profile_section("languages")
    assert {"name": "gjermanisht", "level": "B2"} in langs  # "Gjuhë – nivel" i ndarë
    prods, _ = repo.get_profile_section("products")
    assert prods[0]["name"] == "BusinessSite RC"


def test_seed_idempotent_never_overwrites():
    repo.set_profile_section("skills", [{"name": "CUSTOM", "level": ""}])
    with repo.db() as con:
        repo._seed_profile_sections(con)
    skills, _ = repo.get_profile_section("skills")
    assert skills == [{"name": "CUSTOM", "level": ""}]  # s'u mbishkrua
    repo.set_profile_section("skills", [{"name": "python", "level": ""}])


# ------------------------------------------------- ProfileContext & fakte

def test_context_facts_are_evidence_backed():
    repo.set_profile_section("skills", [{"name": "python", "level": "i avancuar"}])
    repo.set_profile_section("products", [{"name": "BusinessSite RC", "kind": "white-label",
                                           "capabilities": ["website"], "link": ""}])
    ctx = profile_context.build_context()
    keys = {f["key"] for f in ctx["facts"]}
    assert "skill:python" in keys and "product:businesssite rc" in keys
    for f in ctx["facts"]:
        assert f["source"] in profile_context.SECTION_SCHEMAS or f["source"] in ("experience_text", "summary")
    # garda e gjeneratorit lexon vetëm vlera me dëshmi
    vals = profile_context.fact_values(ctx)
    assert "python" in vals and "businesssite rc" in vals
    assert "docker kubernetes" not in vals


def test_empty_context_has_no_invented_facts():
    prof = repo.get_profile() | {"skills": "", "languages": "", "products": "",
                                 "experience": "", "summary": ""}
    repo.save_profile(prof)
    repo.set_profile_section("skills", [])
    repo.set_profile_section("languages", [])
    repo.set_profile_section("products", [])
    ctx = profile_context.build_context(prof)
    assert ctx["facts"] == []


# ------------------------------------------------- CRUD & validimi

def test_section_crud_api(client):
    r = client.put("/api/profile/sections/skills",
                   json={"entries": [{"name": "python", "level": "advanced"}]})
    assert r.status_code == 200
    data = client.get("/api/profile/sections/skills").json()
    assert data["data"] == [{"name": "python", "level": "advanced"}]
    # fushë e detyrueshme mungon → 400
    assert client.put("/api/profile/sections/skills",
                      json={"entries": [{"level": "x"}]}).status_code == 400
    # seksion i panjohur → 404
    assert client.put("/api/profile/sections/hacker",
                      json={"entries": []}).status_code == 404


# --------------------------------------------- gjeneratori me ProfileContext

def test_generator_claims_guard_uses_context(client, monkeypatch):
    repo.set_profile_section("skills", [{"name": "python", "level": ""}])
    import ai_service

    def _resp(facts):
        return httpx.Response(200, json={"response": json.dumps({
            "subject": "S", "body": "Tekst i gjatë i mjaftueshëm për validim. " * 3,
            "tone": "professional", "platform_style": "application",
            "used_profile_facts": facts, "missing_context": []})},
            request=httpx.Request("POST", "http://ai-mock.invalid"))

    repo.set_setting("ai_provider", "ollama")
    repo.set_setting("ai_model", "m")
    repo.set_setting("ai_base_url", "http://ai-mock.invalid")
    repo.set_profile_section("skills", [
        {"name": "python", "level": ""}, {"name": "sql", "level": ""},
        {"name": "git", "level": ""}])
    repo.save_profile({**repo.get_profile(), "skills": "python\nsql\ngit"})

    repo.upsert_opportunity({"source": "g", "url": "https://pc.example/1", "title": "Dev",
                             "company": "C", "location": "B", "remote": False,
                             "description": "python " * 30, "tags": [], "kind": "job"})
    with repo.db() as con:
        oid = con.execute("SELECT id FROM opportunities WHERE source_url='https://pc.example/1'").fetchone()[0]

    # fakt i mbështetur → AI kalon
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _resp(["python"]))
    res = generator_generate(oid)
    assert res["fallback"] is False and res["meta"]["kind"] == "ai"

    # fakt i shpikur → bllokohet, fallback template
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _resp(["docker kubernetes"]))
    res = generator_generate(oid)
    assert res["fallback"] is True and "docker kubernetes" in res["unsupported_claims"]
    _disable()


def generator_generate(oid):
    import generator
    return generator.generate(opp_id=oid)


def _disable():
    repo.set_setting("ai_provider", "disabled")


# ------------------------------------------------------------- CV

def test_cv_upload_validate_and_delete(client):
    # tip i palejuar
    r = client.post("/api/profile/cv", files={"file": ("virus.exe", b"xx", "application/x-exe")})
    assert r.status_code == 400
    # pdf i vogël valid
    r = client.post("/api/profile/cv", files={"file": ("cv.pdf", b"%PDF-1.4 fake", "application/pdf")})
    assert r.status_code == 200
    meta = client.get("/api/profile/cv").json()
    assert meta["filename"] == "cv.pdf" and meta["parse_status"] == "pending"
    # shfaqet te context
    ctx = client.get("/api/profile/context").json()
    assert ctx["cv"]["status"] == "pending" and ctx["cv"]["filename"] == "cv.pdf"
    # fshi
    assert client.delete("/api/profile/cv").status_code == 200
    assert client.get("/api/profile/cv").json()["status"] == "none"
    ctx = client.get("/api/profile/context").json()
    assert ctx["cv"]["status"] == "none"


def test_cv_upload_requires_file(client):
    r = client.post("/api/profile/cv")
    assert r.status_code == 400


# --------------------------------- kompatibiteti & pa-mutimi i prodhimit

def test_legacy_consumers_still_work(client):
    # matching/score vazhdon me fushat legacy
    repo.save_profile({**repo.get_profile(), "skills": "python"})
    oid = None
    repo.upsert_opportunity({"source": "pc2", "url": "https://pc.example/2", "title": "Python Dev",
                             "company": "C", "location": "B", "remote": False,
                             "description": "python", "tags": [], "kind": "job"})
    with repo.db() as con:
        oid = con.execute("SELECT id FROM opportunities WHERE source_url='https://pc.example/2'").fetchone()[0]
    j = repo.get_opportunity(oid)
    assert isinstance(j["rule_score"], int)
    # paneli still renders
    assert client.get(f"/api/jobs/{oid}/panel").status_code == 200


def test_context_crud_refreshes_scores_using_current_skills(client):
    repo.save_profile({**repo.get_profile(), "skills": "python"})
    repo.upsert_opportunity({"source": "pc3", "url": "https://pc.example/3", "title": "Dev",
                             "company": "C", "location": "B", "remote": False,
                             "description": "python", "tags": [], "kind": "job"})
    with repo.db() as con:
        oid = con.execute("SELECT id FROM opportunities WHERE source_url='https://pc.example/3'").fetchone()[0]
        before = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    client.put("/api/profile/sections/skills", json={"entries": [{"name": "java", "level": ""}]})
    with repo.db() as con:
        after = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    assert after['final_score'] < before['final_score']
    assert 'java' in json.loads(after['match_factors'])['missing']
    assert repo.get_profile()['skills']==['java']
    assert before['ai_score']==after['ai_score']  # no fabricated semantic score
