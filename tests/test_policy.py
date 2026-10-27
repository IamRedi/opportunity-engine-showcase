"""Politikat e skorimit (Faza 3D): determinizëm, përjashtime, abstention, kufij."""
import pytest

import policy


def _ai(score=60, desc_len=1200, matched=2, missing=1, ok=True):
    return {"ok": ok, "score": score, "desc_len": desc_len,
            "matched_n": matched, "missing_n": missing}


# 1) determinizëm
def test_policies_deterministic():
    kwargs = dict(rule=50, excluded=False, ai=_ai())
    outs = [policy.apply("weighted_blend", **kwargs) for _ in range(5)]
    assert all(o == outs[0] for o in outs)
    rep1 = policy.analyze([{"rule": 50, "excluded": False, "ai": _ai()}])
    rep2 = policy.analyze([{"rule": 50, "excluded": False, "ai": _ai()}])
    assert rep1 == rep2


# 2) përjashtimi i fortë s'kapërcyhet kurrë
def test_hard_exclusion_cannot_be_overridden():
    for pol in policy.POLICIES:
        r = policy.apply(pol, rule=0, excluded=True, ai=_ai(score=95))
        assert r["final"] == 0 and r["used_ai"] is False and r["abstained"] is True


# 3) abstention funksionon
def test_abstention_on_insufficient_evidence():
    # përshkrim i shkurtër → abstain në çdo politikë me AI
    for pol in ("weighted_blend", "abstain_insufficient", "ai_high_confidence"):
        r = policy.apply(pol, rule=40, excluded=False, ai=_ai(desc_len=100))
        assert r["abstained"] is True and r["final"] == 40 and r["used_ai"] is False
    # AI jo-ok → abstain
    r = policy.apply("weighted_blend", 40, False, _ai(ok=False))
    assert r["abstained"] is True and r["final"] == 40
    # pa matched/missing → abstain (s'ka dëshmi puntore)
    r = policy.apply("weighted_blend", 40, False, _ai(matched=0, missing=0))
    assert r["abstained"] is True


# 4) threshold-i i besueshmërisë
def test_confidence_threshold_gate():
    ai_close = _ai(score=52)   # |52-50|=2 → marrëveshje 98 ≥ 60 → blend
    ai_far = _ai(score=95)     # |95-50|=45 → marrëveshje 55 < 60 → rule
    r_close = policy.apply("ai_high_confidence", 50, False, ai_close)
    r_far = policy.apply("ai_high_confidence", 50, False, ai_far)
    assert r_close["used_ai"] is True and r_close["final"] != 50
    assert r_far["used_ai"] is False and r_far["final"] == 50


# 5) kufijtë e ndikimit dhe bound-et
def test_influence_cap_and_bounds():
    # AI ekstremisht i lartë → final s'mund të tejkalojë rule+MAX_INFLUENCE
    r = policy.apply("weighted_blend", 30, False, _ai(score=100))
    assert r["final"] == 30 + policy.MAX_INFLUENCE
    r = policy.apply("weighted_blend", 90, False, _ai(score=0))
    assert r["final"] == 90 - policy.MAX_INFLUENCE
    assert 0 <= r["final"] <= 100


# 6) reranker/deep_only s'ndryshojnë final, japin order_key
def test_reranker_and_deep_only_keep_rule():
    r = policy.apply("ai_reranker", 62, False, _ai(score=80))
    assert r["final"] == 62 and r["order_key"] == (3, 80)  # 62//20 == 3
    r = policy.apply("ai_deep_only", 62, False, _ai(score=10))
    assert r["final"] == 62 and r["used_ai"] is False


# 6b) porta e plotësisë së profilit (e matur në 3D: AI shpik sfondin)
def test_profile_completeness_gate():
    ai = _ai(score=85)
    ai["profile_skills_n"] = 0   # profil pa aftësi — rasti real i 3D
    for pol in ("weighted_blend", "ai_high_confidence", "ai_reranker", "abstain_insufficient"):
        r = policy.apply(pol, 14, False, ai)
        assert r["abstained"] is True and r["final"] == 14 and r["used_ai"] is False
    # profil i plotë → porta kalon
    ai_ok = _ai(score=85)
    ai_ok["profile_skills_n"] = 5
    r = policy.apply("weighted_blend", 14, False, ai_ok)
    assert r["used_ai"] is True and r["abstained"] is False
    # pa fushë (kompatibitet 3B/3C) → porta nuk aktivizohet
    r = policy.apply("weighted_blend", 14, False, _ai(score=85))
    assert r["used_ai"] is True


# 7) analyze(): metrika strukturore mbi çifte të matura
def test_analyze_measures_all_policies():
    pairs = [
        {"rule": 80, "excluded": False, "ai": _ai(score=75)},
        {"rule": 45, "excluded": False, "ai": _ai(score=10, desc_len=2000)},
        {"rule": 20, "excluded": True, "ai": _ai(score=90)},
        {"rule": 10, "excluded": False, "ai": _ai(desc_len=100)},   # abstain
    ]
    rep = policy.analyze(pairs)
    assert set(rep) == set(policy.POLICIES)
    base = rep["deterministic_only"]
    assert base["changed"] == 0 and base["mean_abs_delta"] == 0
    blend = rep["weighted_blend"]
    # abstain = rasti me desc 100 + rasti i përjashtuar i fortë (AI i injoruar)
    assert blend["abstained"] == 2
    assert blend["changed"] >= 1
    # çdo politikë: final-e brenda [0,100]
    for p in rep.values():
        if p.get("n"):
            assert 0 <= p["min_final"] <= p["max_final"] <= 100


def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        policy.apply("vibe_based", 50, False, _ai())
