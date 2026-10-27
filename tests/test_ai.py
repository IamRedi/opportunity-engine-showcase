"""Shtresa AI (Faza 3B): provider abstrakt, fail-soft, cache, schema-validim.

Asnjë test s'kërkon kredenciale apo rrjet të vërtetë — transporti mock-ohet.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import ai_service
import matching
import repository as repo

MOCK_URL = "http://ai-mock.invalid"


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


def _op(**kw):
    base = {
        "source": "ai-test", "url": f"https://ai-test.example/{kw.get('title', 'x')}",
        "title": "Python Developer", "company": "ACME", "location": "Berlin",
        "remote": False, "description": "python backend", "tags": [], "kind": "job",
        "posted_at": "", "salary_min": None,
    }
    base.update(kw)
    return base


def _seed():
    """Siguro një mundësi në DB (test_ai ekzekutohet i pari alfabetikisht)."""
    repo.upsert_opportunity(_op(url="https://ai-test.example/seed-1"))


def _enable(provider="ollama", model="test-model", base_url=MOCK_URL):
    repo.set_setting("ai_provider", provider)
    repo.set_setting("ai_model", model)
    repo.set_setting("ai_base_url", base_url)


def _disable():
    repo.set_setting("ai_provider", "disabled")


def _response(body: dict):
    """httpx.Response valide e mock-uar (me request, që raise_for_status të punojë)."""
    return httpx.Response(200, json={"response": json.dumps(body)},
                          request=httpx.Request("POST", MOCK_URL))


# --------------------------------------------------- disabled (default)

def test_disabled_by_default_and_fail_soft(client):
    _disable()
    cfg = ai_service.get_config()
    assert cfg["provider"] == "disabled"
    out = ai_service.run_task("explain", _op())
    assert out.ok is False and "çaktivizuar" in out.error
    _seed()
    rows = client.get("/api/jobs").json()
    jid = next(j["id"] for j in rows if j["source"] == "ai-test")
    r = client.post(f"/api/ai/enrich/{jid}/explain")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_status_endpoint(client):
    r = client.get("/api/ai/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] in ai_service.PROVIDERS
    assert set(body["tasks"]) == set(ai_service.SCHEMAS)
    assert "rule_score" in body["note"]


# ------------------------------------------------------- transport gabime

def test_provider_unreachable_is_fail_soft(monkeypatch):
    _enable(base_url="http://127.0.0.1:1")  # port i mbyllur
    def refuse(url, **kw):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(ai_service.httpx, "post", refuse)
    out = ai_service.run_task("explain", _op())
    assert out.ok is False and "transport" in out.error
    _disable()


def test_timeout_is_fail_soft(monkeypatch):
    _enable()
    def slow(url, **kw):
        raise httpx.TimeoutException("timed out")
    monkeypatch.setattr(ai_service.httpx, "post", slow)
    out = ai_service.run_task("extract_requirements", _op())
    assert out.ok is False
    _disable()


# ------------------------------------------------- validim i përgjigjes

def test_malformed_response_rejected(monkeypatch):
    _enable()
    calls = {"n": 0}
    def bad(url, **kw):
        calls["n"] += 1
        return httpx.Response(200, text="nuk ka JSON këtu",
                              request=httpx.Request("POST", MOCK_URL))
    monkeypatch.setattr(ai_service.httpx, "post", bad)
    out = ai_service.run_task("extract_requirements", _op())
    assert out.ok is False
    assert calls["n"] == 1  # gabimi i parsimit s'provokon retry
    assert ("JSON" in out.error) or ("përgjigje" in out.error)
    _disable()


def test_schema_violation_rejected(monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"skills": "jo-liste", "languages": []}))
    out = ai_service.run_task("extract_requirements", _op())
    assert out.ok is False and "skema" in out.error
    # score jashtë [0,100] refuzohet
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"score": 250, "matched": [], "missing": []}))
    out = ai_service.run_task("semantic_match", _op())
    assert out.ok is False
    _disable()


def test_valid_response_validated_and_meta(monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post", lambda url, **kw: _response({
        "skills": ["python", "sql"], "languages": ["gjermanisht"],
        "seniority": None, "remote": None, "min_years": None}))
    out = ai_service.run_task("extract_requirements", _op())
    assert out.ok is True
    assert out.data["skills"] == ["python", "sql"]
    assert out.meta["provider"] == "ollama" and out.meta["model"] == "test-model"
    assert out.meta["prompt_version"] == ai_service.PROMPT_VERSION
    assert not out.cached
    _disable()


# ------------------------------------------------------------- cache

def test_cache_hit_avoids_repeat_call(monkeypatch):
    _enable()
    calls = {"n": 0}
    def good(url, **kw):
        calls["n"] += 1
        return _response({"summary": "s", "pros": [], "cons": []})
    monkeypatch.setattr(ai_service.httpx, "post", good)
    op = _op()
    r1 = ai_service.run_task("explain", op)
    r2 = ai_service.run_task("explain", op)
    assert r1.ok and r2.ok
    assert r1.cached is False and r2.cached is True
    assert calls["n"] == 1  # e dyta erdhi nga cache
    _disable()


def test_cache_invalidates_on_content_profile_model(monkeypatch):
    _enable()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"summary": "s", "pros": [], "cons": []}))
    op = _op()
    assert ai_service.run_task("explain", op).ok
    # përmbajtja e mundësisë ndryshon → kyçe e re → thirrje e re
    r = ai_service.run_task("explain", _op(description="ndryshuar"))
    assert r.cached is False
    # profili ndryshon → kyçe e re
    prof = repo.get_profile() | {"skills": ["ndryshim-profil"]}
    r2 = ai_service.run_task("explain", op, profile=prof)
    assert r2.ok and r2.cached is False
    # modeli ndryshon → kyçe e re
    _enable(model="another-model")
    out = ai_service.run_task("explain", op)
    assert out.ok and out.cached is False
    assert out.meta["model"] == "another-model"
    _disable()


def test_force_bypasses_cache(monkeypatch):
    _enable()
    calls = {"n": 0}
    def good(url, **kw):
        calls["n"] += 1
        return _response({"summary": "s", "pros": [], "cons": []})
    monkeypatch.setattr(ai_service.httpx, "post", good)
    op = _op(description="përmbajtje unike për force-test")  # kyçe e freskët
    ai_service.run_task("explain", op)
    r = ai_service.run_task("explain", op, force=True)
    assert r.cached is False and calls["n"] == 2
    _disable()


# --------------------------------- deterministik mbetet burimi i së vërtetës

def test_deterministic_scoring_unaffected_by_ai_failure(monkeypatch):
    _seed()
    repo.set_setting("ai_provider", "ollama")
    def boom(url, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(ai_service.httpx, "post", boom)
    op = _op(title="Python Developer")
    r1 = matching.score(op)
    out = ai_service.run_task("semantic_match", op)
    assert out.ok is False
    r2 = matching.score(op)
    assert r1.factor_dict() == r2.factor_dict()
    # rule_score/ai_score mbeten të ndarë: ai_score mbetet NULL pa enrich
    with repo.db() as con:
        row = con.execute("SELECT ai_score FROM opportunities LIMIT 1").fetchone()
    assert row["ai_score"] is None
    _disable()


def test_config_endpoints_roundtrip(client):
    r = client.post("/api/ai/config", json={"provider": "ollama", "model": "m1", "timeout": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["status"]["provider"] == "ollama"
    assert body["status"]["model"] == "m1"
    # provider i panjohur refuzohet (mbetet i vjetri)
    r = client.post("/api/ai/config", json={"provider": "skynet"})
    assert r.json()["status"]["provider"] == "ollama"
    client.post("/api/ai/config", json={"provider": "disabled"})
    assert client.get("/api/ai/status").json()["enabled"] is False


def test_unknown_task_rejected():
    out = ai_service.run_task("world_domination", _op())
    assert out.ok is False and "panjohur" in out.error
