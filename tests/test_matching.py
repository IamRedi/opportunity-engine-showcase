"""Kontrata e skorimit (Faza 3A): determinizëm, tri-gjendje, kufij, kompatibitet."""
import json
from datetime import datetime, timedelta, timezone

import matching
import repository as repo


def _op(**kw):
    url = f"https://match-test.example/{kw.get('title', 'x')}"
    base = {
        "source": "test", "url": url,
        "title": "Developer", "company": f"ACME-{abs(hash(url)) % 100000}", "location": "Berlin",
        "remote": False, "description": "python backend", "tags": [],
        "kind": "job", "posted_at": "", "salary_min": None,
    }
    base.update(kw)
    return base


def _prof(**kw):
    base = {
        "skills": ["python"], "keywords": [], "exclude_keywords": [],
        "location": "", "salary_min": 0, "languages": [], "title": "",
        "experience": "", "products": [], "remote_ok": 1, "onsite_ok": 1,
        "hybrid_ok": 1, "hourly_rate_min": 0, "radius_km": None,
    }
    base.update(kw)
    return base


def _recent(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


# 1) determinizëm
def test_identical_input_identical_score():
    op, prof, flt = _op(), _prof(), _prof()
    r1, r2 = matching.score(op, prof, flt), matching.score(op, prof, flt)
    assert r1.final_score == r2.final_score
    assert r1.factor_dict() == r2.factor_dict()


# 2) paga e panjohur s'ndëshkon
def test_unknown_salary_does_not_lower_score():
    prof, flt = _prof(), _prof(salary_min=50000)
    no_salary = matching.score(_op(), prof, flt)
    good_salary = matching.score(_op(salary_min=60000), prof, flt)
    bad_salary = matching.score(_op(salary_min=30000), prof, flt)
    f_no = next(f for f in no_salary.factors if f.id == "salary")
    f_good = next(f for f in good_salary.factors if f.id == "salary")
    f_bad = next(f for f in bad_salary.factors if f.id == "salary")
    assert f_no.status == matching.UNKNOWN
    assert f_good.status == matching.MATCH and f_bad.status == matching.MISMATCH
    # unknown (62) > mismatch — mungesa e të dhënave nuk është negative
    assert no_salary.final_score > bad_salary.final_score
    assert good_salary.final_score > bad_salary.final_score
    # pragu i profilit nuk ka efekt kur pagës i mungon të dhëna (degë unknown identike)
    no_salary_floor0 = matching.score(_op(), prof, _prof(salary_min=0))
    assert no_salary_floor0.final_score == no_salary.final_score


# 3) përjashtimi i fortë
def test_excluded_keyword_is_hard_mismatch():
    flt = _prof(exclude_keywords=["unpaid"])
    r = matching.score(_op(description="unpaid internship python"), _prof(), flt)
    assert r.excluded is True
    assert r.exclusions == ["unpaid"]
    # i pavarur nga pikët — edhe me përputhje të plotë mbetet i përjashtuar
    r2 = matching.score(_op(title="Python Developer", description="python unpaid"), _prof(), flt)
    assert r2.excluded is True


# 4) sjellja e preferencës remote
def test_remote_preference_behavior():
    flt_remote_ok = _prof()
    flt_no_remote = _prof(remote_ok=0, onsite_ok=1, hybrid_ok=1)
    r_ok = matching.score(_op(remote=True), _prof(), flt_remote_ok)
    r_no = matching.score(_op(remote=True), _prof(), flt_no_remote)
    wm_ok = next(f for f in r_ok.factors if f.id == "workmode")
    wm_no = next(f for f in r_no.factors if f.id == "workmode")
    assert wm_ok.status == matching.MATCH and wm_ok.value == 100
    assert wm_no.status == matching.MISMATCH and wm_no.value == 0
    assert r_no.final_score < r_ok.final_score
    # onsite + filtra vetëm remote → mismatch
    r_onsite = matching.score(_op(remote=False), _prof(), _prof(remote_ok=1, onsite_ok=0, hybrid_ok=0))
    assert any(f.status == matching.MISMATCH for f in r_onsite.factors if f.id == "workmode")


# 5) profil pa aftësi
def test_empty_skills_profile():
    r = matching.score(_op(), _prof(skills=[], keywords=[]), _prof())
    sk = next(f for f in r.factors if f.id == "skills")
    assert sk.status == matching.UNKNOWN
    assert "skills" in r.unknown
    # s'është 0 nga aftësitë — faktorët e tjerë të njohur mbajnë pikët
    assert r.final_score >= 0


# 6) pesha e freskësisë
def test_freshness_weighting():
    prof, flt = _prof(), _prof()
    fresh = matching.score(_op(posted_at=_recent(1)), prof, flt)
    stale = matching.score(_op(posted_at=_recent(90)), prof, flt)
    f_fresh = next(f for f in fresh.factors if f.id == "freshness")
    f_stale = next(f for f in stale.factors if f.id == "freshness")
    assert f_fresh.value == 100 and f_stale.value == 10
    assert fresh.final_score > stale.final_score
    # pa datë fare → unknown
    nodate = matching.score(_op(posted_at="", discovered_at=""), prof, flt)
    assert next(f for f in nodate.factors if f.id == "freshness").status == matching.UNKNOWN


# 7) lloje të ndryshme mundësish
def test_different_kinds_score_with_own_factors():
    prof = _prof(products=["BusinessSite RC"], skills=["python"], keywords=["booking"])
    flt = _prof(keywords=["booking"])
    fr = matching.score(_op(kind="freelance", opportunity_type="Website", salary_min=50000,
                            title="Booking platform", description="python"), prof, flt)
    assert {f.id for f in fr.factors} == {"skills", "project_type", "budget", "workmode", "availability"}
    pl = matching.score(_op(kind="product_lead", opportunity_type="Booking",
                            title="Business needs booking", description="python"), prof, flt)
    assert {f.id for f in pl.factors} == {"capability", "problem", "customization"}
    lo = matching.score(_op(kind="local_job", distance_km=3.0, title="Pet sitting",
                            description="python"), prof, _prof(radius_km=10))
    # 5D: + freshness; category = preferencat lokale (jo aftësitë)
    assert {f.id for f in lo.factors} == {"distance", "pay", "category", "transport",
                                          "schedule", "freshness", "constraints"}
    # lloj i panjohur → fallback job
    fb = matching.score(_op(kind="alien"), prof, flt)
    assert fb.kind == "alien" and fb.factors


# 8) kufijtë dhe totali i faktorëve
def test_factor_bounds_and_formula():
    prof, flt = _prof(skills=["python"], languages=["Gjermanisht – B2"], title="Developer",
                      location="Berlin"), _prof(salary_min=40000)
    r = matching.score(_op(salary_min=50000, location="Berlin",
                           posted_at=_recent(2)), prof, flt)
    assert 0 <= r.final_score <= 100
    for f in r.factors:
        assert f.weight > 0
        if f.status == matching.UNKNOWN:
            assert f.value is None
        else:
            assert 0 <= f.value <= 100
    # formula: emëruesi përjashton unknown-at
    known = [f for f in r.factors if f.status != matching.UNKNOWN]
    expected = round(sum((f.value or 0) * f.weight for f in known) / sum(f.weight for f in known))
    assert r.final_score == max(0, min(100, expected))
    # unknown-at janë vetëm jashtë formulës, jo negative
    assert all(f.id in r.unknown for f in r.factors if f.status == matching.UNKNOWN)


# 9) kompatibiteti legacy: API/repository/UI
def test_legacy_compat_repository_and_ui():
    repo.upsert_opportunity(_op(title="Python Developer", url="https://match-test.example/legacy-1"))
    # score_job kthen (int, bool)
    s, e = repo.score_job(_op(title="Python Developer"))
    assert isinstance(s, int) and 0 <= s <= 100 and e is False
    # match_factors persistohet në upsert
    with repo.db() as con:
        row = con.execute(
            "SELECT match_factors, rule_score FROM opportunities WHERE source_url LIKE 'https://match-test.example/legacy-1'"
        ).fetchone()
    bd = json.loads(row["match_factors"])
    assert bd["version"] == matching.VERSION
    assert row["rule_score"] == bd["final_score"]
    assert any(f["id"] == "skills" for f in bd["factors"])
    # paneli renderon faktorët strukturore
    from fastapi.testclient import TestClient
    import app
    client = TestClient(app.app)
    jid = con_id = None
    rows = client.get("/api/jobs").json()
    jid = next(j["id"] for j in rows if j["url"] == "https://match-test.example/legacy-1")
    r = client.get(f"/api/jobs/{jid}/panel")
    assert r.status_code == 200
    assert "Përse përputhet" in r.text and "Aftësi" in r.text


def test_rescore_persists_structured_factors():
    repo.upsert_opportunity(_op(title="X", url="https://match-test.example/rs-1"))
    n = repo.rescore_all()
    assert n > 0
    with repo.db() as con:
        row = con.execute(
            "SELECT match_factors FROM opportunities WHERE source_url='https://match-test.example/rs-1'"
        ).fetchone()
    bd = json.loads(row["match_factors"])
    assert "factors" in bd and bd["version"] == matching.VERSION
