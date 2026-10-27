"""OE-007F — politika e freskisë së sigurt për kërkime të planifikuara.

Qëllimi: feed-i i pronarit mbetet i freskët automatikisht me konsum TË
KUFIZUAR dhe pa konsum pagese të befasishëm.

Parimet:
- Klasat e burimeve jetojnë te connectors.EXECUTION_CLASSES (metadata
  qendrore). MANUAL_ONLY nuk ekzekutohet kurrë pa njeri.
- Porta e pagesave (METERED_PAID): i jashtëm nga çdo cikël i planifikuar
  përveç kur plotësohen TË GJITHA: `scheduling_include_paid=1` (settings,
  opt-in global i shprehur) + usage enforcement aktiv + burimi ndodhet
  NË LISTËN E BURIMEVE të profilit (opt-in per-profil). Kurrë fallback i
  heshtur nga burim falas te një burim me pagesë.
- Buxheti i ciklit: max burime/cikël, plafon ditor per-burim (search_runs),
  est kërkesash per cikël, kufi orar i planifikuar (burime × timeout
  konektori; koha reale regjistrohet). Kur plagon → ndalo, regjistro
  arsyen, mbaj rezultatet e pjesshme — feed-i s'shënohet i sëmurë.
- Backoff i thjeshtë: 3 dështime radhazi të një burimi → anashkalo këtë
  cikël me arsye të klasifikuar (429 / 5xx / kredenciale / timeout).
- Digest-first: cikli i planifikuar NUK dërgon email immediate; përmbledhja
  ditore i përmbledh (notifier, mënyra digest).
- Përmbledhja e ciklit persistohet te settings (pa migrim skeme).
"""
import json
from datetime import datetime, timezone

import connectors
import repository as repo

# --------------------------------------------------------- politika e klasave

# Intervali minimal i ripollimit sipas klasës (orë). Cikli mund të tentojë
# më shpesh (kadenca e profilit), por burimi ripoll vetëm pas kësaj distance
# nga SUKSESI i fundit.
CLASS_MIN_INTERVAL_HOURS = {
    "FREE_SAFE": 6,
    "CONFIGURED_FREE_OR_QUOTA": 6,
    "METERED_PAID": 24,     # vetëm me opt-in të plotë (më poshtë)
    "MANUAL_ONLY": None,    # kurrë polling i planifikuar
}

CYCLE_MAX_SOURCES = 8          # max burime të ekzekutuara për cikël
SOURCE_DAILY_RUN_CAP = 8       # max ekzekutime per burim per PROFIL per ditë UTC
CYCLE_MAX_SECONDS = 300        # kufi orar i planifikuar (burime × timeout ≤ 25s)

PAID_INCLUDE_SETTING = "scheduling_include_paid"

DIGEST_STRONG_MIN = 70         # niveli "përputhje e fortë" (OE-007B/C)
DIGEST_REVIEW_MIN = 60         # "ia vlen të shikohen" — kurrë etiketë "high"


def execution_class(source_id: str) -> str:
    return connectors.execution_class(source_id)


def paid_opt_in_enabled() -> bool:
    """Opt-in i shprehur për pagesa DHE usage enforcement aktiv — të dyja."""
    value = repo.get_setting(PAID_INCLUDE_SETTING, "")
    if value != "1":
        return False
    import usage_ledger
    return usage_ledger.enforcement_state() == "active"


# ------------------------------------------------------------- ndihmës DB

def _source_recent_runs(source_id: str, uid: int, limit: int = 3,
                        profile_id: int | None = None) -> list[dict]:
    """Historiku i ekzekutimeve të planifikuara për këtë burim — me scope
    profilit (disiplina e polling-ut është per-profile; hermetike ndaj
    kërkimeve manuale të tjera)."""
    with repo.db() as con:
        if profile_id is None:
            rows = con.execute(
                """SELECT status, error, started_at FROM search_runs
                   WHERE source=? AND user_id=? ORDER BY id DESC LIMIT ?""",
                (source_id, uid, limit)).fetchall()
        else:
            rows = con.execute(
                """SELECT status, error, started_at FROM search_runs
                   WHERE source=? AND user_id=? AND profile_id=?
                   ORDER BY id DESC LIMIT ?""",
                (source_id, uid, profile_id, limit)).fetchall()
    return [dict(r) for r in rows]


def _source_runs_today(source_id: str, uid: int, now_utc: datetime,
                       profile_id: int | None = None) -> int:
    """Buxhet ditor per burim per PROFIL per ditë UTC (një pronar aktiv →
    e njëjta mbrojtje; scope per-profil e mban politikën hermetike ndaj
    kërkimeve manuale)."""
    day_start = now_utc.strftime("%Y-%m-%d 00:00")
    with repo.db() as con:
        if profile_id is None:
            return con.execute(
                "SELECT COUNT(*) FROM search_runs WHERE source=? AND user_id=? AND started_at >= ?",
                (source_id, uid, day_start)).fetchone()[0]
        return con.execute(
            "SELECT COUNT(*) FROM search_runs WHERE source=? AND user_id=? AND profile_id=? AND started_at >= ?",
            (source_id, uid, profile_id, day_start)).fetchone()[0]


def _backoff_reason(runs: list[dict]) -> str:
    """Arsyeja e backoff-it kur të 3 ekzekutimet e fundit dështuan."""
    last_err = (runs[0].get("error") or "") if runs else ""
    if "429" in last_err:
        return "429 — burimi kërkon pritje; provohet në ciklin e ardhshëm"
    if last_err.startswith("HTTP 5") or "HTTP 5" in last_err:
        return "5xx — ftohje e përkohshme e burimit"
    low = last_err.lower()
    if "401" in last_err or "403" in last_err or "çelës" in low or "celes" in low \
            or "konfigurim" in low:
        return "kredenciale — korrigjo konfigurimin e burimit"
    return "dështime të përsëritura — provohet në ciklin e ardhshëm"


def _last_success_age_hours(source_id: str, uid: int, now_utc: datetime,
                            profile_id: int | None = None) -> float | None:
    runs = _source_recent_runs(source_id, uid, limit=25, profile_id=profile_id)
    for r in runs:
        if r.get("status") == "ok":
            started = r.get("started_at")
            try:
                dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (now_utc - dt).total_seconds() / 3600.0)
            except (ValueError, TypeError):
                return None
    return None  # kurrë sukses → poll i parë i lejuar


# --------------------------------------------------------------- planifikim

def plan_sources(profile: dict, now_utc: datetime | None = None) -> dict:
    """Plan DRY-RUN i ciklit për profilin — lexon vetëm gjendje, s'ekzekuton.
    Kthen {'planned': [{source, class, est_requests}], 'skipped': [{source, reason}],
    'excluded_paid': [...], 'budget': {...}}."""
    now_utc = now_utc or datetime.now(timezone.utc)
    uid = profile["user_id"]
    raw_sources = profile.get("sources") or []
    if isinstance(raw_sources, str):
        try:
            requested = json.loads(raw_sources)
        except (ValueError, TypeError):
            requested = []
    else:
        requested = list(raw_sources)
    paid_ok = paid_opt_in_enabled()

    planned, skipped, excluded_paid = [], [], []
    est_requests = 0
    for sid in requested:
        if len(planned) >= CYCLE_MAX_SOURCES:
            skipped.append({"source": sid, "reason": f"plafon cikli ({CYCLE_MAX_SOURCES} burime)"})
            continue
        klass = execution_class(sid)
        if sid not in connectors.REGISTRY:
            skipped.append({"source": sid, "reason": "burim i panjohur"})
            continue
        if klass == "MANUAL_ONLY":
            skipped.append({"source": sid, "reason": "klasë MANUAL_ONLY — vetëm manualisht"})
            continue
        if klass == "METERED_PAID" and not paid_ok:
            excluded_paid.append(sid)
            skipped.append({"source": sid,
                            "reason": "kërkim me pagesë — jashtë politikës (pa opt-in)"})
            continue
        connector = connectors.REGISTRY[sid]
        if not connector.is_configured():
            skipped.append({"source": sid, "reason": "pa konfigurim (çelësa/board)"})
            continue
        runs_today = _source_runs_today(sid, uid, now_utc, profile_id=profile["id"])
        if runs_today >= SOURCE_DAILY_RUN_CAP:
            skipped.append({"source": sid,
                            "reason": f"plafon ditor i arritur ({runs_today}/{SOURCE_DAILY_RUN_CAP})"})
            continue
        min_hours = CLASS_MIN_INTERVAL_HOURS.get(klass)
        age = _last_success_age_hours(sid, uid, now_utc, profile_id=profile["id"])
        if min_hours is not None and age is not None and age < min_hours:
            skipped.append({"source": sid,
                            "reason": f"i freskët ({age:.1f}h < {min_hours}h për {klass})"})
            continue
        runs = _source_recent_runs(sid, uid, limit=3, profile_id=profile["id"])
        if len(runs) >= 3 and all(r.get("status") != "ok" for r in runs):
            skipped.append({"source": sid, "reason": _backoff_reason(runs)})
            continue
        # Estimim kërkesash: konektorët API bëjnë 1–2 kërkesa; board-et e
        # user-it deri 3. Estim konservativ për buxhetin e ciklit.
        est = 3 if sid in ("greenhouse", "lever", "ashby") else 2
        if est_requests + est > CYCLE_MAX_SOURCES * 2:
            skipped.append({"source": sid, "reason": "buxhet kërkesash i ciklit"})
            continue
        est_requests += est
        planned.append({"source": sid, "class": klass, "est_requests": est})
    return {"planned": planned, "skipped": skipped, "excluded_paid": excluded_paid,
            "budget": {"max_sources": CYCLE_MAX_SOURCES,
                       "source_daily_cap": SOURCE_DAILY_RUN_CAP,
                       "max_seconds": CYCLE_MAX_SECONDS,
                       "paid_opt_in": paid_ok,
                       "usage_enforcement": _enforcement_state_safe()}}


def _enforcement_state_safe() -> str:
    try:
        import usage_ledger
        return usage_ledger.enforcement_state()
    except Exception:
        return "disabled"


# --------------------------------------------------------------- cikli real

def execute_cycle(profile: dict, sched: dict, now_utc: datetime | None = None) -> dict:
    """Ekzekuton ciklin e freskisë për profilin (PAS CAS-claim të schedulerit).
    Kthen përmbledhje të plotë; burimet e planifikuara ekzekutohen përmes
    ORKESTRATORIT (rruga e vetme e kërkimit). Digest-first: pa email immediate."""
    import orchestrator
    import time as _time
    started = _time.perf_counter()
    now_utc = now_utc or datetime.now(timezone.utc)
    run_started = repo.now()

    plan = plan_sources(profile, now_utc)
    planned_sources = [p["source"] for p in plan["planned"]]
    discovered = new = dup = 0
    new_ids: list[int] = []
    failures: list[dict] = []
    executed = 0

    if planned_sources:
        summary = orchestrator.run(profile_id=profile["id"], sources=planned_sources,
                                   label="scheduled")
        executed = summary.get("sources_attempted", 0)
        discovered = summary.get("checked", 0)
        new = summary.get("new", 0)
        dup = summary.get("dup", 0)
        for r in summary.get("results", []):
            new_ids.extend(r.get("new_ids") or [])
            if not r.get("ok"):
                failures.append({"source": r["source"], "error": (r.get("error") or "")[:120]})

    notified = 0
    if new_ids:
        from scheduler import notify_new_opportunities
        notified = notify_new_opportunities(profile, sched, new_ids, run_started)
    # Digest-first: ASNJË deliver_immediate këtu — përmbledhja ditore i mbledh.

    elapsed = _time.perf_counter() - started
    cycle = {
        "profile_id": profile["id"], "profile": profile["name"],
        "at": repo.now(),
        "sources_planned": len(plan["planned"]),
        "sources_executed": executed,
        "sources_skipped": [{"source": s["source"], "reason": s["reason"]}
                            for s in plan["skipped"]],
        "excluded_paid": plan["excluded_paid"],
        "requests_consumed": sum(p["est_requests"] for p in plan["planned"][:executed]) or executed,
        "discovered": discovered, "new": new, "duplicates": dup,
        "notified": notified,
        "high_matches": sum(1 for oid in new_ids
                            if (repo.get_opportunity(oid) or {}).get("rule_score", 0) >= DIGEST_STRONG_MIN),
        "worth_reviewing": sum(1 for oid in new_ids
                               if DIGEST_REVIEW_MIN <= (repo.get_opportunity(oid) or {}).get("rule_score", 0) < DIGEST_STRONG_MIN),
        "failures": failures,
        "elapsed_seconds": round(elapsed, 2),
        "budget": plan["budget"],
    }
    try:
        repo.set_setting(f"scheduling_last_cycle:{profile['id']}",
                         json.dumps(cycle, ensure_ascii=False))
    except Exception:
        pass  # përmbledhja s'thyej ciklin
    return cycle
