"""OE-007F — cikli i freskisë së sigurt: klasa burimesh, buxhetete, pagesa,
backoff, CAS/restart, digest dy-nivelesh, izolim ndër-përdorues."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import json

import app
import connectors
import repository as repo
import scheduling_policy as policy
import scheduler

repo.init()


def _due_schedule(pid, min_match=0, cadence="every_6h"):
    repo.upsert_schedule(pid, {"cadence": cadence, "min_match": min_match,
                               "enabled": True,
                               "next_run_at": "2026-01-01 00:00"})


def _seed(url, title="Python Developer", kind="job"):
    # Kompani/përshkrim unik per URL — L3-dedup (titull+org+vend+përshkrim)
    # i shkrinte rreshtat me seed identike.
    _r = repo.upsert_opportunity({"source": "fresh-test", "url": url, "title": title,
                             "company": f"ACME-{abs(hash(url)) % 100000}",
                             "location": "Berlin", "remote": False,
                             "description": "python react backend " + url,
                             "tags": [], "kind": kind, "posted_at": ""})
    assert _r == "new", f"seed u kthye {_r} për {url}"
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]


def _runs(source_id, pid, uid=1, n=3, error="HTTP 429 Too Many Requests"):
    for _ in range(n):
        rid = repo.start_search_run(source_id, profile_id=pid)
        repo.finish_search_run(rid, error=error, status="error")


def _stub(monkeypatch, results):
    def fake(source, query=None, profile=None, limit=None):
        return results.get(source, {"ok": False, "source": source, "discovered": 0,
                                    "new": 0, "dup": 0, "kinds": {}, "new_ids": [],
                                    "error": "pa mock", "run_id": 0})
    import connectors
    monkeypatch.setattr(connectors, "run_source_search", fake)
    with repo.db() as con:
        for sid in results:
            con.execute("DELETE FROM search_runs WHERE source=?", (sid,))
        con.commit()


# ------------------------------------------------------- klasat e burimeve

def test_source_execution_classes():
    assert policy.execution_class("arbeitnow") == "FREE_SAFE"
    assert policy.execution_class("hn_hiring") == "FREE_SAFE"
    assert policy.execution_class("bund_jobs") == "FREE_SAFE"
    assert policy.execution_class("adzuna") == "CONFIGURED_FREE_OR_QUOTA"
    assert policy.execution_class("greenhouse") == "CONFIGURED_FREE_OR_QUOTA"
    assert policy.execution_class("google_web") == "METERED_PAID"
    assert policy.execution_class("google_jobs") == "METERED_PAID"
    assert policy.execution_class("brave_web") == "METERED_PAID"
    assert policy.execution_class("local_feed") == "MANUAL_ONLY"
    assert policy.execution_class("source-i-panjohur") == "MANUAL_ONLY"
    # metadata qendrore — registry_meta e ekspozon për UI
    meta = {m["id"]: m for m in connectors.registry_meta()}
    assert meta["jobicy"]["exec_class"] == "FREE_SAFE"
    assert meta["google_web"]["exec_class"] == "METERED_PAID"


# --------------------------------------------------------- pagesat

def test_paid_source_excluded_by_default(monkeypatch):
    prof = next(p for p in repo.list_search_profiles() if p["name"] == "Professional Jobs")
    repo.update_search_profile(prof["id"], {"sources": '["google_web","jobicy"]'})
    prof = repo.get_search_profile(prof["id"])
    repo.set_setting("serpapi_key", "test-key")  # i konfiguruar — prapë jashtë
    plan = policy.plan_sources(prof)
    assert "google_web" in plan["excluded_paid"]
    assert all(p["source"] != "google_web" for p in plan["planned"])
    assert any(s["source"] == "google_web" and "pagesë" in s["reason"]
               for s in plan["skipped"])
    assert plan["budget"]["paid_opt_in"] is False
    repo.set_setting("serpapi_key", "")


def test_paid_opt_in_requires_enforcement_and_budget(monkeypatch):
    prof = next(p for p in repo.list_search_profiles() if p["name"] == "Professional Jobs")
    repo.update_search_profile(prof["id"], {"sources": '["google_web"]'})
    prof = repo.get_search_profile(prof["id"])
    repo.set_setting("serpapi_key", "test-key")
    try:
        # opt-in por enforcement disabled → prapë jashtë
        repo.set_setting(policy.PAID_INCLUDE_SETTING, "1")
        repo.set_setting("usage_enforcement_state", "disabled")
        plan = policy.plan_sources(prof)
        assert "google_web" in plan["excluded_paid"] and plan["planned"] == []

        # opt-in + enforcement aktiv → planifikohet brenda buxhetit
        repo.set_setting("usage_enforcement_state", "active")
        plan = policy.plan_sources(prof)
        assert [p["source"] for p in plan["planned"]] == ["google_web"]

        # plafoni ditor per burim respektohet edhe me opt-in
        _runs("google_web", prof["id"], n=policy.SOURCE_DAILY_RUN_CAP)
        plan = policy.plan_sources(prof)
        assert plan["planned"] == []
        assert any(s["source"] == "google_web" and "plafon ditor" in s["reason"]
                   for s in plan["skipped"])
    finally:
        repo.set_setting(policy.PAID_INCLUDE_SETTING, "0")
        repo.set_setting("usage_enforcement_state", "disabled")
        repo.set_setting("serpapi_key", "")


def test_no_silent_fallback_free_to_paid(monkeypatch):
    """Cikli kurrë s'shton burim me pagesë që s'është te burimet e profilit."""
    pid = repo.create_search_profile({"name": "NoFallback Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    prof = repo.get_search_profile(pid)
    repo.set_setting(policy.PAID_INCLUDE_SETTING, "1")
    repo.set_setting("usage_enforcement_state", "active")
    repo.set_setting("serpapi_key", "test-key")
    try:
        plan = policy.plan_sources(prof)
        executed = [p["source"] for p in plan["planned"]]
        assert "google_web" not in executed and "brave_web" not in executed
        assert executed == ["jobicy"]
    finally:
        repo.set_setting(policy.PAID_INCLUDE_SETTING, "0")
        repo.set_setting("usage_enforcement_state", "disabled")
        repo.set_setting("serpapi_key", "")


# --------------------------------------------------------- buxhetet

def test_max_sources_per_cycle():
    sources = ["arbeitnow", "remotive", "jobicy", "remoteok", "hn_hiring",
               "bund_jobs", "bund_tenders", "hackernews_freelance",
               "remotive_contract", "jobicy_contract"]  # 10 FREE_SAFE
    pid = repo.create_search_profile({"name": "Cap Prof", "kinds": '["job"]',
                                      "sources": json.dumps(sources)})
    prof = repo.get_search_profile(pid)
    plan = policy.plan_sources(prof)
    assert len(plan["planned"]) == policy.CYCLE_MAX_SOURCES
    assert len(plan["skipped"]) == len(sources) - policy.CYCLE_MAX_SOURCES


def test_partial_success_under_source_failure(monkeypatch):
    """Një burim dështon, tjetri suksess → cikli sukses, rezultate të pjesshme
    të ruajtura, feed-i jo i sëmurë (consecutive_failures 0)."""
    pid = repo.create_search_profile({"name": "Partial Prof", "kinds": '["job"]',
                                      "sources": '["jobicy","arbeitnow"]'})
    _due_schedule(pid)
    oid = _seed("https://fresh.example/ok-1")
    _stub(monkeypatch, {
        "jobicy": {"ok": True, "source": "jobicy", "discovered": 3, "new": 1, "dup": 2,
                   "kinds": {"job": 1}, "new_ids": [oid], "error": "", "run_id": 0},
        "arbeitnow": {"ok": False, "source": "arbeitnow", "discovered": 0, "new": 0,
                      "dup": 0, "kinds": {}, "new_ids": [], "error": "HTTP 503",
                      "run_id": 0},
    })
    res = scheduler.run_scheduled_profile(pid)
    assert res["ok"] is True and res["sources_executed"] == 2
    assert res["consecutive_failures"] == 0
    sched = repo.get_schedule(pid)
    assert (sched.get("consecutive_failures") or 0) == 0


# ------------------------------------------------------- backoff / gabime

def test_repeated_429_backoff_skips_cycle():
    pid = repo.create_search_profile({"name": "Backoff Prof", "kinds": '["job"]',
                                      "sources": '["arbeitnow"]'})
    _runs("arbeitnow", pid, n=3, error="HTTP 429 Too Many Requests")
    prof = repo.get_search_profile(pid)
    plan = policy.plan_sources(prof)
    assert plan["planned"] == []
    assert "429" in plan["skipped"][0]["reason"]


def test_repeated_5xx_cooldown():
    pid = repo.create_search_profile({"name": "Cooldown Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    _runs("jobicy", pid, n=3, error="HTTP 503 Service Unavailable")
    plan = policy.plan_sources(repo.get_search_profile(pid))
    assert plan["planned"] == []
    assert "5xx" in plan["skipped"][0]["reason"] or "ftohje" in plan["skipped"][0]["reason"]


def test_bad_credentials_reason():
    pid = repo.create_search_profile({"name": "Cred Prof", "kinds": '["job"]',
                                      "sources": '["adzuna"]'})
    repo.set_setting("adzuna_app_id", "id")
    repo.set_setting("adzuna_app_key", "key")
    _runs("adzuna", pid, n=3, error="HTTP 401")
    plan = policy.plan_sources(repo.get_search_profile(pid))
    assert plan["planned"] == []
    assert "kredenciale" in plan["skipped"][0]["reason"]
    repo.set_setting("adzuna_app_id", "")
    repo.set_setting("adzuna_app_key", "")


def test_two_failed_then_success_recovers():
    pid = repo.create_search_profile({"name": "Recover Prof", "kinds": '["job"]',
                                      "sources": '["arbeitnow"]'})
    _runs("arbeitnow", pid, n=2, error="HTTP 429")  # vetëm 2 dështime → provohet
    plan = policy.plan_sources(repo.get_search_profile(pid))
    assert [p["source"] for p in plan["planned"]] == ["arbeitnow"]


# ------------------------------------------- restart / CAS / pa catch-up

def test_double_tick_does_not_rerun(monkeypatch):
    pid = repo.create_search_profile({"name": "DoubleTick Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    _due_schedule(pid)
    calls = {"n": 0}
    oid = _seed("https://fresh.example/dt-1")

    def counting(source, query=None, profile=None, limit=None):
        calls["n"] += 1
        return {"ok": True, "source": source, "discovered": 1, "new": 1, "dup": 0,
                "kinds": {"job": 1}, "new_ids": [oid], "error": "", "run_id": 0}

    monkeypatch.setattr(connectors, "run_source_search", counting)
    with repo.db() as con:
        con.execute("DELETE FROM search_runs WHERE source='jobicy'")
        con.commit()
    r1 = scheduler.run_due()
    r2 = scheduler.run_due()  # tick i dytë menjëherë → asgjë due
    assert r1["ran"] >= 1 and calls["n"] == 1
    assert r2["ran"] == 0 and calls["n"] == 1


def test_missed_occurrence_no_catchup_storm():
    """next_run_at 30 ditë në të kaluarën → NJË ekzekutim, i ardhshmi nga TANI."""
    pid = repo.create_search_profile({"name": "Catchup Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    _due_schedule(pid, cadence="every_6h")
    with repo.db() as con:
        con.execute("UPDATE schedule_configs SET next_run_at='2026-09-01 00:00' WHERE profile_id=?",
                    (pid,))
        con.commit()
    res = scheduler.run_scheduled_profile(pid)
    assert res["skipped"] is False
    sched = repo.get_schedule(pid)
    nxt = datetime.fromisoformat(sched["next_run_at"]).replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    assert now - timedelta(minutes=5) <= nxt <= now + timedelta(hours=7)


def test_two_workers_cannot_claim_same_occurrence():
    pid = repo.create_search_profile({"name": "CAS Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    _due_schedule(pid)
    sched = repo.get_schedule(pid)
    ok1 = repo.claim_schedule(pid, sched["next_run_at"], "2026-10-02 00:00")
    ok2 = repo.claim_schedule(pid, sched["next_run_at"], "2026-10-02 00:00")
    assert ok1 is True and ok2 is False


# ------------------------------------------------------- njoftime/digest

def test_duplicate_imports_suppress_notifications(monkeypatch):
    pid = repo.create_search_profile({"name": "DupNotif Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    _due_schedule(pid, min_match=0)
    oid = _seed("https://fresh.example/dup-1")

    def same_items(source, query=None, profile=None, limit=None):
        return {"ok": True, "source": source, "discovered": 1, "new": 1, "dup": 0,
                "kinds": {"job": 1}, "new_ids": [oid], "error": "", "run_id": 0}

    _stub(monkeypatch, {"jobicy": same_items("jobicy")})
    r1 = scheduler.run_scheduled_profile(pid)
    with repo.db() as con:
        con.execute("DELETE FROM search_runs WHERE source='jobicy'")
        con.commit()
    r2 = scheduler.run_scheduled_profile(pid, now_utc=datetime.now(timezone.utc) + timedelta(hours=7))
    assert r1["notified"] == 1
    assert r2["notified"] == 0  # dedup sched:pid:oid
    notifs = [n for n in repo.list_notifications(50) if n["opportunity_id"] == oid]
    assert len(notifs) == 1


def test_digest_groups_strong_and_review(monkeypatch):
    strong_oid = _seed("https://fresh.example/dg-s2", title="Strong two")
    review_oid = _seed("https://fresh.example/dg-review", title="Review match")
    low_oid = _seed("https://fresh.example/dg-low2", title="Too low")
    with repo.db() as con:
        for oid, score in ((strong_oid, 85), (review_oid, 65), (low_oid, 40)):
            con.execute("UPDATE opportunities SET rule_score=?, final_score=? WHERE id=?",
                        (score, score, oid))
        con.commit()
    import notifier
    cfg = notifier.get_config()
    from context import ActorContext
    actor = ActorContext(1, 1, "owner")
    since = "2000-01-01 00:00"
    strong_min = max(notifier.DIGEST_STRONG_MIN, int(cfg["min_match"] or 0))
    strong = notifier._digest_rows(since, cfg, actor, strong_min)
    review = [j for j in notifier._digest_rows(since, cfg, actor, notifier.DIGEST_REVIEW_MIN)
              if (j["rule_score"] or 0) < strong_min]
    assert strong_oid in {j["id"] for j in strong}
    assert review_oid in {j["id"] for j in review}
    assert low_oid not in {j["id"] for j in strong + review}
    # etiketimi: seksioni review KURRË nuk quhet "high/fortë"
    emails = []
    monkeypatch.setattr(notifier, "send_email",
                        lambda s, t, h, r: emails.append((s, t, h)) or {"ok": True})
    repo.set_setting("email_enabled", "1")
    repo.set_setting("email_recipient", "owner@example.com")
    repo.set_setting("email_mode", "digest")
    try:
        res = notifier.send_digest(now=datetime.now(timezone.utc))
        assert res["ok"] is True and res["items"] >= 2
        html = emails[-1][2]
        assert "Ia vlen të shikohen" in html
        assert "Përputhje të forta" in html
        # rreshti 65% duhet të jetë NËN seksionin review, jo te forta
        strong_pos = html.find("Përputhje të forta")
        review_pos = html.find("Ia vlen të shikohen")
        r65 = html.find("Review match")
        assert strong_pos < review_pos < r65
    finally:
        repo.set_setting("email_enabled", "0")
        repo.set_setting("email_recipient", "")
        # provat e phase7b pres gjendje të pastër deliveries/digests
        with repo.db() as con:
            con.execute("DELETE FROM email_deliveries")
            con.execute("DELETE FROM digest_runs")
            con.commit()


# --------------------------------------------------- izolimi ndër-përdorues

def test_cross_user_scheduler_isolation(monkeypatch):
    import tenancy
    from context import ActorContext, run_as
    uid2 = repo.create_user("fresh-b@example.org", "not-a-hash", display_name="Fresh B")
    tid2 = tenancy.ensure_tenant("Fresh B", f"fresh-b-{uid2}")
    tenancy.add_membership(uid2, tid2, role="owner")
    with run_as(ActorContext(uid2, tid2, "owner")):
        pid2 = repo.create_search_profile({"name": "Fresh B Prof", "kinds": '["job"]',
                                           "sources": '["jobicy"]'})
        repo.save_profile({**repo.get_profile(), "skills": "datev", "title": "Takstertar"})
        _due_schedule(pid2)
    oid2 = None
    with run_as(ActorContext(uid2, tid2, "owner")):
        oid2 = _seed("https://fresh.example/iso-1")
    _stub(monkeypatch, {"jobicy": {"ok": True, "source": "jobicy", "discovered": 1,
                                   "new": 1, "dup": 0, "kinds": {"job": 1},
                                   "new_ids": [oid2], "error": "", "run_id": 0}})
    res = scheduler.run_scheduled_profile(pid2)
    assert res["skipped"] is False and res["notified"] == 1
    with repo.db() as con:
        b_notifs = con.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND opportunity_id=?",
            (uid2, oid2)).fetchone()[0]
        owner_notifs = con.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=1 AND opportunity_id=?",
            (oid2,)).fetchone()[0]
    assert b_notifs == 1 and owner_notifs == 0


# --------------------------------------------------- pa aplikime automatike

def test_cycle_creates_no_applications(monkeypatch):
    pid = repo.create_search_profile({"name": "NoApps Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    _due_schedule(pid, min_match=0)
    oid = _seed("https://fresh.example/noapp-1")
    _stub(monkeypatch, {"jobicy": {"ok": True, "source": "jobicy", "discovered": 1,
                                   "new": 1, "dup": 0, "kinds": {"job": 1},
                                   "new_ids": [oid], "error": "", "run_id": 0}})
    with repo.db() as con:
        before = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    scheduler.run_scheduled_profile(pid)
    with repo.db() as con:
        after = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert before == after


# ----------------------------------------------- përmbledhja e ciklit

def test_cycle_summary_persisted_and_served(monkeypatch):
    client = TestClient(app.app)
    pid = repo.create_search_profile({"name": "Summary Prof", "kinds": '["job"]',
                                      "sources": '["jobicy"]'})
    _due_schedule(pid, min_match=0)
    oid = _seed("https://fresh.example/sum-1")
    _stub(monkeypatch, {"jobicy": {"ok": True, "source": "jobicy", "discovered": 4,
                                   "new": 1, "dup": 3, "kinds": {"job": 1},
                                   "new_ids": [oid], "error": "", "run_id": 0}})
    res = scheduler.run_scheduled_profile(pid)
    assert res["sources_planned"] == 1 and res["sources_executed"] == 1
    c = client.get("/api/scheduler/last-cycle")
    cycles = c.json()["cycles"]
    assert str(pid) in cycles
    cyc = cycles[str(pid)]
    for key in ("sources_planned", "sources_executed", "sources_skipped",
                "requests_consumed", "discovered", "new", "duplicates",
                "high_matches", "worth_reviewing", "failures", "elapsed_seconds"):
        assert key in cyc
    # plan dry-run endpoint ekziston dhe s'ekzekuton
    plan = client.get("/api/scheduler/plan").json()
    assert "plans" in plan
