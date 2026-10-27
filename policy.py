"""Politikat e skorimit me AI (Faza 3D) — përcaktuese dhe të testueshme.

Asnjë politikë s'zgjidhet nga intuitysi: `analyze()` i vlerëson mbi të dhëna
reale të matura (run-e eval) dhe rekomandimi del nga metrikat.

Rregulla të pathyeshme (të gjitha politikat):
- Përjashtimi i fortë deterministic (excluded) NUK kapërcyhet kurrë —
  AI ignororesh plotësisht.
- UNKNOWN/mungesë dëshmie → abstention: final = rule_score (AI s'gjykaton).
- Çdo kombinim mban shpjegim: {final, used_ai, influence, abstained, reason}.
- Ndikimi maksimal i AI-së mbi final është i kufizuar (MAX_INFLUENCE).
"""
import statistics

MAX_INFLUENCE = 15          # pikë maksimale që AI mund ta zhvendosë final-in
BLEND_RULE_WEIGHT = 0.7     # peshë rule në blend (AI = 0.3)
CONF_THRESHOLD = 60         # ai_high_confidence: mbi këtë pranohet ndikimi AI
MIN_DESC_CHARS = 500        # dëshmi e pamjaftueshme nën këtë → abstain
MIN_PROFILE_SKILLS = 3      # profil pa mjaft aftësi → AI shpik sfondin (e matur 3D)

POLICIES = (
    "deterministic_only",     # bazë: final = rule_score
    "weighted_blend",         # 0.7×rule + 0.3×ai (me kap ±MAX_INFLUENCE)
    "ai_high_confidence",     # blend vetëm kur besueshmëria ≥ CONF_THRESHOLD
    "ai_reranker",            # final = rule (AI vetëm për renditje brenda bucket)
    "ai_deep_only",           # final = rule; AI vetëm si analizë në drawer
    "abstain_insufficient",   # blend por abstain pa dëshmi të mjaftueshme
)


def confidence(ai_score: int | None, desc_len: int, matched: int, missing: int) -> int | None:
    """Besueshmëria e AI-së (0–100) si proxy i matshëm i marrëveshjes + dëshmi.
    None = abstain (s'ka gjykatim të vlefshëm)."""
    if ai_score is None or not isinstance(ai_score, (int, float)):
        return None
    if desc_len < MIN_DESC_CHARS:
        return None  # dëshmi e pamjaftueshme — abstain
    if matched == 0 and missing == 0:
        return None  # asnjëlistë → s'ka dëshmi puntore
    # proxy: marrëveshja e përafërt me rule (e matshme pas run-it)
    return None if ai_score < 0 else 100


def _clamp_influence(rule: int, candidate: int) -> int:
    delta = max(-MAX_INFLUENCE, min(MAX_INFLUENCE, candidate - rule))
    return max(0, min(100, rule + delta))


def apply(policy: str, rule: int, excluded: bool,
          ai: dict | None = None) -> dict:
    """Apliko politikën. `ai` = {score, desc_len, matched_n, missing_n, ok}.
    Deterministik: e njëjta hyrje → i njëjti rezultat."""
    if policy not in POLICIES:
        raise ValueError(f"politikë e panjohur: {policy}")
    ai = ai or {}

    # rregull i pathyeshëm #1: përjashtimi i fortë fiton gjithmonë
    if excluded:
        return {"final": rule, "used_ai": False, "abstained": True,
                "reason": "përjashtim i fortë deterministic — AI e injoruar"}

    ai_score = ai.get("score")
    ok = bool(ai.get("ok")) and isinstance(ai_score, (int, float))
    desc_len = int(ai.get("desc_len") or 0)
    conf = confidence(ai_score if ok else None, desc_len,
                      int(ai.get("matched_n") or 0), int(ai.get("missing_n") or 0)) if ok else None

    # rregull i pathyeshëm #3 (e matur në 3D): profil i pamjaftueshëm → AI shpik
    # sfondin e kandidatit dhe inflaton pikët → abstain kur aftësitë < MIN
    if "profile_skills_n" in ai and ai["profile_skills_n"] < MIN_PROFILE_SKILLS:
        return {"final": rule, "used_ai": False, "abstained": True,
                "reason": (f"abstain — profil i pamjaftueshëm "
                           f"({ai['profile_skills_n']} aftësi < {MIN_PROFILE_SKILLS}): "
                           "rrezik hallucinimi i sfondit")}

    # rregull i pathyeshëm #2: abstention — AI jo-valide ose dëshmi e pamjaftueshme
    if not ok or conf is None:
        return {"final": rule, "used_ai": False, "abstained": True,
                "reason": "abstain — dëshmi e pamjaftueshme ose AI i padisponueshëm"}

    blend = _clamp_influence(rule, round(BLEND_RULE_WEIGHT * rule + (1 - BLEND_RULE_WEIGHT) * ai_score))

    if policy == "deterministic_only":
        return {"final": rule, "used_ai": False, "abstained": False,
                "reason": "bazë deterministike"}
    if policy == "weighted_blend":
        return {"final": blend, "used_ai": True, "abstained": False,
                "reason": f"blend 70/30 me kap ±{MAX_INFLUENCE}"}
    if policy == "ai_high_confidence":
        # besueshmëria = marrëveshja me rule (e matshme): sa më afër, aq më besueshëm
        agreement = max(0, 100 - abs(ai_score - rule))
        if agreement >= CONF_THRESHOLD:
            return {"final": blend, "used_ai": True, "abstained": False,
                    "reason": f"marrëveshje {agreement} ≥ {CONF_THRESHOLD}"}
        return {"final": rule, "used_ai": False, "abstained": False,
                "reason": f"marrëveshje {agreement} < {CONF_THRESHOLD} → rule"}
    if policy == "ai_reranker":
        return {"final": rule, "order_key": (rule // 20, ai_score),
                "used_ai": False, "abstained": False,
                "reason": "renditje brenda bucket-i rule me AI"}
    if policy == "ai_deep_only":
        return {"final": rule, "used_ai": False, "abstained": False,
                "reason": "AI vetëm si analizë në drawer"}
    if policy == "abstain_insufficient":
        if desc_len < 1000:  # kërkesë më striktë e dëshmisë
            return {"final": rule, "used_ai": False, "abstained": True,
                    "reason": f"abstain — përshkrim < 1000 karaktere"}
        return {"final": blend, "used_ai": True, "abstained": False,
                "reason": "dëshmi e mjaftueshme → blend"}
    raise AssertionError(policy)


def analyze(pairs: list[dict]) -> dict:
    """Vlerëson të gjitha politikat mbi çifte të matura (rule, ai) — deterministik.

    pairs: [{rule, excluded, ai:{ok, score, desc_len, matched_n, missing_n}}]
    Kthen metrika për politikë: sa final-e ndryshuan, |Δ| mesatar/maks, abstentions.
    """
    report = {}
    for policy in POLICIES:
        finals, used = [], 0
        abstained = 0
        deltas = []
        for p in pairs:
            try:
                r = apply(policy, int(p["rule"]), bool(p["excluded"]), p.get("ai"))
            except (KeyError, TypeError, ValueError):
                continue
            finals.append(r["final"])
            if r["used_ai"]:
                used += 1
            if r["abstained"]:
                abstained += 1
            deltas.append(abs(r["final"] - int(p["rule"])))
        if not finals:
            report[policy] = {"n": 0}
            continue
        changed = sum(1 for d in deltas if d > 0)
        report[policy] = {
            "n": len(finals),
            "changed": changed,
            "changed_pct": round(changed / len(finals) * 100, 1),
            "used_ai": used,
            "abstained": abstained,
            "mean_abs_delta": round(statistics.mean(deltas), 1),
            "max_abs_delta": max(deltas),
            "min_final": min(finals),
            "max_final": max(finals),
        }
    return report
