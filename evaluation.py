"""Kontroll i vlerësimit me modele reale (spec §15.6) — Faza 3C.

PARIME:
- Mostër deterministe (seed) 20–30 mundësi, e ruajtur në run → rerun i riprodhueshëm.
- Limite të forta: max_calls + max_seconds; kalimi ndërpret run-in (status 'aborted').
- Guard për furnizues me pagesë: cloud kërkon celes + opt-in të shprehur
  (settings: eval_paid_opt_in = 'yes'). Lokali (127.0.0.1/localhost) s'kërkon.
- Fail-soft: gabimi i një elementi s'ndërpret run-in; AI kurrë s'prek
  rule_score / ai_score / final_score — vlerësimi shkruan vetëm tabelat eval_*
  dhe ai_cache (provuar në teste).
- Nuk ka thirrje automatike: run niset vetëm me POST të qëllimshëm.
"""
import random
import time

import ai_service
import repository as repo

PAID_OPTIN_SETTING = "eval_paid_opt_in"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


# ------------------------------------------------------- mostëra

def select_sample(n: int = 25, seed: int = 42, kinds: list[str] | None = None) -> list[int]:
    """Mostër e deterministike e përzier sipas nivelit të rule_score.

    Struktura: 3 shtresa (lart ≥70, mes 30–69, ulët <30) me rrathë të alternuar
    me RNG të mbjellë — e njëjta DB + i njëjti seed → e njëjta mostër.
    """
    n = max(1, min(int(n), 30))
    import context as _ctx
    actor = _ctx.require_context()
    uw, uargs = ("1=1", []) if actor.is_system else \
        ("user_id = ? AND tenant_id = ?", [actor.user_id, actor.tenant_id])
    with repo.db() as con:
        rows = con.execute(
            f"""SELECT id, kind, rule_score FROM opportunities
               WHERE excluded = 0 AND {uw} ORDER BY id""", uargs
        ).fetchall()
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    high = [r["id"] for r in rows if (r["rule_score"] or 0) >= 70]
    mid = [r["id"] for r in rows if 30 <= (r["rule_score"] or 0) < 70]
    low = [r["id"] for r in rows if (r["rule_score"] or 0) < 30]
    strata = [high, mid, low]
    rng = random.Random(seed)
    for s in strata:
        rng.shuffle(s)
    picked: list[int] = []
    i = 0
    while len(picked) < n and any(strata):
        s = strata[i % 3]
        if s:
            picked.append(s.pop())
        i += 1
        if i > 3 * n + 6:
            break
    return picked


# ------------------------------------------------- guard i kostos

def _is_local(base_url: str) -> bool:
    for h in LOCAL_HOSTS:
        if base_url.startswith(f"http://{h}") or base_url.startswith(f"https://{h}"):
            return True
    return False


def guard(provider: str, base_url: str, api_key: str) -> str:
    """Kthen '' kur lejohet, përndryshe arsyen e bllokimit."""
    if provider == "disabled":
        return "AI është çaktivizuar (provider=disabled)"
    if provider == "openai-compatible" and not _is_local(base_url or ""):
        if not api_key:
            return "furnizues cloud pa celes (AI_API_KEY)"
        if repo.get_setting(PAID_OPTIN_SETTING) != "yes":
            return (f"furnizues cloud me pagesë: kërkon opt-in të shprehur "
                    f"(settings: {PAID_OPTIN_SETTING} = 'yes')")
    return ""


# ----------------------------------------------------- ekzekutimi

def start_run(task: str, provider: str = "", model: str = "", sample_size: int = 25,
              seed: int = 42, max_calls: int = 25, max_seconds: int = 300,
              label: str = "", force: bool = False) -> dict:
    """Nis një run kontrollimi. Kthen dict me run_id ose blocked+reason.
    Kurrë s'hedh exception; kurrë s'prek opportunities."""
    if task not in ai_service.SCHEMAS:
        return {"blocked": True, "reason": f"detyrë e panjohur: {task}"}
    cfg0 = ai_service.get_config()
    provider = provider or cfg0["provider"]
    model = model or cfg0["model"] or ai_service.DEFAULTS["model"].get(provider, "")
    base_url = cfg0["base_url"] or ai_service.DEFAULTS["base_url"].get(provider, "")
    api_key = cfg0["api_key"]

    reason = guard(provider, base_url, api_key)
    if reason:
        return {"blocked": True, "reason": reason}
    if not model:
        return {"blocked": True, "reason": "model i pazgjedhur (settings: ai_model)"}

    ids = select_sample_stratified(sample_size, seed)
    if not ids:
        return {"blocked": True, "reason": "nuk ka mundësi për mostër"}
    max_calls = max(1, min(int(max_calls), len(ids)))
    max_seconds = max(5, int(max_seconds))

    run_id = repo.create_eval_run(label, task, provider, model, base_url, seed,
                                  len(ids), ids, max_calls, max_seconds)
    cfg = {**cfg0, "provider": provider, "model": model, "base_url": base_url}

    profile = repo.get_profile()
    calls = ok = failed = cached = 0
    total_latency = total_tokens = 0
    status = "complete"
    started = time.monotonic()

    for seq, oid in enumerate(ids):
        if calls >= max_calls:
            status = "aborted"
            break
        if time.monotonic() - started > max_seconds:
            status = "aborted"
            break
        j = repo.get_opportunity(oid)
        if not j:
            repo.add_eval_item(run_id, oid, seq, "error", error="nuk u gjet")
            failed += 1
            calls += 1
            continue
        started_item = time.monotonic()
        out = ai_service.run_task(task, j, profile=profile, force=force, cfg=cfg)
        calls += 1
        latency = int((time.monotonic() - started_item) * 1000)
        total_latency += latency
        tokens = 0
        if out.meta.get("usage"):
            tokens = int((out.meta["usage"] or {}).get("total_tokens") or 0)
        total_tokens += tokens
        if out.cached:
            cached += 1
        if out.ok:
            ok += 1
            repo.add_eval_item(run_id, oid, seq, "ok", cached=int(out.cached),
                               latency_ms=out.meta.get("latency_ms", latency),
                               usage_tokens=tokens, output=out.data, meta=out.meta)
        else:
            failed += 1
            repo.add_eval_item(run_id, oid, seq, "error", error=out.error,
                               latency_ms=latency, usage_tokens=tokens)

    repo.finish_eval_run(
        run_id, status=status, calls=calls, ok_count=ok, failed_count=failed,
        cached_count=cached, total_latency_ms=total_latency,
        total_tokens=total_tokens,
    )
    return {"blocked": False, "run_id": run_id, "status": status, "calls": calls,
            "ok": ok, "failed": failed, "cached": cached}


# -------------------------------------------------- raporti i krahasimit

def _run_metrics(run: dict) -> dict:
    items = run.get("items") or []
    ok_items = [i for i in items if i["status"] == "ok"]
    scores = [i["output"].get("score", i["output"].get("fit_score"))
              for i in ok_items if isinstance(i.get("output"), dict)]
    scores = [s for s in scores if isinstance(s, (int, float))]
    rules = []
    diffs = []
    if scores:
        with repo.db() as con:
            for i in ok_items:
                row = con.execute("SELECT rule_score FROM opportunities WHERE id=?",
                                  (i["opportunity_id"],)).fetchone()
                if row:
                    rules.append(row["rule_score"] or 0)
        out_scores = [i["output"].get("score", i["output"].get("fit_score"))
                      for i in ok_items if isinstance(i.get("output"), dict)
                      and isinstance(i["output"].get("score", i["output"].get("fit_score")), (int, float))]
        diffs = [abs(a - b) for a, b in zip(out_scores, rules)]
    return {
        "run_id": run["id"], "label": run["label"], "task": run["task"],
        "provider": run["provider"], "model": run["model"], "status": run["status"],
        "calls": run["calls"], "ok": run["ok_count"], "failed": run["failed_count"],
        "cached": run["cached_count"],
        "ok_rate": round(run["ok_count"] / run["calls"], 3) if run["calls"] else None,
        "avg_latency_ms": round(run["total_latency_ms"] / run["calls"]) if run["calls"] else None,
        "total_tokens": run["total_tokens"],
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "mean_abs_diff_vs_rule": round(sum(diffs) / len(diffs), 1) if diffs else None,
        "reviewed": sum(1 for i in items if i.get("human_rating") is not None),
    }


def compare_runs(run_ids: list[int]) -> dict:
    """Krahasim shumë-runesh: ok-rate, latencë, tokens, scorçe, reviews."""
    runs = []
    for rid in run_ids:
        run = repo.get_eval_run(rid, with_items=True)
        if run:
            runs.append(_run_metrics(run))
    winner = None
    completed = [m for m in runs if m["status"] == "complete" and m["ok_rate"] is not None]
    if len(completed) >= 2:
        # renditje e thjeshtë: ok-rate desc, pastaj latencë asc
        completed.sort(key=lambda m: (-m["ok_rate"], m["avg_latency_ms"] or 0))
        winner = completed[0]["run_id"]
    return {"runs": runs, "winner": winner}


# ------------------------------------------------------- rubrika

RUBRIC = {
    "scale": "1–5",
    "anchors": {
        1: "e pavlefshme — JSON s'i përgjigjet përmbajtjes / hallucinim i qartë",
        2: "e dobët — kryesisht e pasaktë, kërkon rishkrim të plotë",
        3: "e pranueshme — pjesërisht e saktë, kërkor korrigjime",
        4: "e mirë — e saktë dhe e dobishme me gabime të vogla",
        5: "e shkëlqyer — e saktë, e plotë, e gatshme për përdorim",
    },
    "dimensions": ["saktësi faktike", "përputhje me përmbajtjen", "dobishëm për vendimmarrje"],
}

# rubrika e zgjeruar 3D
RUBRIC_3D = {
    **RUBRIC,
    "fields": {
        "extraction_accuracy": "1–5 saktësia e nxjerrjes së kërkesave",
        "match_judgment": "1–5 gjykimin e përputhjes (score + matched/missing)",
        "explanation_usefulness": "1–5 dobishëmia e shpjegimit për vendim",
        "hallucination": "true/false — pretendime pa mbështetje në tekst",
        "important_missed": "true/false — kërkesë thelbësore e humbur",
    },
}


# ---------------------------------- mostër e shtresuar shumë-dimensionale

def select_sample_stratified(n: int = 25, seed: int = 3407) -> list[int]:
    """Mostër deterministe sipas: niveleve të rule_score (3) × remote/onsite (2),
    me përhapje burimesh dhe alternim përshkrimesh të pasura/të shkurtra."""
    n = max(1, min(int(n), 30))
    import context as _ctx
    actor = _ctx.require_context()
    uw, uargs = ("1=1", []) if actor.is_system else \
        ("user_id = ? AND tenant_id = ?", [actor.user_id, actor.tenant_id])
    with repo.db() as con:
        rows = con.execute(
            f"""SELECT id, kind, rule_score, remote_type, source,
                      LENGTH(description) AS dlen
               FROM opportunities WHERE excluded = 0 AND {uw}""", uargs
        ).fetchall()
    rng = random.Random(seed)

    def tier(score):
        s = score or 0
        return "high" if s >= 70 else ("mid" if s >= 30 else "low")

    cells: dict[tuple, list] = {}
    for r in rows:
        mode = "remote" if r["remote_type"] == "remote" else "onsite"
        cells.setdefault((tier(r["rule_score"]), mode), []).append(dict(r))

    # brenda qelizës: grupim sipas burimit → alternim i pasur/shkurtër
    for key, lst in cells.items():
        by_src: dict[str, list] = {}
        for r in lst:
            by_src.setdefault(r["source"] or "?", []).append(r)
        for src in by_src:
            by_src[src].sort(key=lambda r: (r["dlen"] or 0, r["id"]))
        inter: list = []
        srcs = sorted(by_src)
        take_rich = True
        while any(by_src[s] for s in srcs):
            for s in srcs:
                if not by_src[s]:
                    continue
                if take_rich:
                    inter.append(by_src[s].pop())      # më i pasuri
                else:
                    inter.append(by_src[s].pop(0))     # më i shkurtri
            take_rich = not take_rich
        cells[key] = inter

    keys = sorted(cells)
    for k in keys:
        rng.shuffle(cells[k])  # rnd i mbjellë brenda qelizës
    picked, i = [], 0
    while len(picked) < n and any(cells[k] for k in keys):
        k = keys[i % len(keys)]
        if cells[k]:
            picked.append(cells[k].pop()["id"])
        i += 1
        if i > len(keys) * n + 8:
            break
    return picked


def sample_composition(ids: list[int]) -> dict:
    """Përmbajtja e mostërs — raportim (shtresa × mode × burim × pasuri përshkrimi)."""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with repo.db() as con:
        rows = con.execute(
            f"""SELECT id, rule_score, remote_type, source, LENGTH(description) AS dlen
                FROM opportunities WHERE id IN ({ph})""",
            ids,
        ).fetchall()
    comp = {"total": len(ids), "tier": {}, "mode": {}, "source": {}, "desc": {}}
    for r in rows:
        t = "high" if (r["rule_score"] or 0) >= 70 else ("mid" if (r["rule_score"] or 0) >= 30 else "low")
        comp["tier"][t] = comp["tier"].get(t, 0) + 1
        m = r["remote_type"] == "remote" and "remote" or "onsite"
        comp["mode"][m] = comp["mode"].get(m, 0) + 1
        comp["source"][r["source"]] = comp["source"].get(r["source"], 0) + 1
        d = "rich(≥1200)" if (r["dlen"] or 0) >= 1200 else ("medium(300–1199)" if (r["dlen"] or 0) >= 300 else "short(<300)")
        comp["desc"][d] = comp["desc"].get(d, 0) + 1
    return comp


# --------------------------------------------------- statistika latence

def latency_stats(values_ms: list[int]) -> dict:
    if not values_ms:
        return {"n": 0}
    vs = sorted(values_ms)

    def pct(p):
        k = (len(vs) - 1) * p / 100
        f, c = int(k), min(int(k) + 1, len(vs) - 1)
        return round(vs[f] + (vs[c] - vs[f]) * (k - f))
    return {
        "n": len(vs), "min": vs[0], "median": pct(50), "p95": pct(95),
        "max": vs[-1], "mean": round(sum(vs) / len(vs)),
    }
