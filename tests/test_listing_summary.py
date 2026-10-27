import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import ai_service
import app
import opportunity_summary
import repository as repo


def _op(url="https://summary.example/job"):
    _, oid = repo.upsert_opportunity_full({
        "url": url,
        "title": f"Senior Java Developer {url.rsplit('/', 1)[-1]}",
        "company": "Example GmbH",
        "location": "Dortmund",
        "kind": "job",
        "description": "Sie entwickeln Java-Dienste und arbeiten mit PostgreSQL.",
        "tags": ["Java", "PostgreSQL"],
    })
    return oid


def _cfg():
    return {
        "provider": "openai-compatible",
        "model": "test-model",
        "base_url": "https://ai.example/v1",
        "timeout": 1,
        "retries": 0,
        "max_input_chars": 6000,
        "api_key": "test-key",
    }


def test_listing_summary_uses_requested_language_and_cache(monkeypatch):
    calls = []

    def complete(provider, prompt):
        calls.append(prompt)
        return json.dumps({
            "overview": "Ky është një rol për zhvillimin e shërbimeve Java.",
            "tasks": ["Zhvillon shërbime Java."],
            "requirements": ["Kërkohet PostgreSQL."],
            "conditions": ["Puna është në Dortmund."],
        }), {"total_tokens": 90}

    monkeypatch.setattr(ai_service, "_provider_call", complete)
    op = repo.get_opportunity(_op("https://summary.example/cache"))
    extra = {"language": "sq", "version": opportunity_summary.VERSION}
    first = ai_service.run_task("listing_summary", op, profile={}, cfg=_cfg(), extra=extra)
    second = ai_service.run_task("listing_summary", op, profile={}, cfg=_cfg(), extra=extra)
    assert first.ok and not first.cached
    assert second.ok and second.cached
    assert len(calls) == 1
    assert "Write every explanatory sentence in Albanian" in calls[0]
    assert "Do not assess candidate fit" in calls[0]


def test_panel_never_mistakes_original_excerpt_for_localized_summary(monkeypatch):
    oid = _op("https://summary.example/panel-empty")
    monkeypatch.setattr(opportunity_summary, "cached", lambda *_: None)
    with TestClient(app.app) as client:
        response = client.get(
            f"/api/jobs/{oid}/panel", headers={"X-UI-Language": "en"}
        )
    summary_block = response.text.split('<section class="opportunity-summary">', 1)[1].split("</section>", 1)[0]
    assert "Create a short bullet-point explanation in English" in summary_block
    assert "Explain with AI" in summary_block
    assert "Sie entwickeln" not in summary_block
    assert "Sie entwickeln" in response.text
    assert 'href="https://summary.example/panel-empty"' in response.text


def test_panel_renders_cached_summary_in_selected_language(monkeypatch):
    oid = _op("https://summary.example/panel-cached")
    data = {
        "overview": "A clear English explanation.",
        "tasks": ["Build Java services."],
        "requirements": ["Know PostgreSQL."],
        "conditions": ["Work in Dortmund."],
    }
    monkeypatch.setattr(
        opportunity_summary, "cached", lambda *_: SimpleNamespace(data=data)
    )
    with TestClient(app.app) as client:
        response = client.get(
            f"/api/jobs/{oid}/panel", headers={"X-UI-Language": "en"}
        )
    assert response.status_code == 200
    assert "What you will do" in response.text
    assert "What is required" in response.text
    assert "Key conditions" in response.text
    assert "A clear English explanation." in response.text
    assert "Explain with AI" not in response.text


def test_summary_endpoint_uses_request_language_and_is_user_triggered(monkeypatch):
    oid = _op("https://summary.example/endpoint")
    seen = {}

    def generate(op, language):
        seen.update(opportunity_id=op["id"], language=language)
        return {"ok": True, "cached": False, "data": {"overview": "Kurz"}}

    monkeypatch.setattr(opportunity_summary, "generate", generate)
    with TestClient(app.app) as client:
        response = client.post(
            f"/api/jobs/{oid}/summary", headers={"X-UI-Language": "de"}
        )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert seen == {"opportunity_id": oid, "language": "de"}


def test_invalid_listing_summary_is_not_reused(monkeypatch):
    op = repo.get_opportunity(_op("https://summary.example/invalid-cache"))
    monkeypatch.setattr(repo, "ai_cache_get", lambda *_: {
        "output": {"overview": "Incomplete"}, "meta": {}, "created_at": "now"
    })
    assert ai_service.cached_task(
        "listing_summary", op, profile={}, cfg=_cfg(), extra={"language": "en"}
    ) is None


def test_match_section_shows_only_six_positive_signals(monkeypatch):
    oid = _op("https://summary.example/compact-match")
    monkeypatch.setattr(opportunity_summary, "cached", lambda *_: None)
    monkeypatch.setattr(repo, "match_breakdown", lambda *_: {
        "factors": [],
        "matched": [f"Relevant {number}" for number in range(1, 8)],
        "missing": ["Unrelated profile skill", "Another unrelated skill"],
        "exclusions": [],
    })
    with TestClient(app.app) as client:
        response = client.get(f"/api/jobs/{oid}/panel")
    assert response.status_code == 200
    assert "Relevant 1" in response.text and "Relevant 6" in response.text
    assert "Relevant 7" not in response.text
    assert "Unrelated profile skill" not in response.text
    assert "Pa dëshmi:" not in response.text
