"""Eval kontrollor (Faza 3C): mostër deterministe, limite, guard, raport.

Të gjitha me provider mock — zero kredenciale, zero rrjet të vërtetë.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import ai_service
import evaluation
import repository as repo

MOCK_URL = "http://ai-mock.invalid"


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


def _response(body: dict):
    return httpx.Response(200, json={"response": json.dumps(body)},
                          request=httpx.Request("POST", MOCK_URL))


def _seed_ops(n=30):
    """Mbush DB me mundësi me rule_score të shpërndarë në 3 shtresa.

    Profil 'python' + filtra vetëm-remote japin: Developer→lart, Java
    (python vetëm në tekst)→mes, Baker→ulët (workmode mismatch + freskësi).
    """
    repo.save_profile({**repo.get_profile(), "title": "Developer",
                       "skills": "python", "languages": "", "products": "",
                       "location": ""})
    repo.save_filters({**repo.get_filters(), "keywords": [], "exclude_keywords": [],
                       "remote_ok": 1, "onsite_ok": 0, "hybrid_ok": 0})
    for i in range(n):
        score_kind = i % 3
        title = {0: "Python Developer", 1: "Senior Java Architect", 2: "Baker"}[score_kind]
        repo.upsert_opportunity({
            "source": "eval-test", "external_id": f"e{i}",
            "url": f"https://eval-test.example/{i}",
            "title": title, "company": f"C{i}", "location": "Berlin" if i % 2 else "Romë",
            "remote": False, "description": "python backend" if score_kind != 2 else "bukë",
            "tags": [], "kind": "job", "posted_at": "",
        })


def _enable_local(model="test-model"):
    repo.set_setting("ai_provider", "ollama")
    repo.set_setting("ai_model", model)
    repo.set_setting("ai_base_url", MOCK_URL)


def _disable():
    repo.set_setting("ai_provider", "disabled")


@pytest.fixture(scope="module", autouse=True)
def _setup():
    repo.init()  # idempotent — tabela e testit krijohet para çdo thirrjeje repo
    _seed_ops()
    yield
    _disable()


# ------------------------------------------------------- mostëra

def test_sample_deterministic_and_bounded():
    a = evaluation.select_sample(25, seed=42)
    b = evaluation.select_sample(25, seed=42)
    assert a == b
    assert len(a) == 25
    assert all(isinstance(x, int) for x in a)
    c = evaluation.select_sample(25, seed=7)
    assert c != a  # seed tjetër → mostër tjetër (pothuajse sigurt)
    assert len(set(a)) == len(a)  # pa duplikate
    assert evaluation.select_sample(100, seed=1) == evaluation.select_sample(30, seed=1)


def test_sample_is_stratified_over_scores():
    ids = evaluation.select_sample(30, seed=42)
    with repo.db() as con:
        scores = {r["id"]: r["rule_score"] for r in con.execute(
            f"SELECT id, rule_score FROM opportunities WHERE id IN ({','.join('?'*len(ids))})", ids)}
    vals = [scores[i] or 0 for i in ids]
    assert any(v >= 70 for v in vals) and any(v < 30 for v in vals)  # shtresa të pranishme


# ---------------------------------------------------- guard i kostos

def test_paid_provider_blocked_without_optin(monkeypatch):
    repo.set_setting("ai_provider", "openai-compatible")
    repo.set_setting("ai_model", "gpt-x")
    repo.set_setting("ai_base_url", "https://api.openai.com/v1")
    # me celes fake → bllokohet vetëm për opt-in
    monkeypatch.setenv("AI_API_KEY", "fake-key-for-guard-test")
    res = evaluation.start_run("explain", sample_size=5)
    assert res["blocked"] and "opt-in" in res["reason"]
    # pa celes → bllokohet për celes
    monkeypatch.delenv("AI_API_KEY")
    res = evaluation.start_run("explain", sample_size=5)
    assert res["blocked"] and "celes" in res["reason"]
    # asnjë run s'është regjistruar për këtë provider
    runs = repo.list_eval_runs()
    assert not any(r["provider"] == "openai-compatible" for r in runs)
    _disable()


def test_paid_provider_blocked_without_key(monkeypatch):
    repo.set_setting("ai_provider", "openai-compatible")
    repo.set_setting("ai_base_url", "https://api.openai.com/v1")
    import os
    os.environ.pop("AI_API_KEY", None)
    res = evaluation.start_run("explain", sample_size=5)
    assert res["blocked"] and "celes" in res["reason"]
    _disable()


def test_disabled_provider_blocked():
    _disable()
    res = evaluation.start_run("explain", sample_size=5)
    assert res["blocked"] and "çaktivizuar" in res["reason"]


def test_local_provider_allowed_without_optin(monkeypatch):
    _enable_local()
    def good(url, **kw):
        return _response({"summary": "s", "pros": [], "cons": []})
    monkeypatch.setattr(ai_service.httpx, "post", good)
    res = evaluation.start_run("explain", sample_size=3, max_calls=3)
    assert res["blocked"] is False and res["ok"] == 3
    _disable()


# ------------------------------------------------- ekzekutimi i run-it

def test_run_complete_with_metadata_and_totals(monkeypatch):
    _enable_local()
    def good(url, **kw):
        return _response({"score": 55, "matched": ["python"], "missing": ["docker"], "notes": "n"})
    monkeypatch.setattr(ai_service.httpx, "post", good)
    res = evaluation.start_run("semantic_match", sample_size=6, seed=42, max_calls=6,
                               force=True)  # pa cache → output-e të reja të garantuara
    assert res["blocked"] is False and res["status"] == "complete"
    run = repo.get_eval_run(res["run_id"], with_items=True)
    assert run["calls"] == 6 and run["ok_count"] == 6 and run["failed_count"] == 0
    assert run["provider"] == "ollama" and run["model"] == "test-model"
    assert run["seed"] == 42 and len(run["opportunity_ids"]) == 6
    item = run["items"][0]
    assert item["status"] == "ok" and item["output"]["score"] == 55
    assert item["meta"]["prompt_version"] == ai_service.PROMPT_VERSION
    assert item["latency_ms"] >= 0
    _disable()


def test_fail_soft_item_errors_do_not_break_run(monkeypatch):
    _enable_local()
    calls = {"n": 0}
    def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] > 1:  # thirrja e parë ok, pjesa tjetër dështon deterministikisht
            raise httpx.ConnectError("down")
        return _response({"summary": "s", "pros": [], "cons": []})
    monkeypatch.setattr(ai_service.httpx, "post", flaky)
    res = evaluation.start_run("explain", sample_size=6, seed=5, max_calls=6)
    assert res["status"] == "complete"  # dështimet e pjesshme s'e ndërpresin
    assert res["failed"] > 0 and res["ok"] > 0
    run = repo.get_eval_run(res["run_id"], with_items=True)
    errs = [i for i in run["items"] if i["status"] == "error"]
    assert errs and all("transport" in i["error"] for i in errs)
    _disable()


def test_hard_call_limit_aborts(monkeypatch):
    _enable_local()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"summary": "s", "pros": [], "cons": []}))
    res = evaluation.start_run("explain", sample_size=25, seed=11, max_calls=3)
    assert res["status"] == "aborted" and res["calls"] == 3
    run = repo.get_eval_run(res["run_id"], with_items=True)
    assert len(run["items"]) == 3
    _disable()


def test_unknown_task_blocked():
    res = evaluation.start_run("nope", sample_size=3)
    assert res["blocked"] and "panjohur" in res["reason"]


# ------------------------------- prodhimi s'preket (DoD kryesor i 3C)

def test_production_scores_untouched_by_evaluation(monkeypatch):
    _enable_local()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"score": 99, "matched": [], "missing": []}))
    with repo.db() as con:
        before = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"], r["match_factors"])
                  for r in con.execute("SELECT id, rule_score, ai_score, final_score, match_factors FROM opportunities")}
    res = evaluation.start_run("semantic_match", sample_size=10, seed=99, max_calls=10)
    assert res["blocked"] is False
    with repo.db() as con:
        after = {r["id"]: (r["rule_score"], r["ai_score"], r["final_score"], r["match_factors"])
                 for r in con.execute("SELECT id, rule_score, ai_score, final_score, match_factors FROM opportunities")}
    assert before == after  # rule/ai/final + faktorët: të gjitha identike
    _disable()


def test_reproducible_rerun_same_seed(monkeypatch):
    _enable_local()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"summary": "s", "pros": [], "cons": []}))
    r1 = evaluation.start_run("explain", sample_size=8, seed=123, max_calls=8, label="a")
    r2 = evaluation.start_run("explain", sample_size=8, seed=123, max_calls=8, label="b")
    run1 = repo.get_eval_run(r1["run_id"])
    run2 = repo.get_eval_run(r2["run_id"])
    assert run1["opportunity_ids"] == run2["opportunity_ids"]  # i riprodhueshëm
    _disable()


# --------------------------------------------- review, export, krahasim

def test_review_persistence_and_export(client, monkeypatch):
    _enable_local()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"summary": "s", "pros": [], "cons": []}))
    res = evaluation.start_run("explain", sample_size=2, seed=77, max_calls=2)
    run = repo.get_eval_run(res["run_id"], with_items=True)
    item_id = run["items"][0]["id"]
    assert client.post(f"/api/eval/items/{item_id}/review",
                       json={"rating": 4, "notes": "e saktë"}).status_code == 200
    assert client.post(f"/api/eval/items/{item_id}/review",
                       json={"rating": 9}).status_code == 400  # jashtë rubrikës
    exp = client.get(f"/api/eval/runs/{res['run_id']}/export").json()
    assert exp["format_version"] == 1
    assert exp["items"][0]["human_rating"] == 4
    assert exp["items"][0]["human_notes"] == "e saktë"
    assert exp["rubric"]["scale"] == "1–5"
    assert "exported_at" in exp
    _disable()


def test_compare_report(client, monkeypatch):
    _enable_local()
    monkeypatch.setattr(ai_service.httpx, "post",
                        lambda url, **kw: _response({"score": 50, "matched": [], "missing": []}))
    r1 = evaluation.start_run("semantic_match", sample_size=4, seed=21, max_calls=4,
                              label="mock-A", force=True)
    r2 = evaluation.start_run("semantic_match", sample_size=4, seed=22, max_calls=4,
                              label="mock-B", force=True)
    rep = client.get("/api/eval/compare", params={"ids": f"{r1['run_id']},{r2['run_id']}"}).json()
    assert len(rep["runs"]) == 2
    m = rep["runs"][0]
    assert {"run_id", "task", "provider", "model", "ok_rate", "avg_latency_ms",
            "total_tokens", "avg_score", "mean_abs_diff_vs_rule", "reviewed"} <= set(m)
    assert m["avg_score"] == 50.0
    assert rep["winner"] in (r1["run_id"], r2["run_id"])
    _disable()


def test_eval_routes_fail_soft_when_disabled(client):
    _disable()
    r = client.post("/api/eval/run", json={"task": "explain", "sample_size": 3})
    assert r.status_code == 403
    assert r.json()["blocked"] is True
    assert client.get("/api/eval/runs").status_code == 200
