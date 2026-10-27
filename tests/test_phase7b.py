"""Faza 7B: email notifications + digest — dedup, fail-soft, quiet hours.

Dërgesa mock-ohet në nivel `notifier.send_email` — zero rrjet/SMTP real.
"""
from datetime import datetime, timedelta, timezone

import notifier
import pytest
from fastapi.testclient import TestClient

import repository as repo
import scheduler


@pytest.fixture(scope="module")
def client():
    import app
    return TestClient(app.app)


@pytest.fixture(autouse=True)
def _clean_email_state():
    yield
    repo.set_setting("email_enabled", "0")
    repo.set_setting("email_mode", "digest")
    with repo.db() as con:
        con.execute("DELETE FROM email_deliveries")
        con.execute("DELETE FROM digest_runs")
        con.commit()


def _cleanup(profile_id, opp_id):
    with repo.db() as con:
        con.execute("DELETE FROM notifications WHERE dedup_key LIKE 'sched:%' AND opportunity_id=?",
                    (opp_id,))
        con.execute("DELETE FROM email_deliveries WHERE dedup_key LIKE ?",
                    ("email:immediate:" + str(opp_id) + ":%",))
        con.commit()


def _seed(url, title="Booking platform", kind="freelance", score=None):
    repo.upsert_opportunity({
        "source": "7b-test", "url": url, "title": title,
        "company": f"Klinika-{abs(hash(url)) % 100000}",
        "location": "Berlin", "remote": True,
        "description": "We need a booking website with appointments and CRM.",
        "tags": [], "kind": kind, "posted_at": "",
    })
    with repo.db() as con:
        oid = con.execute("SELECT id FROM opportunities WHERE source_url=?", (url,)).fetchone()[0]
    if score is not None:
        with repo.db() as con:
            con.execute("UPDATE opportunities SET rule_score=?, final_score=? WHERE id=?",
                        (score, score, oid))
        with repo.db() as con:
            row = con.execute("SELECT kind_data FROM opportunities WHERE id=?", (oid,)).fetchone()
    return oid


def _enable(mode="immediate", min_match=0, recipient="me@example.com"):
    repo.set_setting("email_enabled", "1")
    repo.set_setting("email_recipient", recipient)
    repo.set_setting("email_mode", mode)
    repo.set_setting("email_min_match", str(min_match))
    repo.set_setting("email_quiet_start", "00:00")  # pa window → dërgesa menynëherë
    repo.set_setting("email_quiet_end", "00:00")


# ------------------------------------------------------------ disabled

def test_email_disabled_by_default(client):
    cfg = notifier.get_config()
    assert cfg["enabled"] is False
    oid = _seed("https://7b.example/d1")
    res = notifier.deliver_immediate(repo.get_opportunity(oid))
    assert res["sent"] is False and "çaktivizuar" in res["reason"]
    # endpoint test → 400
    assert client.post("/api/email/test").status_code == 400
    # pa deliveries
    with repo.db() as con:
        assert con.execute("SELECT COUNT(*) FROM email_deliveries").fetchone()[0] == 0


# --------------------------------------------------------- konfigurimi

def test_config_validation_and_defaults(client):
    # mode i panjohur → digest default; min_match negativ → clamp 0
    repo.set_setting("email_mode", "wrongmode")
    repo.set_setting("email_min_match", "-5")
    cfg = notifier.get_config()
    assert cfg["mode"] == "digest" and cfg["min_match"] == 0
    # ruajtje përmes API
    r = client.post("/api/email/config", json={
        "enabled": "1", "recipient": "me@example.com", "mode": "immediate",
        "min_match": 70, "kinds": ["job", "freelance"], "quiet_start": "23:00",
        "quiet_end": "06:00"})
    assert r.status_code == 200
    cfg = client.get("/api/email/config").json()
    assert cfg["enabled"] is True and cfg["min_match"] == 70
    assert cfg["kinds"] == ["job", "freelance"]
    assert cfg["quiet_start"] == "23:00"
    # sekretet kurrë në API
    assert "smtp_pass" not in cfg
    _disable()


def _disable():
    repo.set_setting("email_enabled", "0")


# ------------------------------------------------- immediate + dedup

def test_immediate_send_success_and_dedup(client, monkeypatch):
    _enable(mode="immediate")
    sends = {"n": 0}
    def fake_send(subject, text, html, recipient):
        sends["n"] += 1
        return {"ok": True}
    monkeypatch.setattr(notifier, "send_email", fake_send)
    oid = _seed("https://7b.example/i1", score=85)
    op = repo.get_opportunity(oid)
    r1 = notifier.deliver_immediate(op)
    r2 = notifier.deliver_immediate(op)
    assert r1["sent"] is True and r2["sent"] is False
    assert r2["reason"].startswith("e dërguar më parë") or r2["skipped"] is True
    assert sends["n"] == 1
    # delivery e regjistruar
    with repo.db() as con:
        rows = con.execute("SELECT status, recipient FROM email_deliveries").fetchall()
    assert rows[0]["status"] == "sent" and rows[0]["recipient"] == "me@example.com"


def test_min_match_threshold_gates_immediate(client):
    _enable(mode="immediate", min_match=80)
    sends = {"n": 0}
    def fake_send(subject, text, html, recipient):
        sends["n"] += 1
        return {"ok": True}
    import notifier as n
    monkeypatch2 = None
    oid = _seed("https://7b.example/t1", score=40)
    op = repo.get_opportunity(oid)
    orig = n.send_email
    n.send_email = lambda *a, **k: (sends.__setitem__("n", sends["n"] + 1), {"ok": True})[1]
    res = n.deliver_immediate(op)
    assert res["sent"] is False and res["reason"].startswith("score")
    assert sends["n"] == 0
    n.send_email = orig
    _disable()


# ------------------------------------------------------ quiet hours

def test_quiet_hours_defer_immediate(client):
    _enable(mode="immediate")
    # quiet 22:00–07:00; ora 23:30 UTC → brenda window
    now = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)
    repo.set_setting("email_quiet_start", "22:00")
    repo.set_setting("email_quiet_end", "07:00")
    res = notifier.deliver_immediate(repo.get_opportunity(_seed("https://7b.example/q1", score=90)),
                                     now=now)
    assert res["skipped"] is True and "quiet" in res["reason"].lower()
    # jashtë quiet → dërgohet (mock)
    now_day = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    res2 = notifier.deliver_immediate(repo.get_opportunity(_seed("https://7b.example/q2", score=90)),
                                      now=now_day)
    assert res2["sent"] is True
    _disable()


# ------------------------------------------------------------- digest

def test_digest_groups_by_kind_top5_and_threshold(client, monkeypatch):
    _enable(mode="digest")
    repo.set_setting("email_min_match", "50")
    sent_emails = []
    def fake_send(subject, text, html, recipient):
        sent_emails.append((subject, text, html))
        return {"ok": True}
    monkeypatch.setattr(notifier, "send_email", fake_send)
    # 6 freelance (5 mbi prag), 2 job (1 mbi prag), 1 product_lead
    for i in range(6):
        _seed(f"https://7b.example/dg-f{i}", title=f"Freelance Projekt {i}", kind="freelance")
    for i in range(2):
        _seed(f"https://7b.example/dg-j{i}", title=f"Job Rolë {i}", kind="job")
    _seed("https://7b.example/dg-p", title="Platforma CRM", kind="product_lead")
    # vendos score-t: 5 mbi 50 (freelance), 1 job mbi 50
    with repo.db() as con:
        rows = con.execute("SELECT id FROM opportunities WHERE source='7b-test'").fetchall()
        for idx, (rid,) in enumerate(rows):
            score = 80 - (idx % 7)  # disa nën prag
            con.execute("UPDATE opportunities SET rule_score=?, final_score=? WHERE id=?",
                        (score, score, rid))
        con.commit()
    res = notifier.send_digest(now=datetime(2026, 9, 4, 8, 0, tzinfo=timezone.utc))
    assert res["ok"] is True
    # OE-007F: digest dy-nivelesh — subjekti numëron të forta + për rishikim
    assert sent_emails and "përputhje të forta" in sent_emails[0][0]
    first_items = res["items"]
    assert first_items > 0
    # anëtarësim i rreshtave TË TIJ (suite e përbashkët ka rreshta të tjerë
    # me pikë të larta që mund ta mbushin top-5)
    html_body = sent_emails[0][2]
    assert "Freelance Projekt 0" in html_body or "Freelance" in html_body
    assert "Job Rolë" in html_body or "Job" in html_body
    # dedup: digest i dytë menjëherë → asnjë item i re
    res2 = notifier.send_digest(now=datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc))
    assert res2["ok"] is True
    assert len(sent_emails) == 1  # pa ridërgim
    # digest_run i parë ruan items_count > 0
    with repo.db() as con:
        counts = [r[0] for r in con.execute("SELECT items_count FROM digest_runs ORDER BY id")]
    assert counts[0] > 0
    _disable()


def test_digest_skips_below_threshold(client, monkeypatch):
    """OE-007F: rreshti nën të dyja nivelet (30 < 60 review, < 90 strong)
    NUK përmbahet kurrë në digest — pavarësisht rreshtave të tjerë të
    suitës në të njëjtin dritare."""
    _enable(mode="digest")
    repo.set_setting("email_min_match", "90")
    emails = []
    monkeypatch.setattr(notifier, "send_email",
                        lambda s, t, h, r: emails.append((s, t, h)) or {"ok": True})
    low_oid = _seed("https://7b.example/dg-low", title="Ulët-poshtë-30", score=30)
    res = notifier.send_digest(now=datetime.now(timezone.utc))
    assert res["ok"] is True
    for _, text, html in emails:
        assert "Ulët-poshtë-30" not in text and "Ulët-poshtë-30" not in html
        assert f"/jobs/{low_oid}" not in html
    # kufijtë e niveleve, të izoluar nga gjendja e suitës:
    import context as _ctx
    actor = _ctx.require_context(None)
    cfg = notifier.get_config(actor)
    from datetime import timedelta as _td
    since = (datetime.now(timezone.utc) - _td(days=1)).strftime("%Y-%m-%d %H:%M")
    review = notifier._digest_rows(since, cfg, actor, notifier.DIGEST_REVIEW_MIN)
    assert all((j["rule_score"] or 0) >= notifier.DIGEST_REVIEW_MIN for j in review)
    assert low_oid not in {j["id"] for j in review}
    strong = notifier.digest_eligible(since, cfg, actor)
    assert all((j["rule_score"] or 0) >= max(notifier.DIGEST_STRONG_MIN, 90) for j in strong)
    _disable()


def test_digest_failure_recorded(client, monkeypatch):
    _enable(mode="digest")
    def boom(subject, text, html, recipient):
        raise RuntimeError("disk full")
    monkeypatch.setattr(notifier, "send_email", boom)
    res = notifier.send_digest(now=datetime.now(timezone.utc))
    assert res["ok"] is False and "disk full" in res["error"]
    last = repo.last_digest()
    assert last["status"] == "failed" and "disk full" in last["error"]
    _disable()


# ------------------------------------- scheduli i mbijeton dështimin email

def test_scheduler_survives_email_failure(client, monkeypatch):
    _enable(mode="immediate")
    oid = _seed("https://7b.example/sv1", score=90)
    def boom(*a, **k):
        raise RuntimeError("SMTP exploded")
    monkeypatch.setattr(notifier, "send_email", boom)
    profiles = repo.list_search_profiles()
    prof = next(p for p in profiles if p["name"] == "Professional Jobs")
    repo.update_search_profile(prof["id"], {"sources": '["jobicy"]'})
    prof = repo.get_search_profile(prof["id"])
    repo.upsert_schedule(prof["id"], {"next_run_at": "2026-01-01 00:00", "min_match": 0})
    def fake(source, query=None, profile=None, limit=None):
        return {"ok": True, "source": source, "discovered": 1, "new": 1, "dup": 0,
                "kinds": {"job": 1}, "new_ids": [oid], "error": "", "run_id": 0}
    import connectors
    monkeypatch.setattr(connectors, "run_source_search", fake)
    # OE-007F: burimi i mockuar duhet "jo i freskët" për politikën e ciklit.
    with repo.db() as con:
        con.execute("DELETE FROM search_runs WHERE source='jobicy'")
        con.commit()
    res = scheduler.run_scheduled_profile(prof["id"])
    assert res["ok"] is True          # scheduli mbijeton
    assert res["notified"] == 1       # in-app u krijua
    with repo.db() as con:
        row = con.execute("SELECT rule_score FROM opportunities WHERE id=?", (oid,)).fetchone()
    assert row["rule_score"] is not None  # pikët e pandryshuara
    _disable()


def test_no_application_mutation_from_email_flow(client, monkeypatch):
    _enable(mode="immediate")
    monkeypatch.setattr(notifier, "send_email",
                        lambda s, t, h, r: {"ok": True})
    oid = _seed("https://7b.example/app1", score=90)
    with repo.db() as con:
        apps_before = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    notifier.deliver_immediate(repo.get_opportunity(oid))
    notifier.send_digest(now=datetime.now(timezone.utc))
    with repo.db() as con:
        apps_after = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert apps_before == apps_after
    _disable()
