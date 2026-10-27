"""Schedulerr-i i profileve të ruajtura (Faza 7A) — deterministic, fail-soft.

Kontrata:
- `compute_next(cadence, tz_name, run_time, from_utc, now=None) -> ISO UTC|string`
  kadencat: hourly, every_3h, every_6h, daily, weekdays_daily, manual.
- `claim(...)` CAS në DB: avancimi i next_run_at është leja — dy worker/restart
  s'mund të ekzekutojnë të njëjtën occasion dy herë.
- `run_due(now_utc=None)` gjen profilet e afërta, i ekzekuton përmes
  ORCHESTRATORIT (e vetmja rrugë e kërkimit), regjistron run-et, krijon
  njoftime vetëm për mundësi VËRTET të reja që kalojnë pragun, përditëson
  next_run_at. Nuk hedh exception; dështimi i një profili s'ndalon të tjerët.

Ora e testit: çdo funksion pranon `now` (datetime UTC) — injektim deterministic.
"""
from datetime import datetime, timedelta, timezone

import orchestrator
import repository as repo

CADENCES = ("hourly", "every_3h", "every_6h", "daily", "weekdays_daily", "manual")
INTERVALS = {"hourly": timedelta(hours=1), "every_3h": timedelta(hours=3),
             "every_6h": timedelta(hours=6)}

NOTIF_TYPES = {
    "job": "new_high_match",
    "freelance": "new_freelance_opportunity",
    "product_lead": "new_product_lead",
    "local_job": "new_local_opportunity",
}


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse(s: str):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def compute_next(cadence: str, tz_name: str, run_time: str,
                 from_utc: datetime, now_utc: datetime | None = None) -> str | None:
    """next_run_at (ISO UTC) pas `from_utc`. manual → None."""
    from zoneinfo import ZoneInfo
    if cadence == "manual":
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    if cadence in INTERVALS:
        return (from_utc + INTERVALS[cadence]).strftime("%Y-%m-%d %H:%M")
    # daily / weekdays_daily: hera e ardhshme e orarit të caktuar në zonë
    try:
        tz = ZoneInfo(tz_name or "Europe/Berlin")
    except Exception:
        tz = ZoneInfo("Europe/Berlin")
    hh, mm = (run_time or "08:00").split(":")[:2]
    local_now = from_utc.astimezone(tz)
    cand = local_now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if cand <= local_now:
        cand += timedelta(days=1)
    while cadence == "weekdays_daily" and cand.weekday() >= 5:
        cand += timedelta(days=1)
    return _utc(cand).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------ njoftimet

def _notification_type(kind: str, score: int, min_match: int) -> str:
    if kind == "local_job":
        return "new_local_opportunity"
    if kind == "freelance":
        return "new_freelance_opportunity"
    if kind == "product_lead":
        return "new_product_lead"
    if min_match and score >= min_match:
        return "new_high_match"
    return "new_opportunity"


def notify_new_opportunities(profile: dict, schedule: dict, new_ids: list[int],
                             run_started_at: str) -> int:
    """Krijon njoftime për mundësitë e reja që kalojnë filtrat. Kthen sa u krijuan."""
    min_match = int(schedule.get("min_match") or 0)
    kinds = schedule.get("kinds") or []
    created = 0
    for oid in new_ids:
        op = repo.get_opportunity(oid)
        if not op or op.get("excluded"):
            continue  # përjashtime të forta — kurrë njoftim
        if min_match and (op.get("rule_score") or 0) < min_match:
            continue
        if kinds and op.get("kind") not in kinds:
            lenses = set(op.get("lenses") or [])
            if not (lenses & set(kinds)) and op.get("kind") not in kinds:
                continue
        ntype = _notification_type(op.get("kind") or "job",
                                   op.get("rule_score") or 0, min_match)
        title = f"{op.get('title') or 'Mundësi'}"
        summary_bits = [op.get("org_name") or op.get("source")]
        if op.get("location"):
            summary_bits.append(op["location"])
        if op.get("salary_display"):
            summary_bits.append(op["salary_display"])
        dedup = f"sched:{profile['id']}:{oid}"
        nid = repo.create_notification(
            profile_id=profile["id"], opportunity_id=oid, ntype=ntype,
            title=title, summary=" · ".join(str(b) for b in summary_bits if b),
            dedup_key=dedup,
            metadata={"rule_score": op.get("rule_score"), "kind": op.get("kind"),
                      "run_started_at": run_started_at, "source": op.get("source")},
        )
        if nid:
            created += 1
    return created


# --------------------------------------------------- ekzekutimi i due

def run_scheduled_profile(profile_id: int, now_utc: datetime | None = None) -> dict:
    """Ekzekuto NJË profil të scheduluar përmes orkestratorit — fail-soft.
    CAS: avanon next_run_at para ekzekutimit (levë kundër dyfishtës).
    IZOLIMI (8C2): gjithë ekzekutimi ndodh nën scheduler_context të PRONARIT
    të profilit — kërkimet, njoftimet dhe email-et prekin VETËM të dhënat e
    atij përdoruesi, kurrë të një user-i tjetër."""
    import context
    now_utc = now_utc or datetime.now(timezone.utc)
    sched = repo.get_schedule(profile_id, context.migration_context())  # gjeje pronarin
    if not sched:
        return {"skipped": True, "reason": "pa konfigurim/profil"}
    profile = repo.get_search_profile(profile_id, context.migration_context())
    if not profile:
        return {"skipped": True, "reason": "pa profil"}
    if not sched.get("enabled"):
        return {"skipped": True, "reason": "çaktivizuar"}
    # tenant-i VJEN nga anëtarësia e verifikuar e pronarit (kurrë nga inputi)
    import tenancy
    try:
        member = tenancy.resolve_actor(profile["user_id"])
    except tenancy.TenantMembershipError:
        return {"skipped": True, "reason": f"pa anëtarësi aktive (user {profile['user_id']})"}
    with context.run_as(context.scheduler_context(member.user_id, member.tenant_id)):
        return _run_scheduled_for_owner(profile, sched, now_utc)


def _run_scheduled_for_owner(profile: dict, sched: dict, now_utc: datetime) -> dict:
    profile_id = profile["id"]
    expected = sched.get("next_run_at")
    cadence = sched.get("cadence", "daily")
    # baza = max(i vjetër, TANI): një sched i prapambetur ekzekutohet një herë
    # dhe i ardhshmja planifikohet nga tani (jo në të kaluarën → pa due dyfish)
    base = max(filter(None, [_parse(expected), now_utc]),
               default=now_utc)
    new_next = compute_next(cadence, sched.get("timezone", "Europe/Berlin"),
                            sched.get("run_window_time", "08:00"), base, now_utc)
    if not repo.claim_schedule(profile_id, expected, new_next,
                               lease_minutes=30):
        return {"skipped": True, "reason": "leja/next_run u mor nga një tjetër worker"}

    # OE-007F: politika e freskisë — plan me klasa burimesh, buxhet, backoff,
    # përjashtim pagesash dhe përmbledhje cikli. Digest-first: kurrë email
    # immediate nga cikli i planifikuar.
    import scheduling_policy
    cycle = scheduling_policy.execute_cycle(profile, sched, now_utc)

    # sukses: të paktën një burim ekzekutuar me sukses (dështimi i njërit
    # mbetet i izoluar), ose cikël pa asgjë për ekzekutim (gjithçka e freskët)
    success = (cycle.get("sources_executed", 0) > len(cycle.get("failures", []))) \
        or cycle.get("sources_planned", 0) == 0

    repo.finish_schedule_run(profile_id, success=success,
                             last_run_at=cycle.get("at"),
                             last_success_at=cycle.get("at") if success else None)
    failures = 0 if success else (sched.get("consecutive_failures") or 0) + 1
    return {"skipped": False, "ok": success, "profile_id": profile_id,
            "profile": profile["name"], "new": cycle.get("new", 0),
            "notified": cycle.get("notified", 0),
            "sources_planned": cycle.get("sources_planned", 0),
            "sources_executed": cycle.get("sources_executed", 0),
            "sources_skipped": cycle.get("sources_skipped", []),
            "excluded_paid": cycle.get("excluded_paid", []),
            "high_matches": cycle.get("high_matches", 0),
            "worth_reviewing": cycle.get("worth_reviewing", 0),
            "consecutive_failures": failures,
            "elapsed_seconds": cycle.get("elapsed_seconds"),
            "next_run_at": new_next}


def _maybe_daily_digest(now_utc: datetime) -> None:
    """Digest ditor PER USER (8C2): çdo user aktiv me email-digest të aktivuar
    marrë digest-in e vet — konfigurimi, dedup-i dhe regjistri janë scoped."""
    try:
        import context as _ctxmod
        import notifier
        with repo.db() as con:
            uids = [r["id"] for r in con.execute(
                "SELECT id FROM users WHERE status='active' ORDER BY id").fetchall()]
        for uid in uids:
            try:
                with _ctxmod.run_as(_ctxmod.scheduler_context(uid)):
                    _digest_for_user(now_utc)
            except Exception:
                pass  # digest-i i një user-i kurrë s'thye të tjerët
    except Exception:
        pass  # digest-i kurrë s'thyej tick-un


def _digest_for_user(now_utc: datetime) -> None:
    import notifier
    cfg = notifier.get_config()
    if not cfg["enabled"] or cfg["mode"] not in ("digest", "both"):
        return
    last = repo.last_digest()
    if last and last["sent_at"][:10] == now_utc.strftime("%Y-%m-%d"):
        return
    digest_time = cfg.get("digest_time") or "08:00"
    try:
        hh, mm = (int(x) for x in digest_time.split(":")[:2])
    except ValueError:
        hh, mm = 8, 0
    if (now_utc.hour, now_utc.minute) < (hh, mm):
        return
    notifier.send_digest(now=now_utc)


def run_due(now_utc: datetime | None = None) -> dict:
    """Gjen të gjitha profilet e afërta dhe i ekzekuton — për loop/cron/tick.
    Listimi i schedulave bëhet me kontekst sistem (operacion i brendshëm i
    emërtuar); çdo ekzekutim profili izolohet te pronari i vet (8C2)."""
    import context
    now_utc = now_utc or datetime.now(timezone.utc)
    with context.run_as(context.migration_context()):
        now_s = now_utc.strftime("%Y-%m-%d %H:%M")
        _maybe_daily_digest(now_utc)
        due = []
        for s in repo.list_schedules():
            if not s.get("enabled"):
                continue
            nxt = s.get("next_run_at")
            if not nxt:
                # sched i ri pa next → llogarit nga tani dhe regjistro (pa ekzekutim)
                nxt = compute_next(s.get("cadence", "daily"), s.get("timezone", "Europe/Berlin"),
                                   s.get("run_window_time", "08:00"), now_utc, now_utc)
                repo.upsert_schedule(s["profile_id"], {"next_run_at": nxt},
                                     context.scheduler_context(s.get("user_id") or 1))
                continue
            if _parse(nxt) and _parse(nxt) <= now_utc:
                due.append(s["profile_id"])
    results = [run_scheduled_profile(pid, now_utc=now_utc) for pid in due]
    return {"due": len(due), "results": results,
            "ran": sum(1 for r in results if not r.get("skipped"))}
