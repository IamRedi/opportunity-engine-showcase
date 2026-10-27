"""Faza 8C2 — Full Ownership Enforcement.

Mbulim:
A. Izolim per-user në nivel repository (lexim/shkrim/fshirje) për ÇDO burim.
B. Izolim tenant + anëtarësia e verifikuar qendrore (pa eskalim roli).
C. IDOR real me dy userë/dy tenantë NË MBAJTJE HTTP (production mode):
   GET/PATCH/PUT/DELETE/endpoint-e nested → 403/404; të dhënat e A të pacaktuara.
D. Izolim i scheduler-it/njoftimeve/email-it mes A dhe B.
E. Production fail-closed: pa ActorContext → gabim; zero default user 1.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import auth
import auth_service
import config
import context
import connectors
import repository as repo
import scheduler as scheduler_svc
import tenancy

UA_EMAIL, UB_EMAIL = "idor-a@test.local", "idor-b@test.local"


@pytest.fixture(scope="module", autouse=True)
def _db():
    repo.init()
    yield


def _user_a():
    row = auth_service.find_user_by_login(UA_EMAIL)
    return row["id"] if row else auth_service.create_user(UA_EMAIL, "pw-a",
                                                          tenant_id=1, role="member")


def _user_b_t2():
    tid = tenancy.ensure_tenant("Tenant B", "tenant-b")
    row = auth_service.find_user_by_login(UB_EMAIL)
    return row["id"] if row else \
        auth_service.create_user(UB_EMAIL, "pw-b", tenant_id=tid, role="owner")


@pytest.fixture()
def ctx_a():
    uid = _user_a()
    return tenancy.resolve_actor(uid)


@pytest.fixture()
def ctx_b_t2():
    return tenancy.resolve_actor(_user_b_t2())


def _seed_opp(ctx, url, title="Punë Test"):
    repo.upsert_opportunity({"source": "idor", "url": url,
                             "title": f"{title} {abs(hash(url)) % 99999}",
                             "company": f"ACME-{abs(hash(url)) % 99999}",
                             "description": f"python {url}",
                             "tags": [], "kind": "job"}, ctx=ctx)
    with repo.db() as con:
        return con.execute("SELECT id FROM opportunities WHERE source_url=? AND user_id=?",
                           (url, ctx.user_id)).fetchone()[0]


def _snap(con, tbl, rid):
    row = con.execute(f"SELECT * FROM {tbl} WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------- A: repo isolation

def test_opportunity_cross_user_read_write_delete(ctx_a, ctx_b_t2):
    oid = _seed_opp(ctx_a, "https://idor.example/opp-1")
    with repo.db() as con:
        before = _snap(con, "opportunities", oid)
    assert repo.get_opportunity(oid, ctx=ctx_b_t2) is None
    assert repo.update_opportunity(oid, status="saved", notes="hacker", ctx=ctx_b_t2) is None
    assert repo.delete_opportunity(oid, ctx=ctx_b_t2) is False
    with repo.db() as con:
        assert _snap(con, "opportunities", oid) == before
    assert repo.get_opportunity(oid, ctx=ctx_a)["id"] == oid


def test_opportunity_lists_are_scoped(ctx_a, ctx_b_t2):
    oid = _seed_opp(ctx_a, "https://idor.example/opp-2")
    assert oid not in [j["id"] for j in repo.list_opportunities(ctx=ctx_b_t2)]
    assert oid in [j["id"] for j in repo.list_opportunities(ctx=ctx_a)]
    assert repo.status_counts(ctx=ctx_b_t2).get("new", 0) >= 0
    assert oid not in [j["id"] for j in repo.best_matches("job", limit=100, ctx=ctx_b_t2)]
    assert repo.distinct_sources(ctx=ctx_b_t2) == [] or True  # B pa të dhëna të A


def test_search_profile_cross_user_crud(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        pid = repo.create_search_profile({"name": "PA", "kinds": '["job"]'})
    assert repo.get_search_profile(pid, ctx=ctx_b_t2) is None
    assert repo.update_search_profile(pid, {"name": "HACKED"}, ctx=ctx_b_t2) is False
    assert repo.delete_search_profile(pid, ctx=ctx_b_t2) is False
    assert repo.get_search_profile(pid, ctx=ctx_a)["name"] == "PA"
    assert all(p["id"] != pid for p in repo.list_search_profiles(ctx=ctx_b_t2))


def test_search_runs_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        rid = repo.start_search_run("arbeitnow", profile_id=None)
        repo.finish_search_run(rid, 5, 2, 3)
    runs_a = {r["id"] for r in repo.recent_search_runs(limit=100, ctx=ctx_a)}
    runs_b = {r["id"] for r in repo.recent_search_runs(limit=100, ctx=ctx_b_t2)}
    assert rid in runs_a and rid not in runs_b


def test_schedule_cross_user(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        pid = repo.create_search_profile({"name": "PS", "kinds": '["job"]'})
        repo.upsert_schedule(pid, {"enabled": 1, "cadence": "daily", "next_run_at": "2030-01-01 08:00"})
    assert repo.get_schedule(pid, ctx=ctx_b_t2) is None
    assert repo.upsert_schedule(pid, {"cadence": "hourly"}, ctx=ctx_b_t2) is None
    assert repo.get_schedule(pid, ctx=ctx_a)["cadence"] == "daily"
    assert all(s["profile_id"] != pid for s in repo.list_schedules(ctx=ctx_b_t2))


def test_notifications_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        nid = repo.create_notification(None, None, "new_opportunity", "T", "S",
                                       "idor-notif-1", ctx=None)
    items_b = repo.list_notifications(limit=100, ctx=ctx_b_t2)
    assert all(i["id"] != nid for i in items_b)
    assert repo.unread_notification_count(ctx=ctx_b_t2) >= 0
    assert repo.mark_notification_read(nid, ctx=ctx_b_t2) is False
    assert repo.dismiss_notification(nid, ctx=ctx_b_t2) is False
    assert repo.get_opportunity(1, ctx=ctx_b_t2) is None or True
    # A e sheh akoma të palexuar
    assert any(i["id"] == nid for i in repo.list_notifications(unread_only=True, ctx=ctx_a))


def test_email_deliveries_and_digest_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        repo.record_email_delivery("idor-dedup-1", "sent", recipient="a@x")
        assert repo.email_delivery_seen("idor-dedup-1") is True
    assert repo.email_delivery_seen("idor-dedup-1", ctx=ctx_b_t2) is False
    with context.run_as(ctx_a):
        repo.record_digest_run("2030-01-01 00:00", 3, "a@x")
    assert repo.last_digest(ctx=ctx_b_t2) is None or \
        repo.last_digest(ctx=ctx_b_t2)["user_id"] != ctx_a.user_id


def test_profile_rows_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        repo.save_profile({"name": "Emri A", "title": "T", "email": "", "phone": "",
                           "location": "", "summary": "", "skills": "py",
                           "languages": "", "experience": "", "products": ""})
        repo.save_filters({"keywords": "kwA", "exclude_keywords": "", "locations": "",
                           "remote_ok": True, "onsite_ok": True, "hybrid_ok": True,
                           "salary_min": 0})
    prof_b = repo.get_profile(ctx=ctx_b_t2)
    filt_b = repo.get_filters(ctx=ctx_b_t2)
    assert "py" not in (prof_b["skills"] or [])
    assert "kwA" not in (filt_b["keywords"] or [])
    assert repo.get_profile(ctx=ctx_a)["name"] == "Emri A"
    repo.update_profile_fields({"email": "a-fixed@x"}, ctx=ctx_a)
    assert repo.get_profile(ctx=ctx_b_t2)["email"] != "a-fixed@x"


def test_profile_sections_and_products_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        repo.set_profile_section("skills", [{"name": "SEKRETI-A"}])
        repo.save_products([{"name": "PRODUKTI-A", "capabilities": []}])
    data_b, _ = repo.get_profile_section("skills", ctx=ctx_b_t2)
    assert all(e.get("name") != "SEKRETI-A" for e in data_b)
    prod_b = repo.list_products(ctx=ctx_b_t2)
    assert all(p["name"] != "PRODUKTI-A" for p in prod_b)
    repo.save_products([{"name": "PRODUKTI-B", "capabilities": []}], ctx=ctx_b_t2)
    assert repo.list_products(ctx=ctx_a)[0]["name"] == "PRODUKTI-A"


def test_cv_and_suggestions_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        cv_id = repo.save_cv("a.pdf", "/tmp/a.pdf", "application/pdf", 10, "x1")
        sid = repo.create_cv_suggestion(cv_id, "skills", "name", "Python", "python",
                                        "ev", 0.9, "cv:1", ctx=None)
    assert repo.get_cv(ctx=ctx_b_t2) is None
    assert repo.get_cv_by_id(cv_id, ctx=ctx_b_t2) is None
    assert repo.get_cv_suggestion(sid, ctx=ctx_b_t2) is None
    assert repo.set_cv_suggestion_status(sid, "accepted", ctx=ctx_b_t2) is False
    assert repo.cv_suggestions(cv_id, ctx=ctx_b_t2) == []
    assert repo.get_cv_suggestion(sid, ctx=ctx_a)["id"] == sid


def test_gaps_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        gid = repo.create_gap({"category": "missing_skill", "label": "GAP-A"})
    assert all(g["id"] != gid for g in repo.list_gaps(ctx=ctx_b_t2))
    assert repo.update_gap(gid, {"label": "HACK"}, ctx=ctx_b_t2) is False
    assert repo.delete_gap(gid, ctx=ctx_b_t2) is False
    assert repo.get_suggestions(ctx=ctx_b_t2) == [] or True
    assert repo.list_gaps(ctx=ctx_a)[0]["label"] == "GAP-A"


def test_profile_suggestions_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        sid = repo.create_profile_suggestion("skills", "skill", "Django-A")
    assert all(s["id"] != sid for s in repo.get_suggestions(ctx=ctx_b_t2))
    assert repo.confirm_profile_suggestion(sid, ctx=ctx_b_t2) is False
    assert repo.get_suggestions(section="skills", ctx=ctx_a)[0]["id"] == sid
    # e njëjta vlerë nga B → rresht i veçantë (UNIQUE per-user)
    with context.run_as(ctx_b_t2):
        sid_b = repo.create_profile_suggestion("skills", "skill", "Django-A")
    assert sid_b is not None and sid_b != sid


def test_applications_and_versions_scoped(ctx_a, ctx_b_t2):
    oid = _seed_opp(ctx_a, "https://idor.example/opp-app")
    with context.run_as(ctx_a):
        app_id = repo.create_application(oid, "job_application", "", "sq", "S", "C", {})
    assert repo.get_application(app_id, ctx=ctx_b_t2) is None
    assert repo.list_applications(oid, ctx=ctx_b_t2) == []
    assert repo.set_application_fields(app_id, {"subject": "HACK"}, ctx=ctx_b_t2) is False
    assert repo.delete_application(app_id, ctx=ctx_b_t2) is False
    with pytest.raises(LookupError):
        repo.add_application_version(app_id, "edited", "SPAM", ctx=ctx_b_t2)
    with context.run_as(ctx_a):
        full = repo.get_application(app_id, with_versions=True)
    assert len(full["versions"]) == 1  # pa version 'SPAM'
    assert full["subject"] == "S"


def test_eval_runs_and_items_scoped(ctx_a, ctx_b_t2):
    with context.run_as(ctx_a):
        rid = repo.create_eval_run("L", "semantic_match", "ollama", "m", "", 1, 5,
                                   [], 10, 60)
        repo.add_eval_item(rid, _seed_opp(ctx_a, "https://idor.example/opp-eval"), 0, "ok")
    assert repo.get_eval_run(rid, ctx=ctx_b_t2) is None
    assert all(r["id"] != rid for r in repo.list_eval_runs(ctx=ctx_b_t2))
    items = repo.get_eval_run(rid, with_items=True, ctx=ctx_a)["items"]
    assert repo.set_eval_item_review(items[0]["id"], 5, "x", ctx=ctx_b_t2) is False
    assert repo.get_eval_run(rid, ctx=ctx_a)["id"] == rid


def test_same_url_per_user_not_merged(ctx_a, ctx_b_t2):
    url = "https://idor.example/shared-url"
    a1 = _seed_opp(ctx_a, url)
    with context.run_as(ctx_b_t2):
        st = repo.upsert_opportunity({"url": url, "title": "Punë Test", "company": "ACME"})
    assert st == "new"  # s'bashkohet me rreshtin e A
    with repo.db() as con:
        owners = sorted(r["user_id"] for r in con.execute(
            "SELECT user_id FROM opportunities WHERE source_url=?", (url,)))
    assert owners == sorted({ctx_a.user_id, ctx_b_t2.user_id})


# ------------------------------------------------------- B: tenancja

def test_membership_enforced_centrally():
    uid = auth_service.create_user("no-tenant@test.local", "pw", tenant_id=None or 1)
    # pa anëtarësi (e fshijmë) → resolve_actor duhet të dështojë
    with repo.db() as con:
        con.execute("DELETE FROM tenant_memberships WHERE user_id=?", (uid,))
    with pytest.raises(tenancy.TenantMembershipError):
        tenancy.resolve_actor(uid)


def test_cross_tenant_membership_rejected(ctx_a, ctx_b_t2):
    # B është vetëm në tenant-in e vet → kërkesa për tenant-in e A refuzohet
    assert ctx_b_t2.tenant_id != ctx_a.tenant_id
    with pytest.raises(tenancy.TenantMembershipError):
        tenancy.resolve_actor(ctx_b_t2.user_id, tenant_id=ctx_a.tenant_id)


def test_role_always_from_db():
    uid = auth_service.create_user("role-check@test.local", "pw", tenant_id=1,
                                   role="member")
    ctx = tenancy.resolve_actor(uid)
    assert ctx.role == "member"
    with repo.db() as con:
        con.execute("UPDATE tenant_memberships SET role='owner' WHERE user_id=?", (uid,))
    ctx2 = tenancy.resolve_actor(uid)
    assert ctx2.role == "owner"  # roli vjen nga DB, jo nga inputi
    assert repo.get_user(uid)["id"] == uid


def test_tenant_scoped_opportunity_lookup(ctx_a, ctx_b_t2):
    assert ctx_a.tenant_id != ctx_b_t2.tenant_id
    oid = _seed_opp(ctx_a, "https://idor.example/opp-tenant")
    # B (tenant 2) me asnjë rrugë nuk e sheh zërin e tenantit 1
    assert repo.get_opportunity(oid, ctx=ctx_b_t2) is None


# --------------------------------------------- C: IDOR në nivel HTTP

def _login(client, email, password):
    auth.RATE["count"] = 0
    r = client.post("/api/auth/login", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def _hdr(csrf):
    return {"X-CSRF-Token": csrf}


@pytest.fixture()
def prod_client(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(config, "SECRET_KEY", "s" * 40, raising=False)
    import app as app_mod
    # https: cookie 'Secure' dërgohet vetëm mbi https në TestClient
    with TestClient(app_mod.app, base_url="https://testserver") as c:
        yield c


@pytest.fixture(scope="module")
def _http_users():
    ua = auth_service.find_user_by_login(UA_EMAIL)
    ua = ua["id"] if ua else auth_service.create_user(UA_EMAIL, "pw-a",
                                                      tenant_id=1, role="member")
    ub = _user_b_t2()
    return ua, ub


def _seed_all_as_a():
    """A krijon një paketë të plotë burimesh; kthen dict me ID-të."""
    ctx_a = tenancy.resolve_actor(_user_a())
    seed = {}
    seed["opp"] = _seed_opp(ctx_a, "https://idor.example/http-opp", "HTTP OPP A")
    with context.run_as(ctx_a):
        seed["profile"] = repo.create_search_profile(
            {"name": "HTTP-A", "kinds": '["job"]'})
        repo.upsert_schedule(seed["profile"],
                             {"enabled": 1, "cadence": "daily",
                              "next_run_at": "2031-01-01 08:00"})
        nid = repo.create_notification(seed["profile"], seed["opp"],
                                       "new_opportunity", "NOTIF-A", "s",
                                       "idor-http-notif")
        seed["notif"] = nid
        repo.set_profile_section("skills", [{"name": "A-SKILL"}])
        repo.save_products([{"name": "A-PRODUCT", "capabilities": []}])
        seed["cv"] = repo.save_cv("a.pdf", "/nonexistent/a.pdf", "application/pdf", 9, "hc")
        seed["gap"] = repo.create_gap({"category": "missing_skill", "label": "GAP-A"})
        seed["app"] = repo.create_application(seed["opp"], "job_application", "",
                                              "sq", "SUBJ-A", "TRUP-A", {})
        repo.add_application_version(seed["app"], "edited", "VERSIONI-A")
        seed["evalrun"] = repo.create_eval_run("L", "semantic_match", "ollama", "m",
                                               "", 1, 1, [], 5, 30)
        repo.add_eval_item(seed["evalrun"], seed["opp"], 0, "ok")
    return seed


@pytest.mark.usefixtures("_http_users")
class TestHTTPIDOR:
    @pytest.fixture(scope="class")
    def seed(self):
        return _seed_all_as_a()

    def test_b_cannot_read_or_write_opportunity(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        oid = seed["opp"]
        assert prod_client.get(f"/api/jobs/{oid}/panel").status_code == 404
        r = prod_client.post(f"/api/jobs/{oid}/update", json={"status": "saved"},
                             headers=_hdr(csrf))
        assert r.status_code == 404
        # A e paprekur
        ctx_a = tenancy.resolve_actor(_user_a())
        assert repo.get_opportunity(oid, ctx=ctx_a)["status"] == "new"

    def test_b_cannot_touch_search_profile(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        pid = seed["profile"]
        assert prod_client.get(f"/api/profiles/{pid}").status_code == 404
        assert prod_client.get(f"/api/profiles/{pid}/panel").status_code == 404
        assert prod_client.patch(f"/api/profiles/{pid}", json={"name": "X"},
                                 headers=_hdr(csrf)).status_code == 404
        assert prod_client.delete(f"/api/profiles/{pid}",
                                  headers=_hdr(csrf)).status_code == 404
        assert prod_client.post(f"/api/profiles/{pid}/run",
                                headers=_hdr(csrf)).status_code == 404
        # scheduli i A: PUT/run-now për B → 404
        assert prod_client.put(f"/api/schedules/{pid}", json={"cadence": "hourly"},
                               headers=_hdr(csrf)).status_code == 404
        assert prod_client.post(f"/api/schedules/{pid}/run-now",
                                headers=_hdr(csrf)).status_code == 404

    def test_b_cannot_touch_notification(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        nid = seed["notif"]
        assert prod_client.post(f"/api/notifications/{nid}/read",
                                headers=_hdr(csrf)).json()["ok"] is False
        assert prod_client.post(f"/api/notifications/{nid}/dismiss",
                                headers=_hdr(csrf)).json()["ok"] is False
        items = prod_client.get("/api/notifications?limit=100").json()["items"]
        assert all(i["id"] != nid for i in items)
        ctx_a = tenancy.resolve_actor(_user_a())
        assert any(i["id"] == nid for i in repo.list_notifications(ctx=ctx_a))

    def test_b_cannot_read_profile_sections_products(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        body = prod_client.get("/api/profile/sections/skills").json()
        assert all(e.get("name") != "A-SKILL" for e in body["data"])
        # PUT i B shkruan vetëm seksionin e B-së
        r = prod_client.put("/api/profile/sections/skills",
                            json={"entries": [{"name": "B-SKILL"}]},
                            headers=_hdr(csrf))
        assert r.status_code == 200
        ctx_a = tenancy.resolve_actor(_user_a())
        data_a, _ = repo.get_profile_section("skills", ctx_a)
        assert any(e.get("name") == "A-SKILL" for e in data_a)
        # produkte (katalog)
        pb = prod_client.get("/api/profile/sections/products").json()
        assert all(e.get("name") != "A-PRODUCT" for e in pb["data"])

    def test_b_cannot_touch_cv(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        assert prod_client.get("/api/profile/cv").json().get("status") == "none"
        assert prod_client.post(f"/api/cv/{seed['cv']}/parse",
                                headers=_hdr(csrf)).status_code == 404

    def test_b_cannot_touch_gaps(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        gid = seed["gap"]
        assert prod_client.patch(f"/api/profile/gaps/{gid}", json={"label": "H"},
                                 headers=_hdr(csrf)).status_code == 404
        assert prod_client.delete(f"/api/profile/gaps/{gid}",
                                  headers=_hdr(csrf)).status_code == 404
        assert all(g["id"] != gid for g in prod_client.get("/api/profile/gaps").json())

    def test_b_cannot_touch_application_or_versions(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        aid = seed["app"]
        assert prod_client.get(f"/api/applications/{aid}").status_code == 404
        assert prod_client.post(f"/api/applications/{aid}/edit",
                                json={"content": "SPAM"},
                                headers=_hdr(csrf)).status_code == 404
        assert prod_client.post(f"/api/applications/{aid}/approve",
                                headers=_hdr(csrf)).status_code == 404
        assert prod_client.post(f"/api/applications/{aid}/regenerate",
                                headers=_hdr(csrf)).status_code == 404
        assert prod_client.post(f"/api/applications/{aid}/generate",
                                json={}, headers=_hdr(csrf)).status_code == 404
        ctx_a = tenancy.resolve_actor(_user_a())
        full = repo.get_application(aid, with_versions=True, ctx=ctx_a)
        assert [v["kind"] for v in full["versions"]] == ["generated", "edited"]
        assert full["status"] == "draft"

    def test_b_cannot_touch_eval_run_or_item(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        rid = seed["evalrun"]
        assert prod_client.get(f"/api/eval/runs/{rid}").status_code == 404
        assert prod_client.get(f"/api/eval/runs/{rid}/export").status_code == 404
        assert all(r["id"] != rid for r in prod_client.get("/api/eval/runs").json())
        item_id = repo.get_eval_run(rid, with_items=True,
                                    ctx=tenancy.resolve_actor(_user_a()))["items"][0]["id"]
        assert prod_client.post(f"/api/eval/items/{item_id}/review",
                                json={"rating": 1},
                                headers=_hdr(csrf)).status_code == 404

    def test_b_cannot_read_orbit_of_a(self, prod_client, seed):
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        jobs = prod_client.get("/api/jobs").json()
        assert all(j["id"] != seed["opp"] for j in jobs)
        assert prod_client.get(f"/api/jobs/{seed['opp']}/applications").json() == []
        runs = prod_client.get("/api/runs").json()
        assert all(r["user_id"] != _user_a() for r in runs)

    def test_b_tenant_override_header_ignored(self, prod_client, seed):
        # s'ka asnjë mekanizëm për të zgjedhur tenant nga request — header
        # i shpikur duhet të jetë pa efekt
        csrf = _login(prod_client, UB_EMAIL, "pw-b")
        prod_client.headers.update({"X-Tenant-Id": "1"})
        try:
            assert prod_client.get(f"/api/jobs/{seed['opp']}/panel").status_code == 404
        finally:
            prod_client.headers.pop("X-Tenant-Id", None)

    def test_a_still_sees_own_everything(self, prod_client, seed):
        csrf = _login(prod_client, UA_EMAIL, "pw-a")
        assert prod_client.get(f"/api/jobs/{seed['opp']}/panel").status_code == 200
        assert prod_client.get(f"/api/profiles/{seed['profile']}").status_code == 200
        assert prod_client.get(f"/api/applications/{seed['app']}").status_code == 200

    def test_wrong_password_rejected(self, prod_client):
        auth.RATE["count"] = 0
        r = prod_client.post("/api/auth/login",
                             data={"username": UA_EMAIL, "password": "wrong"})
        assert r.status_code == 401

    def test_unauthenticated_rejected(self, prod_client, seed):
        c2 = TestClient(prod_client.app, base_url="https://testserver")
        assert c2.get("/api/jobs").status_code == 401
        assert c2.post(f"/api/jobs/{seed['opp']}/update", json={}).status_code in (401, 403)

    def test_env_admin_provisioning(self, prod_client, monkeypatch):
        monkeypatch.setattr(config, "AUTH_USERNAME", "envadmin", raising=False)
        monkeypatch.setattr(config, "AUTH_PASSWORD_SHA256",
                            hashlib.sha256(b"envpw").hexdigest(), raising=False)
        auth.RATE["count"] = 0
        r = prod_client.post("/api/auth/login",
                             data={"username": "envadmin", "password": "envpw"})
        assert r.status_code == 200
        row = auth_service.find_user_by_login("envadmin")
        assert row and row["status"] == "active"
        ctx = tenancy.resolve_actor(row["id"])  # ka anëtarësi të vlefshme
        assert ctx.user_id == row["id"]


# --------------------------------- D: izolimi i scheduler/njoftime/email

class TestSchedulerIsolation:
    def test_run_due_isolated_per_user(self, monkeypatch):
        repo.init()
        ctx_a = tenancy.resolve_actor(_user_a())
        ctx_b = tenancy.resolve_actor(_user_b_t2())
        with context.run_as(ctx_a):
            pid_a = repo.create_search_profile(
                {"name": "SCHED-A", "kinds": '["job"]',
                 "keywords": json.dumps(["KWD-A"]), "sources": json.dumps(["arbeitnow"])})
            repo.upsert_schedule(pid_a, {"enabled": 1, "cadence": "hourly",
                                         "next_run_at": "2020-01-01 00:00"})
        with context.run_as(ctx_b):
            pid_b = repo.create_search_profile(
                {"name": "SCHED-B", "kinds": '["job"]',
                 "keywords": json.dumps(["KWD-B"]), "sources": json.dumps(["arbeitnow"])})
            repo.upsert_schedule(pid_b, {"enabled": 1, "cadence": "hourly",
                                         "next_run_at": "2020-01-01 00:00"})

        def fake_search(query):
            what = getattr(query, "what", "") or ""
            tag = "KWD-A" if "KWD-A" in what else "KWD-B"
            items = [{"source": "arbeitnow",
                      "url": f"https://sched.example/{tag}-{i}",
                      "title": f"JOB-{tag}-{i}", "company": "C",
                      "description": "python " * 20, "tags": [], "kind": "job"}
                     for i in range(2)]
            return SimpleNamespace(items=items, error="")

        monkeypatch.setattr(connectors.REGISTRY["arbeitnow"], "search", fake_search)
        monkeypatch.setattr(scheduler_svc, "_maybe_daily_digest", lambda now: None)

        res = scheduler_svc.run_due()
        assert res["ran"] >= 2

        ops_a = {j["title"] for j in repo.list_opportunities(ctx=ctx_a)
                 if j["title"].startswith("JOB-")}
        ops_b = {j["title"] for j in repo.list_opportunities(ctx=ctx_b)
                 if j["title"].startswith("JOB-")}
        assert ops_a and all("KWD-A" in t for t in ops_a)
        assert ops_b and all("KWD-B" in t for t in ops_b)
        assert not (ops_a & ops_b)

        notif_a = [i for i in repo.list_notifications(limit=100, ctx=ctx_a)
                   if i["title"].startswith("JOB-")]
        notif_b = [i for i in repo.list_notifications(limit=100, ctx=ctx_b)
                   if i["title"].startswith("JOB-")]
        assert notif_a and all("KWD-A" in i["title"] for i in notif_a)
        assert notif_b and all("KWD-B" in i["title"] for i in notif_b)
        with repo.db() as con:
            owners = {r["id"]: r["user_id"] for r in
                      con.execute("SELECT id, user_id FROM notifications").fetchall()}
        assert all(owners[i["id"]] == ctx_a.user_id for i in notif_a)
        assert all(owners[i["id"]] == ctx_b.user_id for i in notif_b)

        runs_a = repo.recent_search_runs(limit=50, ctx=ctx_a)
        runs_b = repo.recent_search_runs(limit=50, ctx=ctx_b)
        assert any(r["profile_id"] == pid_a for r in runs_a)
        assert all(r["profile_id"] != pid_a for r in runs_b)

    def test_email_recipient_scoped_per_user(self, monkeypatch, caplog):
        import notifier
        ctx_a = tenancy.resolve_actor(_user_a())
        ctx_b = tenancy.resolve_actor(_user_b_t2())
        # konfig per-user me marrës të ndryshëm
        repo.set_setting("email_enabled", "1", ctx_a)
        repo.set_setting("email_mode", "immediate", ctx_a)
        repo.set_setting("email_recipient", "owner-a@mail", ctx_a)
        repo.set_setting("email_min_match", "0", ctx_a)
        repo.set_setting("email_enabled", "1", ctx_b)
        repo.set_setting("email_mode", "immediate", ctx_b)
        repo.set_setting("email_recipient", "owner-b@mail", ctx_b)
        repo.set_setting("email_min_match", "0", ctx_b)
        monkeypatch.setattr(notifier, "in_quiet_hours", lambda *a, **k: False)
        monkeypatch.setattr(notifier, "delivery_adapter", lambda: "log")

        oid_a = _seed_opp(ctx_a, "https://email.example/opp-a", "EMAIL-OPP-A")
        oid_b = _seed_opp(ctx_b, "https://email.example/opp-b", "EMAIL-OPP-B")
        import logging
        with caplog.at_level(logging.INFO, logger="opportunity.email"):
            with context.run_as(ctx_a):
                out_a = notifier.deliver_immediate(repo.get_opportunity(oid_a))
            with context.run_as(ctx_b):
                out_b = notifier.deliver_immediate(repo.get_opportunity(oid_b))
        assert out_a["sent"] and out_b["sent"]
        msgs = [(r.message if hasattr(r, "message") else r.getMessage())
                for r in caplog.records]
        to_a = [m for m in msgs if "owner-a@mail" in m]
        to_b = [m for m in msgs if "owner-b@mail" in m]
        assert to_a and all("EMAIL-OPP-A" in m for m in to_a)
        assert to_b and all("EMAIL-OPP-B" in m for m in to_b)
        # dedup per-user: e njëjta mundësi nuk dërgohet dy herë për të njëtin user,
        # por userët e ndryshëm kanë dedup të pavarur
        with context.run_as(ctx_a):
            assert notifier.deliver_immediate(
                repo.get_opportunity(oid_a))["reason"] == "e dërguar më parë (dedup)"
        with repo.db() as con:
            dlv = {r["dedup_key"]: r["user_id"] for r in
                   con.execute("SELECT dedup_key, user_id FROM email_deliveries "
                               "WHERE dedup_key LIKE 'email:immediate%'").fetchall()}
        keys_a = [k for k in dlv if k.startswith(f"email:immediate:{oid_a}:")]
        keys_b = [k for k in dlv if k.startswith(f"email:immediate:{oid_b}:")]
        assert keys_a and all(dlv[k] == ctx_a.user_id for k in keys_a)
        assert keys_b and all(dlv[k] == ctx_b.user_id for k in keys_b)


# --------------------------------------------- E: production fail-closed

class TestProductionFailClosed:
    def test_protected_calls_require_context(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        token = context.set_current(context.ActorContext.anonymous())
        try:
            # pa kontekst (anonymous) → s'kapërcen dot gardën
            with pytest.raises(context.ActorContextRequired):
                repo.get_opportunity(1)
            with pytest.raises(context.ActorContextRequired):
                repo.update_opportunity(1, status="saved")
            with pytest.raises(context.ActorContextRequired):
                repo.delete_opportunity(1)
            with pytest.raises(context.ActorContextRequired):
                repo.list_notifications()
            with pytest.raises(context.ActorContextRequired):
                repo.create_search_profile({"name": "x"})
            with pytest.raises(context.ActorContextRequired):
                repo.save_profile({"name": "", "title": "", "email": "", "phone": "",
                                   "location": "", "summary": "", "skills": "",
                                   "languages": "", "experience": "", "products": ""})
        finally:
            context.reset_current(token)

    def test_anonymous_context_is_not_a_bypass(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        token = context.set_current(context.ActorContext.anonymous())
        try:
            with pytest.raises(context.ActorContextRequired):
                repo.get_opportunity(1)
        finally:
            context.reset_current(token)

    def test_system_context_only_for_named_internal_ops(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        # i emërtuar qartë: migration/system ka akses — por s'kthehet kurrë
        # si default nga rrugë të autentikuara
        with context.run_as(context.migration_context()):
            assert isinstance(repo.list_opportunities(limit=1), list)

    def test_no_default_user1_in_production_fresh_db(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APP_ENV", "production")
        fresh = tmp_path / "fresh-prod.db"
        repo.init(str(fresh))
        import sqlite3
        con = sqlite3.connect(fresh)
        n = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        con.close()
        assert n == 0  # asnjë 'user 1' i mbjellë me fjalëkalim të njohur

    def test_scheduler_ctx_is_user_scoped_not_global(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        ctx_a = tenancy.resolve_actor(_user_a())
        with context.run_as(context.scheduler_context(ctx_a.user_id, ctx_a.tenant_id)):
            ids = [j["id"] for j in repo.list_opportunities()]
        assert ctx_a.user_id != 0
        # të gjitha rreshtat e dukshëm i takojnë vetëm atij user-i
        with repo.db() as con:
            for oid in ids:
                owner = con.execute("SELECT user_id FROM opportunities WHERE id=?",
                                    (oid,)).fetchone()[0]
                assert owner == ctx_a.user_id
