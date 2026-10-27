"""OE-004C HTTP/domain/UI integration with local fixture handlers only."""
import json

from fastapi.testclient import TestClient
import pytest

import applications
import auth_service
import background_domain
import background_jobs
import background_worker
import config
import context
import discovery
import generator
import profile_intelligence
import repository as repo
import resume_ai

ONE = context.ActorContext(1, 1, "owner")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "work-api.sqlite3")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SECRET_KEY", "fixture-background-api-secret-at-least-32")
    repo.init()


@pytest.fixture
def client():
    import app
    return TestClient(app.app)


def fixture_plan(source="fixture", query="fixture query"):
    return {"tasks": [{"source": source, "what": query, "where": "Essen",
            "country": "de", "kinds": ["job"], "limit": 30,
            "category": "", "site": ""}], "query": query, "where": "Essen",
            "country": "de", "kind": "job", "categories": []}


def test_discovery_job_survives_reopen_and_keeps_query_out_of_job_and_queue(client, monkeypatch):
    private_query = "PRIVATE SEARCH QUERY"
    monkeypatch.setattr(discovery, "plan", lambda **kwargs: fixture_plan(query=private_query))

    def run_task(task):
        run_id = repo.start_search_run(task["source"])
        repo.finish_search_run(run_id, discovered=4, new=2, dup=2, status="ok")
        return {"ok": True, "run_id": run_id, "source": task["source"],
                "discovered": 4, "new": 2, "dup": 2, "error": ""}

    monkeypatch.setattr(discovery, "run_task", run_task)
    response = client.post("/api/work", json={"type": "profile_discovery", "input": {
        "kind": "job", "what": private_query, "where": "Essen", "country": "de"
    }})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    reopened = client.get(f"/api/work/{job_id}")
    assert reopened.status_code == 200
    body = reopened.json()
    assert body["status"] == "succeeded"
    assert body["result"] == {"runs": [{"id": body["result"]["runs"][0]["id"],
        "source": "fixture", "status": "ok", "discovered": 4, "new_count": 2,
        "duplicate_count": 2, "error": ""}], "discovered": 4, "new": 2, "duplicates": 2}
    with repo.db() as con:
        job_input = con.execute("SELECT input_json FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()[0]
        request_input = con.execute("SELECT request_json FROM background_work_requests").fetchone()[0]
        outbox = con.execute("SELECT job_id,contract_version FROM background_job_outbox WHERE job_id=?", (job_id,)).fetchall()
    assert private_query not in job_input and private_query in request_input
    assert all(tuple(row) == (job_id, 1) for row in outbox)


def test_failed_discovery_is_localized_and_retry_creates_new_job(client, monkeypatch):
    monkeypatch.setattr(discovery, "plan", lambda **kwargs: fixture_plan())

    def fail_task(task):
        run_id = repo.start_search_run(task["source"])
        repo.finish_search_run(run_id, error="fixture", status="error")
        return {"ok": False, "run_id": run_id, "source": task["source"],
                "discovered": 0, "new": 0, "dup": 0, "error": "fixture"}

    monkeypatch.setattr(discovery, "run_task", fail_task)
    first = client.post("/api/work", json={"type": "profile_discovery", "input": {
        "kind": "job", "country": "de"
    }}).json()
    status = client.get(f"/api/work/{first['job_id']}", headers={"X-UI-Language": "en"}).json()
    assert status["status"] == "failed"
    assert status["message"] == "The external sources did not respond. Try again."
    retried = client.post(f"/api/work/{first['job_id']}/retry").json()
    assert retried["job_id"] != first["job_id"] and retried["retry_of_job_id"] == first["job_id"]


def test_resume_intelligence_and_application_jobs_publish_reviewable_results(client, monkeypatch):
    monkeypatch.setattr(resume_ai, "generate", lambda language, options: {"ok": True, "language": language})
    monkeypatch.setattr(resume_ai, "get", lambda language, options: {"document": {}, "language": language})
    resume = client.post("/api/work", json={"type": "resume_generation", "input": {
        "language": "de", "skill_limit": 24, "include_projects": True,
        "include_personal": False, "include_photo": True,
    }}).json()
    resume_status = client.get(resume["progress_url"]).json()
    assert resume_status["status"] == "succeeded"
    assert resume_status["result"] == {"language": "de", "url": "/profile/resume?cv_language=de"}

    monkeypatch.setattr(profile_intelligence, "analyze", lambda: {"ok": True, "count": 1})
    monkeypatch.setattr(profile_intelligence, "state", lambda: {
        "stale": False, "concepts": [{"id": "one"}]
    })
    intelligence = client.post("/api/work", json={
        "type": "profile_intelligence", "input": {"language": "sq"}
    }).json()
    intelligence_status = client.get(intelligence["progress_url"]).json()
    assert intelligence_status["status"] == "succeeded"
    assert intelligence_status["result"]["count"] == 1

    created = client.post("/api/jobs", json={"title": "Fixture job",
        "url": "https://example.org/fixture-job", "company": "Fixture", "location": "Berlin"}).json()
    opportunity = next(item for item in repo.list_opportunities() if item["title"] == "Fixture job")
    application_id = applications.create_draft(opportunity)
    monkeypatch.setattr(generator, "generate", lambda **kwargs: {"ok": True,
        "application_id": application_id})
    draft = client.post("/api/work", json={"type": "application_draft", "input": {
        "application_id": application_id, "language": "en", "tone": "professional"
    }}).json()
    draft_status = client.get(draft["progress_url"]).json()
    assert draft_status["status"] == "succeeded"
    assert draft_status["result"]["application_id"] == application_id


def test_cancel_and_cross_user_reads_fail_closed(client):
    pending = background_jobs.create("profile_discovery", {"sources": ["fixture"]}, ctx=ONE)
    cancelled = client.post(f"/api/work/{pending['job_id']}/cancel").json()
    assert cancelled["status"] == "cancelled"

    uid = auth_service.create_user("second-work@example.org", "fixture-password-long-enough")
    other = context.ActorContext(uid, 1, "member")
    with context.run_as(other), pytest.raises(background_jobs.JobNotFound):
        background_domain.public(pending["job_id"], ctx=other)
    assert client.get("/api/work/not-a-job").status_code == 404


def test_inline_harness_finishes_largest_profile_discovery_plan(client, monkeypatch):
    tasks = fixture_plan()["tasks"] * 14
    plan = {**fixture_plan(), "tasks": tasks}
    monkeypatch.setattr(discovery, "plan", lambda **kwargs: plan)
    calls = []

    def run_task(task):
        calls.append(task["source"])
        run_id = repo.start_search_run(task["source"])
        repo.finish_search_run(run_id, discovered=1, new=1, status="ok")
        return {"ok": True, "run_id": run_id}

    monkeypatch.setattr(discovery, "run_task", run_task)
    created = client.post("/api/work", json={
        "type": "profile_discovery", "input": {"kind": "job", "country": "de"}
    }).json()
    status = client.get(created["progress_url"]).json()
    assert status["status"] == "succeeded"
    assert status["progress_total"] == 15 and status["progress_current"] == 15
    assert len(calls) == 14


def test_retry_rejects_old_profile_and_production_without_queue_creates_nothing(
    client, monkeypatch
):
    failed = background_jobs.create("profile_discovery", {"sources": ["fixture"]}, ctx=ONE)
    queued = background_jobs.pending_outbox(job_id=failed["job_id"])[0]
    background_jobs.mark_outbox_sent(queued["id"], "fixture")
    claim = background_jobs.claim(failed["job_id"], "fixture-worker", ctx=ONE)
    background_jobs.fail(claim, "provider_unavailable", ctx=ONE)
    repo.update_profile_fields({"title": "Changed profile"}, ctx=ONE)
    response = client.post(f"/api/work/{failed['job_id']}/retry")
    assert response.status_code == 409

    before = len(background_jobs.list_jobs(ctx=ONE))
    monkeypatch.setattr(config, "APP_ENV", "production")
    monkeypatch.delenv("BACKGROUND_JOBS_ENABLED", raising=False)
    monkeypatch.delenv("BACKGROUND_DISPATCH", raising=False)
    response = client.post("/api/work", json={
        "type": "profile_intelligence", "input": {"language": "en"}
    })
    assert response.status_code == 503
    assert len(background_jobs.list_jobs(ctx=ONE)) == before


def test_real_domain_handlers_are_registered_for_queue_consumers():
    assert set(background_domain.HANDLERS) == background_jobs.JOB_TYPES
    assert all(callable(handler) for handler in background_domain.HANDLERS.values())


def test_work_ui_assets_and_localized_catalog_are_present(client):
    discovery_page = client.get("/discover", params={"lang": "en"})
    assert discovery_page.status_code == 200
    assert 'id="discovery-cancel"' in discovery_page.text
    assert "/static/work.js" in discovery_page.text
    resume_page = client.get("/profile/resume", params={"lang": "de"})
    assert resume_page.status_code == 200 and 'id="resume-cancel"' in resume_page.text
    catalog = json.loads((config.BASE_DIR / "static" / "translations.json").read_text())
    for key in ("Puna po vazhdon edhe nëse e mbyll këtë faqe.",
                "work_status_queued", "work_status_running", "work_status_succeeded",
                "work_status_failed", "work_status_cancelled", "work_status_superseded"):
        assert set(catalog[key]) == {"en", "de"}
