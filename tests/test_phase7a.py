"""Faza 7A: schedul + CAS + njoftime (me ora të injektuar dhe mock burimesh)."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

import repository as repo
import scheduler


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


def _make_profile(name="Sched Prof", sources='["jobicy"]'):
    return repo.create_search_profile({"name": name, "kinds": '["job"]',
                                       "sources": sources})


@pytest.fixture(scope="module")
def client_global():
    return None


def _seed_op(url, title="Python Dev", score_kind="high", desc=None):
    repo.upsert_opportunity({
        "source": "sched-test", "url": url, "title": title, "company": "ACME",
        "location": "Berlin", "remote": False,
        "description": desc or ("python backend " + title), "tags": [], "kind": "job",
        "posted_at": "",
    })
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]


def _mock_sources(monkeypatch, results):
    def fake(source, query=None, profile=None, limit=None):
        return results.get(source, {"ok": False, "source": source, "discovered": 0,
                                    "new": 0, "dup": 0, "kinds": {}, "new_ids": [],
                                    "error": "pa mock", "run_id": 0})
    import connectors
    monkeypatch.setattr(connectors, "run_source_search", fake)
    # OE-007F: politika e freskisë sheh historikun e kërkimeve — testet e
    # njoftimeve e presin burimin "jo të freskët"; pastazo search_runs e
    # burimeve të mockuara (gjendje e kontrolluar: s'u pollua kohët e fundit).
    with repo.db() as con:
        for sid in results:
            con.execute("DELETE FROM search_runs WHERE source=?", (sid,))
        con.commit()


# --------------------------------------------------------- kadencat

def test_compute_next_interval_cadences():
    base = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    assert scheduler.compute_next("hourly", "UTC", "08:00", base) == "2026-09-04 11:00"
    assert scheduler.compute_next("every_3h", "UTC", "08:00", base) == "2026-09-04 13:00"
    assert scheduler.compute_next("every_6h", "UTC", "08:00", base) == "2026-09-04 16:00"
    assert scheduler.compute_next("manual", "UTC", "08:00", base) is None


def test_daily_window_same_day_vs_next_day():
    # ora 06:00 UTC → dita e njëjtë 08:00 Berlin (06:00 UTC verë) kufi
    morning = datetime(2026, 9, 4, 5, 30, tzinfo=timezone.utc)  # 07:30 Berlin
    nxt = scheduler.compute_next("daily", "Europe/Berlin", "08:00", morning)
    assert nxt == "2026-09-04 06:00"
    # pas orarit → dita tjetër
    late = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)  # 11:00 Berlin
    nxt2 = scheduler.compute_next("daily", "Europe/Berlin", "08:00", late)
    assert nxt2 == "2026-09-05 06:00"


def test_weekdays_daily_skips_weekend():
    friday_evening = datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc)  # e shtunë natën BE
    nxt = scheduler.compute_next("weekdays_daily", "Europe/Berlin", "08:00", friday_evening)
    dt = datetime.fromisoformat(nxt)
    assert dt.weekday() < 5


# ------------------------------------------------- CRUD & due/CAS

def test_schedule_crud_and_due(client):
    pid = _make_profile()
    sched = client.put(f"/api/schedules/{pid}", json={
        "cadence": "daily", "min_match": 50, "enabled": True,
        "next_run_at": "2026-01-01 00:00",  # e kaluar → due
    }).json()
    assert sched["cadence"] == "daily" and sched["min_match"] == 50
    assert sched["next_run_at"] == "2026-01-01 00:00"
    lst = client.get("/api/schedules").json()
    assert any(s["profile_id"] == pid for s in lst)
    # invalid
    assert client.put(f"/api/schedules/{pid}",
                      json={"cadence": "whenever"}).status_code == 400
    assert client.put("/api/schedules/9999",
                      json={"cadence": "daily"}).status_code == 404


def test_claim_prevents_double_run():
    repo.upsert_schedule(4, {"next_run_at": "2026-01-01 00:00"})
    sched = repo.get_schedule(4)
    ok1 = repo.claim_schedule(4, sched["next_run_at"], "2026-02-01 00:00")
    ok2 = repo.claim_schedule(4, sched["next_run_at"], "2026-02-01 00:00")
    assert ok1 is True and ok2 is False  # i dyti nuk e merr lejen
    repo.upsert_schedule(4, {"next_run_at": None})


def test_scheduler_restart_idempotent(client):
    # restart (init) NUK reseton next_run_at — baza e idempotencës
    pid = _make_profile(name="Restart Prof")
    repo.upsert_schedule(pid, {"cadence": "daily", "next_run_at": "2026-12-01 00:00"})
    s1 = repo.get_schedule(pid)
    import repository
    repository.init()  # simulo restart
    s2 = repo.get_schedule(pid)
    assert s1["next_run_at"] == s2["next_run_at"] == "2026-12-01 00:00"


# --------------------------------------------------- ekzekutimi real

def test_successful_run_creates_notification_once(client, monkeypatch):
    oid = _seed_op("https://7a.example/n1", "Python Dev (high)")
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Professional Jobs")
    repo.update_search_profile(prof["id"], {"sources": '["jobicy"]'})
    prof = repo.get_search_profile(prof["id"])
    repo.upsert_schedule(prof["id"], {"cadence": "daily", "min_match": 0,
                                      "next_run_at": "2026-01-01 00:00"})
    _mock_sources(monkeypatch, {"jobicy": {
        "ok": True, "source": "jobicy", "discovered": 1, "new": 1, "dup": 0,
        "kinds": {"job": 1}, "new_ids": [oid], "error": "", "run_id": 0}})
    res = scheduler.run_scheduled_profile(prof["id"])
    assert res["skipped"] is False and res["ok"] is True
    assert res["notified"] == 1
    # i dyti identik → pa njoftim të re (dedup)
    res2 = scheduler.run_scheduled_profile(prof["id"], now_utc=datetime.now(timezone.utc) + timedelta(hours=1))
    assert res2["notified"] == 0
    notifs = [n for n in repo.list_notifications(50) if n["opportunity_id"] == oid]
    assert len(notifs) == 1
    _cleanup(prof["id"], oid)


def test_match_threshold_and_exclusion_suppressed(client, monkeypatch):
    repo.set_profile_section("filters_test", []) if False else None
    oid_low = _seed_op("https://7a.example/low", "Mystery Role",
                       desc="xyzzy plugh mystery listing")
    prof = next(p for p in repo.list_search_profiles() if p["name"] == "Professional Jobs")
    repo.update_search_profile(prof["id"], {"sources": '["jobicy"]', "min_match": 50})
    prof = repo.get_search_profile(prof["id"])
    repo.upsert_schedule(prof["id"], {"next_run_at": "2026-01-01 00:00", "min_match": 50})
    _mock_sources(monkeypatch, {"jobicy": {
        "ok": True, "source": "jobicy", "discovered": 1, "new": 1, "dup": 0,
        "kinds": {"job": 1}, "new_ids": [oid_low], "error": "", "run_id": 0}})
    res = scheduler.run_scheduled_profile(prof["id"])
    assert res["notified"] == 0  # nën prag → pa njoftim
    # excluded → kurrë njoftim
    flt = repo.get_filters() | {"exclude_keywords": "unpaid"}
    repo.save_filters(flt)
    res2 = scheduler.run_scheduled_profile(prof["id"])
    assert res2["notified"] == 0
    flt2 = repo.get_filters() | {"exclude_keywords": ""}
    repo.save_filters(flt2)
    _cleanup(prof["id"], oid_low)


def _cleanup(profile_id, opp_id):
    with repo.db() as con:
        con.execute("DELETE FROM notifications WHERE dedup_key LIKE 'sched:%' AND opportunity_id=?", (opp_id,))
        con.execute("UPDATE schedule_configs SET sources='[]' WHERE profile_id=?", (profile_id,))
        con.commit()


# ------------------------------------------------- dështimet

def test_partial_failure_counts_success_and_full_failure_increments(client, monkeypatch):
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Freelance")
    repo.update_search_profile(prof["id"], {"sources": '["jobicy","hn_leads"]'})
    prof = repo.get_search_profile(prof["id"])
    repo.upsert_schedule(prof["id"], {"next_run_at": "2026-01-01 00:00"})
    # të gjitha dështojnë
    _mock_sources(monkeypatch, {"jobicy": _fail(), "hn_leads": _fail()})
    res = scheduler.run_scheduled_profile(prof["id"])
    assert res["ok"] is False
    sched = repo.get_schedule(prof["id"])
    assert sched["consecutive_failures"] >= 1
    f1 = sched["consecutive_failures"]
    # pjesërisht ok → sukses, counter reset
    ok_res = {"ok": True, "source": "jobicy", "discovered": 1, "new": 1, "dup": 0,
              "kinds": {"job": 1}, "new_ids": [], "error": "", "run_id": 0}
    _mock_sources(monkeypatch, {"jobicy": ok_res, "hn_leads": _fail()})
    res2 = scheduler.run_scheduled_profile(prof["id"])
    assert res2["ok"] is True
    sched2 = repo.get_schedule(prof["id"])
    assert sched2["consecutive_failures"] == 0
    _cleanup(prof["id"], 0)
    repo.upsert_schedule(prof["id"], {"next_run_at": None})


def _fail():
    return {"ok": False, "source": "?", "discovered": 0, "new": 0, "dup": 0,
            "kinds": {}, "new_ids": [], "error": "transport dështoi", "run_id": 0}


# ------------------------------------------- lexo/hiq + API njoftime

def test_read_dismiss_workflow_and_endpoints(client, monkeypatch):
    repo.set_setting("x", "1") if False else None
    repo.create_notification(None, None, "scheduled_search_failed", "Gabim burimi", "hn", "dedup-x")
    n = repo.list_notifications(5)[0]
    assert n["read_at"] is None
    assert client.post(f"/api/notifications/{n['id']}/read").json()["ok"] is True
    assert client.get("/api/notifications?unread_only=1").json()["unread"] >= 0
    assert client.post(f"/api/notifications/{n['id']}/dismiss").json()["ok"] is True
    assert client.post("/api/notifications/read-all").status_code == 200
    repo.delete_notification_test(n["id"]) if hasattr(repo, "delete_notification_test") else None
    # pastrim
    with repo.db() as con:
        con.execute("DELETE FROM notifications WHERE dedup_key='dedup-x'")
        con.commit()


def test_tick_endpoint(client, monkeypatch):
    _enable = repo.upsert_schedule
    _mock_sources(monkeypatch, {"jobicy": {"ok": True, "source": "jobicy", "discovered": 0,
                                           "new": 0, "dup": 0, "kinds": {}, "new_ids": [],
                                           "error": "", "run_id": 0}})
    prof = next(p for p in repo.list_search_profiles() if p["name"] == "Professional Jobs")
    repo.upsert_schedule(prof["id"], {"next_run_at": "2026-01-01 00:00"})
    r = client.post("/api/scheduler/tick")
    assert r.status_code == 200
    assert r.json()["due"] >= 1
    repo.upsert_schedule(prof["id"], {"next_run_at": None})
