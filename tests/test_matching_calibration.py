"""OE-007B — Kalibrimi i skorit: vetitë e formulas së re "skills" (v4-relevance).

Kontrata e ruajtur: MATCH/MISMATCH/UNKNOWN, përjashtimet e forta, determinizmi,
emëruesi pa UNKNOWN. E re: gjerësia e profilit s'ndëshkon (termat e papërmendur
nuk hyjnë në formulë), dëshmia saturohet te SKILL_SATURATION_POINTS, dhe
kalimi i nxehtë i rescore ngarkon gaps/preferencat një herë.
"""
import json
from datetime import datetime, timedelta, timezone

import matching
import repository as repo

# Kjo skedë sorton pas test_matching.py dhe s'importon kurrë `app` —
# inicializo skemën vetë (idempotent) që të mos varet nga rendi i skedarëve.
repo.init()


def _op(**kw):
    title = kw.pop("title", "Developer")
    url = kw.pop("url", f"https://calib-test.example/{abs(hash((title, str(kw)))) % 10**9}")
    base = {
        "source": "test", "url": url, "title": title, "company": "ACME",
        "location": "Berlin", "remote": False, "description": "python backend",
        "tags": [], "kind": "job", "posted_at": "", "salary_min": None,
    }
    base.update(kw)
    return base


CORE = ["python", "react", "typescript", "node.js", "postgresql"]


def _prof(**kw):
    base = {
        "skills": CORE, "keywords": [], "exclude_keywords": [], "location": "",
        "salary_min": 0, "languages": [], "title": "", "experience": "",
        "products": [], "remote_ok": 1, "onsite_ok": 1, "hybrid_ok": 1,
        "hourly_rate_min": 0, "radius_km": None,
    }
    base.update(kw)
    return base


def _recent(days=1):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


def _skills_factor(result):
    return next(f for f in result.factors if f.id == "skills")


# 1) Vetia kryesore: gjerësia e profilit nuk ndëshkon.
def test_broad_profile_scores_like_narrow_one():
    broad = _prof(skills=CORE + [f"habilidade-{i}" for i in range(59)])
    narrow = _prof()
    op = _op(title="Senior Python Developer",
             description="react typescript node.js postgresql python")
    r_broad, r_narrow = matching.score(op, broad, broad), matching.score(op, narrow, narrow)
    assert _skills_factor(r_broad).value == _skills_factor(r_narrow).value
    assert r_broad.final_score == r_narrow.final_score


# 2) Renditja: i fortë > mesatar > i palidhëm.
def test_strong_above_medium_above_irrelevant():
    strong = matching.score(_op(title="Senior Python Developer",
                                description="react typescript node.js postgresql python"),
                            _prof(), _prof())
    medium = matching.score(_op(title="Backend Developer", description="python"),
                            _prof(), _prof())
    irrelevant = matching.score(_op(title="Steuerfachangestellte",
                                    description="buchhaltung datev"), _prof(), _prof())
    assert strong.final_score > medium.final_score > irrelevant.final_score
    assert _skills_factor(strong).status == matching.MATCH
    assert _skills_factor(medium).status == matching.MATCH
    assert _skills_factor(irrelevant).status == matching.MISMATCH
    assert _skills_factor(irrelevant).value == 0


# 3) Saturoimi: 4 terma në titull (8 pikë) ose 8 në tekst → 100, jo më shumë.
def test_saturation_at_sufficiency_anchor():
    prof = _prof(skills=CORE + ["docker", "vitest", "playwright", "linux",
                                "vercel", "stripe", "paddle"])
    four_title = matching.score(_op(title="python react typescript postgresql developer"),
                                prof, prof)
    assert _skills_factor(four_title).value == 100  # 4×2 = 8 pikë
    eight_desc = matching.score(_op(title="Unrelated Role",
                                    description="python react typescript node.js postgresql "
                                                "docker vitest playwright"),
                                prof, prof)
    assert _skills_factor(eight_desc).value == 100  # 8×1 = 8 pikë
    one_title = matching.score(_op(title="Python Developer"), prof, prof)
    assert _skills_factor(one_title).value == 25  # 2/8 — nën pragun e mjaftueshmërisë


# 4) Dëshmia e titullit rëndohet dyfish: 1 titull (2p) > 1 përshkrim (1p),
#    dhe 1 titull baraz 2 përshkrime (2p = 2p).
def test_title_evidence_outweighs_description_evidence():
    one_title = matching.score(_op(title="Python Developer",
                                   description="tekst pa terma përkatëse"),
                               _prof(), _prof())
    one_desc = matching.score(_op(title="Unrelated Role", description="python"),
                              _prof(), _prof())
    assert _skills_factor(one_title).value > _skills_factor(one_desc).value
    two_desc = matching.score(_op(title="Unrelated Role", description="python react"),
                              _prof(), _prof())
    assert _skills_factor(two_desc).value == _skills_factor(one_title).value


# 5) UNKNOWN nuk llogaritet negativ (ruajtur nga v3b).
def test_unknown_information_is_not_negative():
    prof, flt = _prof(), _prof(salary_min=50000)
    no_salary = matching.score(_op(), prof, flt)
    bad_salary = matching.score(_op(salary_min=30000), prof, flt)
    assert no_salary.final_score > bad_salary.final_score


# 6) Përjashtimi i fortë mbetet autoritar pavarësisht pikëve të larta.
def test_hard_exclusion_remains_authoritative():
    flt = _prof(exclude_keywords=["unpaid"])
    r = matching.score(_op(title="Python Developer", description="unpaid internship python"),
                       _prof(), flt)
    assert r.excluded is True and r.exclusions == ["unpaid"]
    # me mbulim të plotë — ende i përjashtuar
    r2 = matching.score(_op(title="python react typescript postgresql",
                            description="unpaid"), _prof(), flt)
    assert r2.excluded is True


# 7) Tri-gjendjet dhe kufijtë e kontratës mbeten të paprekur.
def test_factor_contract_intact():
    r = matching.score(_op(title="Python Developer", posted_at=_recent(2)),
                       _prof(), _prof())
    assert 0 <= r.final_score <= 100
    for f in r.factors:
        assert f.weight > 0
        if f.status == matching.UNKNOWN:
            assert f.value is None
        else:
            assert 0 <= f.value <= 100
    known = [f for f in r.factors if f.status != matching.UNKNOWN]
    expected = round(sum((f.value or 0) * f.weight for f in known) / sum(f.weight for f in known))
    assert r.final_score == max(0, min(100, expected))


# 8) Versioni i ri i skorimit persistohet me rreshtin.
def test_version_persisted_in_match_factors():
    repo.save_profile({**repo.get_profile(), "skills": "python"})
    # Titull/organizatë unike — L3-dedup i gabuar do ta shkrinte me rreshta
    # të tjerë të suite-s dhe SELECT-i sipas URL-së do të gjente asgjë.
    repo.upsert_opportunity(_op(title="Calibration Python Developer 8B",
                                company="CalibCo-8B", location="Calibstadt",
                                url="https://calib-test.example/ver-1"))
    with repo.db() as con:
        row = con.execute(
            "SELECT rule_score, match_factors FROM opportunities WHERE source_url='https://calib-test.example/ver-1'"
        ).fetchone()
    assert row is not None, "upsert s'krijoi rreshtin (dedup e shkrinti?)"
    bd = json.loads(row["match_factors"])
    assert bd["version"] == matching.VERSION == "4-relevance"
    assert row["rule_score"] == bd["final_score"]
    assert any(f["id"] == "skills" for f in bd["factors"])
    # Format i ri dëshmie — i verifikuar me thirrje të pastër (pa gjendje DB).
    r_direct = matching.score(_op(title="Python Developer"), _prof(), _prof())
    assert _skills_factor(r_direct).evidence == "1 terma përkatës (1 në titull)"


# 9) Krediti i gap-eve "po mësohet" zbatohet edhe me formulën e re.
def test_learning_gap_credit_applies():
    prof = _prof(skills=CORE + ["kubernetes"])
    gaps = [{"active": 1, "willingness": "learning", "label": "kubernetes",
             "severity": "soft", "target_state": ""}]
    without = matching.score(_op(title="Python Developer"), prof, prof)
    with_credit = matching.score(_op(title="Python Developer"), prof, prof, gaps=gaps)
    assert _skills_factor(without).value == 25
    assert _skills_factor(with_credit).value == 40  # 25 + 15
    assert "kubernetes" in _skills_factor(with_credit).evidence


# 10) Kalimi i nxehtë: rescore_all pyet gaps/preferencat NJË herë, jo për rresht.
def test_rescore_loads_gaps_and_local_prefs_once(monkeypatch):
    for i in range(5):
        repo.upsert_opportunity(_op(title=f"Python Developer {i}",
                                    url=f"https://calib-test.example/hot-{i}"))
    calls = {"gaps": 0, "prefs": 0}
    real_gaps, real_prefs = repo.list_gaps, repo.get_local_prefs

    def gaps_counter(*a, **kw):
        calls["gaps"] += 1
        return real_gaps(*a, **kw)

    def prefs_counter(*a, **kw):
        calls["prefs"] += 1
        return real_prefs(*a, **kw)

    monkeypatch.setattr(repo, "list_gaps", gaps_counter)
    monkeypatch.setattr(repo, "get_local_prefs", prefs_counter)
    n = repo.rescore_all()
    assert n >= 5
    assert calls["gaps"] == 1, f"list_gaps u thirr {calls['gaps']} herë"
    # 2 = ngarkimi paraprak i rescore + vetë get_profile (stated_preferences);
    # thelbësore: jo një thirrje për rresht.
    assert calls["prefs"] == 2, f"get_local_prefs u thirr {calls['prefs']} herë"


# 11) Seti i vlerësimit: vetitë e pranuara mbi raste sintetike të plota.
def test_evaluation_set_ranking_properties():
    prof = _prof(title="Full-Stack Developer", location="Berlin",
                 languages=["Gjermanisht – B2"], experience="senior python developer")
    flt = _prof(salary_min=40000, location="Berlin", exclude_keywords=["unpaid"])
    ops = {
        "strong": _op(title="Senior Python Developer",
                      description="react typescript node.js postgresql python",
                      location="Berlin", salary_min=60000, posted_at=_recent(2)),
        "medium": _op(title="Backend Developer", description="python api",
                      location="Berlin", posted_at=_recent(20)),
        "weak": _op(title="Unrelated Role", description="sales marketing",
                    location="München", posted_at=_recent(40)),
        "irrelevant": _op(title="Steuerfachangestellte", description="datev buchhaltung",
                          location="Hamburg", posted_at=_recent(90)),
        "strong_but_excluded": _op(title="python react typescript postgresql",
                                   description="unpaid internship", location="Berlin",
                                   posted_at=_recent(2)),
    }
    results = {name: matching.score(op, prof, flt) for name, op in ops.items()}
    # renditja bërthamë
    assert results["strong"].final_score > results["medium"].final_score
    assert results["medium"].final_score > results["weak"].final_score
    assert results["weak"].final_score >= results["irrelevant"].final_score
    # përjashtimi i fortë mposht edhe përputhjen e plotë
    assert results["strong_but_excluded"].excluded is True
    # determinizëm mbi gjithë setin
    again = {name: matching.score(op, prof, flt) for name, op in ops.items()}
    for name in ops:
        assert results[name].factor_dict() == again[name].factor_dict()
    # shpjegueshmëria: çdo faktor ka dëshmi / arsyetim të tri-gjendjeve
    for r in results.values():
        for f in r.factors:
            assert f.status in (matching.MATCH, matching.MISMATCH, matching.UNKNOWN)
            assert f.evidence or f.status == matching.UNKNOWN or f.value == 100
