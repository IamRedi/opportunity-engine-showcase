"""Gjeneratori sipas platformës (Faza 4B): garda, fallback, revisione, metadata.

Të gjitha me transport mock — zero kredenciale, zero rrjet të vërtetë.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import ai_service
import applications as apps_svc
import generator
import repository as repo

MOCK_URL = "http://ai-mock.invalid"


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


@pytest.fixture(scope="module", autouse=True)
def _db():
    repo.init()  # idempotent — tabelat e testit para çdo thirrjeje repo
    repo.save_profile({**repo.get_profile(), "skills": "python\nsql\ngit",
                       "languages": "", "products": "", "experience": ""})
    yield


def _response(body: dict):
    return httpx.Response(200, json={"response": json.dumps(body)},
                          request=httpx.Request("POST", MOCK_URL))


def _seed(url="https://gen-test.example/1", title="Backend Developer", kind="job"):
    repo.upsert_opportunity({
        "source": "gen-test", "url": url, "title": title,
        "company": f"ACME-{abs(hash(url)) % 100000}",
        "location": "Berlin", "remote": False, "description": "python backend " * 40,
        "tags": [], "kind": kind, "posted_at": "",
    })
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]


def _enable(model="test-model"):
    repo.set_setting("ai_provider", "ollama")
    repo.set_setting("ai_model", model)
    repo.set_setting("ai_base_url", MOCK_URL)


def _disable():
    repo.set_setting("ai_provider", "disabled")


def _valid_draft(facts=None, missing=None):
    return {"subject": "Aplikim — Backend Developer",
            "body": "Përshëndetje, po aplikoj me eksperiencën time relevante. " * 3,
            "tone": "professional", "platform_style": "application",
            "used_profile_facts": facts or [], "missing_context": missing or []}


@pytest.fixture(scope="module", autouse=True)
def _after():
    yield
    _disable()


# --------------------------------- ndërtimi i kontekstit sipas platformës

def test_platform_styles_differ(monkeypatch):
    _enable()
    captured = {}
    def fake_build(task, op, profile, cfg, extra=None):
        captured["extra"] = extra
        return "PROMPT"
    monkeypatch.setattr(ai_service, "build_input", fake_build)
    for style in ("linkedin", "kleinanzeigen", "freelance", "application"):
        generator.generate(_seed(), style=style, language="de")
    instrs = captured and None
    # çdo style jep instruksione të ndryshme
    seen = set()
    for style in generator.STYLE_INSTRUCTIONS:
        seen.add(generator.STYLE_INSTRUCTIONS[style])
    assert len(seen) == len(generator.STYLE_INSTRUCTIONS)


def test_type_maps_to_default_style():
    assert generator.TYPE_DEFAULT_STYLE["job_application"] == "application"
    assert generator.TYPE_DEFAULT_STYLE["freelance_proposal"] == "freelance"
    assert generator.TYPE_DEFAULT_STYLE["product_outreach"] == "product"
    assert generator.TYPE_DEFAULT_STYLE["local_message"] == "local"


def test_missing_context_deterministic():
    profile = {"skills": [], "experience": "", "languages": [], "products": [],
               "location": "", "summary": "", "title": "Dev", "name": "X",
               "email": "", "phone": ""}
    op = {"description": "short", "title": "T"}
    m = generator.missing_context(profile, op, "job_application")
    assert any("aftësitë" in x for x in m)
    assert any("eksperienc" in x for x in m)
    assert any("shkurtër" in x for x in m)
    m2 = generator.missing_context(profile, op, "product_outreach")
    assert any("katalogu" in x for x in m2)


# --------------------------------------------- garda e hallucinimit

def test_unsupported_facts_blocked(monkeypatch):
    _enable()
    oid = _seed("https://gen-test.example/h1")
    # AI pretendon 'docker kubernetes' — JO në profil → duhet fallback template
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["docker kubernetes"])))
    res = generator.generate(opp_id=oid)
    assert res["ok"] is True and res["fallback"] is True
    assert "docker kubernetes" in res["unsupported_claims"]
    assert res["meta"]["kind"] == "template"  # teksti i shpikur NUK persistohet
    app = repo.get_application(res["application_id"], with_versions=True)
    assert app["latest_content"].startswith("Përshëndetje")  # template, jo AI-invented


def test_supported_facts_pass(monkeypatch):
    prof = repo.get_profile() | {"skills": "python\nsql\ngit"}
    repo.save_profile(prof)
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["python", "sql"])))
    res = generator.generate(opp_id=_seed("https://gen-test.example/ok1"))
    assert res["ok"] is True and res["fallback"] is False
    assert res["meta"]["kind"] == "ai"
    assert res["meta"]["provider"] == "ollama"
    _disable()


# ------------------------------------------------- malformed & fallback

def test_malformed_ai_output_falls_back(monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"body": "pa fushat e tjera"}))
    res = generator.generate(opp_id=_seed("https://gen-test.example/m1"))
    assert res["fallback"] is True and res["ok"] is True
    _disable()


def test_ai_disabled_uses_template_fallback(client):
    _disable()
    oid = _seed("https://gen-test.example/d1")
    res = generator.generate(opp_id=oid, tone="friendly", language="sq")
    assert res["ok"] is True and res["fallback"] is True
    assert "çaktivizuar" in res["reason"]
    app = repo.get_application(res["application_id"], with_versions=True)
    assert app["versions"][0]["kind"] == "generated"
    assert app["versions"][0]["content"].startswith("Përshëndetje")


def test_endpoint_generate_disabled_fail_soft(client):
    _disable()
    oid = _seed("https://gen-test.example/d2")
    r = client.post(f"/api/jobs/{oid}/generate", json={"tone": "friendly", "language": "sq"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["fallback"] is True


# ----------------------------------------------- revisione & metadata

def test_generation_creates_new_immutable_revision(client, monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["python"])))
    oid = _seed("https://gen-test.example/v1")
    app_id = client_create(client, oid)
    client.post(f"/api/applications/{app_id}/edit", json={"content": "EDITIMI IM UNIK"})
    client.post(f"/api/applications/{app_id}/generate", json={})
    app = repo.get_application(app_id, with_versions=True)
    kinds = [v["kind"] for v in app["versions"]]
    assert kinds == ["generated", "edited", "generated"]
    assert "EDITIMI IM UNIK" in "\n".join(v["content"] for v in app["versions"])
    # metadata e generator-it të fundit e ruajtur
    assert app["generator"]["kind"] == "ai"
    assert app["generator"]["model"] == "test-model"
    assert app["generator"]["prompt_version"] == ai_service.PROMPT_VERSION
    _disable()


def client_create(client, oid):
    return client.post(f"/api/jobs/{oid}/applications", json={}).json()["id"]


def test_approved_final_never_overwritten_by_generation(client, monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["python"])))
    oid = _seed("https://gen-test.example/v2")
    app_id = client_create(client, oid)
    client.post(f"/api/applications/{app_id}/approve")
    before_final = [v for v in repo.get_application(app_id, with_versions=True)["versions"]
                    if v["kind"] == "final"]
    client.post(f"/api/applications/{app_id}/generate", json={})
    app = repo.get_application(app_id, with_versions=True)
    after_final = [v for v in app["versions"] if v["kind"] == "final"]
    assert before_final == after_final  # final-i immutable
    assert app["latest_version"] > before_final[0]["version_number"]


def test_no_automatic_submission_via_generation(client, monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["python"])))
    oid = _seed("https://gen-test.example/v3")
    app_id = client_create(client, oid)
    client.post(f"/api/applications/{app_id}/generate", json={})
    app = repo.get_application(app_id)
    assert app["status"] in ("draft", "edited", "approved")  # kurrë submitted automatik
    assert app["submitted_at"] is None
    _disable()


def test_no_score_mutation_from_generation(client, monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["python"])))
    oid = _seed("https://gen-test.example/v4")
    with repo.db() as con:
        before = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    client.post(f"/api/jobs/{oid}/generate", json={})
    with repo.db() as con:
        after = dict(con.execute(
            "SELECT rule_score, ai_score, final_score, match_factors FROM opportunities WHERE id=?",
            (oid,)).fetchone())
    assert before == after
    _disable()


def test_apps_section_in_panel(client, monkeypatch):
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["python"])))
    oid = _seed("https://gen-test.example/v5")
    client.post(f"/api/jobs/{oid}/generate", json={})
    r = client.get(f"/api/jobs/{oid}/panel")
    assert r.status_code == 200
    assert "Gjenero me AI" in r.text
    assert "js-app-lang" in r.text and "js-app-tone" in r.text


def test_profile_completeness_gate_blocks_ai_generation():
    """E matur në 3D: profil bosh → AI shpik sfondin → gjenerimi abstain."""
    _disable()  # porta e profilit ndizet para kontrollit disabled
    _enable()
    repo.save_profile({**repo.get_profile(), "skills": ""})  # profil bosh → porta
    oid = _seed("https://gen-test.example/gate1")
    res = generator.generate(opp_id=oid)
    assert res["fallback"] is True
    assert "profil i pamjaftueshëm" in res["reason"]
    assert res["meta"]["kind"] == "template"
    _disable()


def test_profile_gate_passes_with_skills(monkeypatch):
    prof = repo.get_profile() | {"skills": "python\nsql\ngit"}
    repo.save_profile(prof)
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response(_valid_draft(facts=["python"])))
    oid = _seed("https://gen-test.example/gate2")
    res = generator.generate(opp_id=oid)
    assert res["ok"] is True and res["fallback"] is False
    assert res["meta"]["kind"] == "ai"
    _disable()
