"""Dërgimi i njoftimeve me email (Faza 7B) — provider-independent, fail-soft.

Kanalet: in_app (ekzistues, persistuar në DB) · email (SMTP nga ENV ose
adapter 'log' për dev) · push (e ardhshme). ASNJË email real pa konfigurim
të shprehur (email_enabled + SMTP_HOST).

Parimet:
- Dedup: çdo event nuk dërgohet më shumë se një herë — `email_deliveries`
  me dedup_key unik (përfshin kanalin).
- Fail-soft: SMTP/rrjet → status 'failed' e regjistruar; scheduli dhe
  in-app vazhdojnë normalisht.
- Quiet hours: dërgesat immediate brenda orarit të qetë shtyhen për digest.
- Sekretet VETËM nga env (SMTP_USER/SMTP_PASS) — kurrë në DB/UI.
"""
import json
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import repository as repo

DEFAULT_BASE_URL = "http://127.0.0.1:8765"


# ------------------------------------------------------------- konfigurimi

def get_config(ctx=None) -> dict:
    """Konfigurim per-user (8C2): celesat `u<uid>:email_*` te aktorit, me
    fallback global (pjesshmëri e dhënave të vjetra). Sistem → vetëm global."""
    import context as _ctx
    s = repo.get_all_settings(_ctx.require_context(ctx))
    enabled = s.get("email_enabled") == "1"
    recipient = (s.get("email_recipient") or "").strip()
    mode = s.get("email_mode") or "digest"
    if mode not in ("immediate", "digest", "both", "off"):
        mode = "digest"
    return {
        "enabled": enabled and bool(recipient),
        "recipient": recipient,
        "mode": mode,                       # immediate | digest | both
        "digest_time": s.get("email_digest_time") or "08:00",
        "min_match": max(0, int(s.get("email_min_match") or 70)),
        "kinds": [k.strip() for k in (s.get("email_kinds") or "").split(",") if k.strip()]
                 or ["job", "freelance", "product_lead", "local_job"],
        "quiet_start": s.get("email_quiet_start") or "22:00",
        "quiet_end": s.get("email_quiet_end") or "07:00",
        "base_url": (s.get("email_base_url") or "").rstrip("/") or DEFAULT_BASE_URL,
        "smtp_host": s.get("email_smtp_host") or "",
        "smtp_port": int(s.get("email_smtp_port") or 587),
        "smtp_user": s.get("email_smtp_user") or "",
        # kredencialet VETËM nga env (kurrë DB/UI)
        "smtp_pass": s.get("email_smtp_pass") or "",
    }


def env_smtp() -> dict:
    import os
    return {
        "host": os.environ.get("SMTP_HOST", ""),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": os.environ.get("SMTP_USER", ""),
        "pass": os.environ.get("SMTP_PASS", ""),
        "from": os.environ.get("EMAIL_FROM", os.environ.get("SMTP_USER", "")),
    }


def save_config(data: dict, ctx=None) -> None:
    """Ruaj vetëm fushat jo-sekrete, per-user (8C2). Celesat SMTP mbeten në env."""
    import context as _ctx
    actor = _ctx.require_context(ctx)
    mapping = {
        "email_enabled": lambda v: "1" if str(v).strip().lower() in ("1", "true", "on", "yes") else "0",
        "email_recipient": lambda v: str(v).strip(),
        "email_mode": lambda v: str(v).strip(),
        "email_digest_time": lambda v: str(v).strip(),
        "email_min_match": lambda v: str(max(0, int(float(v or 0)))),
        "email_quiet_start": lambda v: str(v).strip(),
        "email_quiet_end": lambda v: str(v).strip(),
        "email_base_url": lambda v: str(v).strip(),
        "email_kinds": lambda v: ", ".join(str(x) for x in (v if isinstance(v, list) else
                                     [x.strip() for x in str(v).split(",") if x.strip()])),
    }
    for key, cast in mapping.items():
        short = key.removeprefix("email_")
        if short in (data or {}):
            repo.set_setting(key, cast(data[short]), actor)


def delivery_adapter() -> str:
    """'smtp' nëse SMTP_HOST është në env, përndryshe 'log' (dev — pa dërgim real)."""
    import os
    return "smtp" if os.environ.get("SMTP_HOST") else "log"


# ------------------------------------------------------- quiet hours

def in_quiet_hours(now: datetime | None = None, cfg: dict | None = None) -> bool:
    cfg = cfg or get_config()
    def _m(hm):
        try:
            h, m = hm.split(":")
            return int(h) * 60 + int(m)
        except (ValueError, AttributeError):
            return None
    start, end = _m(cfg["quiet_start"]), _m(cfg["quiet_end"])
    if start is None or end is None or start == end:
        return False
    minutes = now.astimezone().hour * 60 + now.astimezone().minute if now else \
        datetime.now().hour * 60 + datetime.now().minute
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # window që kalon mesnatën


# ------------------------------------------------------------ dërgesa

def _smtp_send(subject: str, text: str, html: str, recipient: str) -> dict:
    smtp_env = env_smtp()
    if not smtp_env["host"]:
        return {"ok": False, "error": "SMTP_HOST i pakonfiguruar"}
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_env["from"] or "opportunity-engine@localhost"
    msg["To"] = recipient
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(smtp_env["host"], smtp_env["port"], timeout=20) as server:
            server.starttls()
            if smtp_env["user"]:
                server.login(smtp_env["user"], smtp_env["pass"])
            server.sendmail(msg["From"], [recipient], msg.as_string())
        return {"ok": True}
    except (smtplib.SMTPException, OSError) as e:
        return {"ok": False, "error": f"SMTP: {e}"}


def _log_send(subject: str, text: str, recipient: str) -> dict:
    """Adapter dev: regjistron email-in në notifications-log (pa rrjet)."""
    import logging
    logging.getLogger("opportunity.email").info(
        "EMAIL (dev-log) → %s | %s\n%s", recipient, subject, text[:300])
    return {"ok": True, "logged": True}


def send_email(subject: str, text: str, html: str, recipient: str) -> dict:
    adapter = delivery_adapter()
    if adapter == "log":
        return _log_send(subject, text, recipient)
    return _smtp_send(subject, text, html, recipient)


# -------------------------------------------------- immediate (7B)

def deliver_immediate(op: dict, notification_meta: dict | None = None,
                      now: datetime | None = None, ctx=None) -> dict:
    """Email immediate për event me vlerë të lartë TË AKTORIT. Fail-soft;
    dedup + quiet hours per-user. Kthen {sent, skipped, reason, error}."""
    import context as _ctx
    actor = _ctx.require_context(ctx)
    now = now or datetime.now(timezone.utc)
    cfg = get_config(actor)
    outcome = {"sent": False, "skipped": True, "reason": "", "error": ""}
    if not cfg["enabled"]:
        outcome["reason"] = "email i çaktivizuar"
        return outcome
    if cfg["mode"] == "digest":
        outcome["reason"] = "mënyra digest — immediate e shtyer"
        return outcome
    kind = (notification_meta or {}).get("kind") or op.get("kind") or "job"
    if kind not in cfg["kinds"]:
        outcome["reason"] = f"lloji {kind} jashtë preferencave"
        return outcome
    score = (notification_meta or {}).get("rule_score") or op.get("rule_score") or 0
    if int(cfg["min_match"]) and score < int(cfg["min_match"]):
        outcome["reason"] = f"score {score} < {cfg['min_match']}"
        return outcome
    if in_quiet_hours(now, cfg):
        outcome["reason"] = "quiet hours — shtyrë për digest"
        return outcome

    dedup = f"email:immediate:{op['id']}:{score}"
    if repo.email_delivery_seen(dedup, actor):
        outcome["reason"] = "e dërguar më parë (dedup)"
        outcome["sent"] = False
        return outcome

    subject, text, html = _render_immediate(op, cfg)
    sent = send_email(subject, text, html, cfg["recipient"])
    if sent.get("ok"):
        repo.record_email_delivery(dedup, "sent", recipient=cfg["recipient"], ctx=actor)
        outcome.update(sent=True, skipped=False)
    else:
        repo.record_email_delivery(dedup, "failed", error=sent.get("error", ""),
                                   recipient=cfg["recipient"], ctx=actor)
        outcome["error"] = sent.get("error", "")
    return outcome


def _render_immediate(op: dict, cfg: dict) -> tuple[str, str, str]:
    title = op.get("title") or "Mundësi"
    link = f"{cfg['base_url']}/jobs/{op['id']}"
    bits = [op.get("org_name") or op.get("source") or ""]
    if op.get("location"):
        bits.append(op["location"])
    if op.get("salary_display"):
        bits.append(op["salary_display"])
    subject = f"[{cfg.get('min_match', 0)}%+] {title} — {' · '.join(str(b) for b in bits if b)}"
    text = (f"{title}\n{link}\n\n"
            f"Përputhja: {op.get('rule_score') or 0}%\n"
            + (f"Kompani: {op.get('org_name')}\n" if op.get("org_name") else "")
            + (f"Vendndodhja: {op['location']}\n" if op.get("location") else "")
            + (f"Paga: {op['salary_display']}\n" if op.get("salary_display") else ""))
    html = (f"<h3><a href='{link}'>{title}</a></h3>"
            f"<p>Përputhja: <strong>{op.get('rule_score') or 0}%</strong></p>"
            f"<p>{bits_html(op)}</p><p><a href='{link}'>Hap në aplikacion</a></p>")
    return subject, text, html


def bits_html(op: dict) -> str:
    parts = [op.get("org_name"), op.get("location"), op.get("salary_display")]
    return " · ".join(str(p) for p in parts if p)


# ------------------------------------------------------------- digest

DIGEST_STRONG_MIN = 70   # "përputhje e fortë" — niveli i lartë (OE-007B/C)
DIGEST_REVIEW_MIN = 60   # "ia vlen të shikohen" — kurrë etiketë "high match"


def digest_eligible(since: str, cfg: dict | None = None, ctx=None) -> list[dict]:
    """Mundësitë e reja TË AKTORIT që nga digest-i i fundit, mbi pragun e
    fortë, pa të përjashtuara, të renditura sipas pikëve më të larta."""
    import context as _ctx
    actor = _ctx.require_context(ctx)
    cfg = cfg or get_config(actor)
    strong_min = max(DIGEST_STRONG_MIN, int(cfg["min_match"] or 0))
    return _digest_rows(since, cfg, actor, strong_min)


def _digest_rows(since: str, cfg: dict, actor, min_score: int) -> list[dict]:
    uw, uargs = ("1=1", []) if actor.is_system else \
        ("user_id = ? AND tenant_id = ?", [actor.user_id, actor.tenant_id])
    with repo.db() as con:
        rows = con.execute(
            f"""SELECT id, kind, opportunity_type, title, org_name, location, source,
                      rule_score, salary_text, salary_min, salary_max, remote_type,
                      distance_km, kind_data, source_url, discovered_at
               FROM opportunities
               WHERE excluded = 0 AND discovered_at >= ? AND {uw}
               ORDER BY rule_score DESC, discovered_at DESC""",
            (since, *uargs)).fetchall()
    kinds = set(cfg["kinds"])
    out = []
    for r in rows:
        j = dict(r)
        if j.get("salary_min") or j.get("salary_max"):
            lo = f"{j['salary_min']:,.0f}".replace(",", ".") if j.get("salary_min") else ""
            hi = f"{j['salary_max']:,.0f}".replace(",", ".") if j.get("salary_max") else ""
            j["salary_display"] = f"{lo}–{hi} €" if lo or hi else ""
        else:
            j["salary_display"] = j.get("salary_text") or ""
        if j["kind"] not in kinds:
            continue
        if (j["rule_score"] or 0) < min_score:
            continue
        out.append(j)
    return out


def _group_top(items: list[dict], per_kind: int = 5) -> dict:
    groups: dict = {}
    for it in items:
        groups.setdefault(it["kind"], []).append(it)
    out = {}
    for kind, lst in groups.items():
        lst.sort(key=lambda j: -(j.get("rule_score") or 0))
        out[kind] = lst[:per_kind]
    return out


def send_digest(now: datetime | None = None, ctx=None) -> dict:
    """Digest ditor i aktorit (OE-007F, digest-first): përmblidh mundësitë e
    reja të TIJA që nga digest-i i fundit në dy nivele — "përputhje të forta"
    (≥70) dhe "ia vlen të shikohen" (60–69, kurrë etiketë high). Dedup në
    nivel item; zero mundësi të reja → pa email (regjistrohet ok)."""
    import context as _ctx
    actor = _ctx.require_context(ctx)
    now = now or datetime.now(timezone.utc)
    cfg = get_config(actor)
    if not cfg["enabled"]:
        return {"ok": False, "reason": "email i çaktivizuar"}
    last = repo.last_digest(actor)
    since = last["sent_at"] if last else (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")

    strong_min = max(DIGEST_STRONG_MIN, int(cfg["min_match"] or 0))
    strong = _digest_rows(since, cfg, actor, strong_min)
    review = [j for j in _digest_rows(since, cfg, actor, DIGEST_REVIEW_MIN)
              if (j["rule_score"] or 0) < strong_min]
    items = strong + review
    if not items:
        repo.record_digest_run(since, 0, cfg["recipient"], "ok", ctx=actor)
        return {"ok": True, "items": 0, "reason": "pa mundësi të reja"}

    dedup_keys = [f"email:digest:{it['id']}:{it['discovered_at']}" for it in items]
    fresh = [k for k in dedup_keys if not repo.email_delivery_seen(k, actor)]
    if not fresh:
        repo.record_digest_run(since, 0, cfg["recipient"], "ok", ctx=actor)
        return {"ok": True, "items": 0, "reason": "gjithçka e dërguar më parë (dedup)"}

    labels = {"job": "Punë", "freelance": "Freelance", "product_lead": "Product Leads",
              "local_job": "Lokale"}

    def section(title: str, lst: list[dict]) -> tuple[list[str], list[str], int]:
        lines_out, html_out, shown = [f"== {title} =="], [f"<h3>{title}</h3><ul>"], 0
        for kind, lst_k in _group_top(lst).items():
            lines_out.append(f"-- {labels.get(kind, kind)} --")
            html_out.append(f"<h4>{labels.get(kind, kind)}</h4><ul>")
            for it in lst_k[:5]:
                line = (f"- [{it.get('rule_score') or 0}%] {it['title']}"
                        f" @ {it['org_name'] or it['source']}"
                        f"{' · ' + it['location'] if it['location'] else ''}"
                        f"{' · ' + it['salary_display'] if it['salary_display'] else ''}")
                lines_out.append(line)
                link = f"{cfg['base_url']}/jobs/{it['id']}"
                html_out.append(f"<li><a href='{link}'>{it['title']}</a> — "
                                f"{it.get('rule_score') or 0}% · {it['org_name'] or it['source']}</li>")
                shown += 1
            html_out.append("</ul>")
        html_out.append("</ul>")
        lines_out.append("")
        return lines_out, html_out, shown

    subject = f"Opportunity Engine — {len(strong)} përputhje të forta, {len(review)} për rishikim"
    lines = [f"Digest: {len(strong)} të forta · {len(review)} për rishikim (që nga {since})", ""]
    html_parts = [f"<h2>{len(strong)} përputhje të forta · {len(review)} për rishikim</h2>"]
    total_shown = 0
    if strong:
        l, h, n = section(f"Përputhje të forta (≥{strong_min}%)", strong)
        lines += l
        html_parts += h
        total_shown += n
    if review:
        l, h, n = section(f"Ia vlen të shikohen ({DIGEST_REVIEW_MIN}–{strong_min - 1}%)", review)
        lines += l
        html_parts += h
        total_shown += n
    text = "\n".join(lines)
    html = "\n".join(html_parts)

    try:
        sent = send_email(subject, text, html, cfg["recipient"])
    except Exception as e:
        sent = {"ok": False, "error": f"e papritur: {e}"}
    if not sent.get("ok"):
        repo.record_digest_run(since, len(items), cfg["recipient"], "failed",
                               sent.get("error", ""), ctx=actor)
        return {"ok": False, "items": len(items), "error": sent.get("error", "")}
    for key in dedup_keys:
        repo.record_email_delivery(key, "sent", recipient=cfg["recipient"], ctx=actor)
    repo.record_digest_run(since, len(items), cfg["recipient"], "ok", ctx=actor)
    return {"ok": True, "items": len(items), "shown": total_shown,
            "strong": len(strong), "review": len(review)}
