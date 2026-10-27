"""Klasifikimi me shumë lentë (spec §5, Faza 5B) — deterministic, pa AI.

Një mundësi reale mund të interpretohet përmes disa lentëve analitike pa
duplikuar rekordin: baza `kind` mbetet kanonike (kompatibiteti i vjetër),
ndërsa `opportunity_lenses` mban interpretime shtesë me dëshmi sinjali.

Sinjalet e njohura (kategorri aftësish/gjenerimi — konfigurueshme):
website, booking, automation, ai, internal_tool, saas, white_label,
workflow, social, crm, custom_software.
"""
import json
import re
from contextlib import nullcontext

import repository as repo

# kategoria → fjalë-kyçe (lowercase; përmbajtje)
SIGNALS = {
    "website": ("website", "web page", "web site", "landing page", "web presence"),
    "booking": ("booking", "appointment", "rezervim", "scheduling system"),
    "automation": ("automation", "automate", "automatizo", "automatizim"),
    "ai": ("ai ", "ai-", "chatbot", "llm", "gpt", "machine learning", "artificial intelligence"),
    "internal_tool": ("internal tool", "internal software", "admin tool", "intranet"),
    "saas": ("saas", "software as a service", "subscription platform"),
    "white_label": ("white-label", "white label"),
    "workflow": ("workflow", "work flow", "process management"),
    "social": ("social media", "instagram", "facebook", "linkedin tooling", "social tool"),
    "crm": ("crm", "customer management", "lead management", "customer relationship"),
    "custom_software": ("custom software", "bespoke", "custom development", "custom tool",
                        "custom platform"),
}

# sinjalet që mbajnë lens-in product_lead
PRODUCT_LEAD_SIGNALS = tuple(SIGNALS)


def detect_signals(text: str) -> dict:
    """Kthen {kategoria: [frazat e gjetura]} — deterministik."""
    lower = (text or "").lower()
    out = {}
    for cat, phrases in SIGNALS.items():
        hits = [p for p in phrases if p in lower]
        if hits:
            out[cat] = hits
    return out


def detect_lenses(op: dict) -> list[dict]:
    """Kthen lentët E REJA që i përkasin mundësisë (≠ baza kind).
    Rreshtat me bazë `job` marrin vetëm lentën job (implicit) — një postim pune
    që përmend 'AI' nuk bëhet product_lead (mësim nga live-5B)."""
    kind = op.get("kind") or "job"
    if kind == "job":
        text=(str(op.get('title') or '')+' '+str(op.get('description') or '')).casefold()
        if re.search(r'\b(mini[- ]?job|nebenjob|aushilfe)\b',text):
            return [{'lens':'local_job','source':'derived','detected_need':'','opportunity_type':'minijob','reason':'Njoftimi përmend minijob, nebenjob ose aushilfe.'}]
        if op.get('_local_search'):
            return [{'lens':'local_job','source':'search','detected_need':'','opportunity_type':'','reason':'U gjet nga kërkimi për punë lokale.'}]
        return []
    text = " ".join([
        op.get("title") or "", op.get("description") or "",
        op.get("opportunity_type") or "",
        " ".join(str(t) for t in (op.get("tags") or [])),
    ])
    signals = detect_signals(text)
    lenses = []
    if kind != "product_lead" and signals:
        lenses.append({
            "lens": "product_lead",
            "source": "derived",
            "detected_need": (op.get("kind_data") and
                              (op["kind_data"].get("detected_need") if isinstance(op["kind_data"], dict) else "")) or "",
            "opportunity_type": op.get("opportunity_type") or "",
            "reason": "sinjale: " + ", ".join(sorted(signals)),
        })
    if kind != "freelance":
        freelance_hints = ("freelance", "contract", "project basis", "per project",
                           "fixed price", "fixed-price")
        if any(h in text.lower() for h in freelance_hints):
            lenses.append({
                "lens": "freelance", "source": "derived", "detected_need": "",
                "opportunity_type": "", "reason": "fjalë-kyçe freelance/kontratë",
            })
    return lenses


def attach_lenses(op_id: int, op: dict, connection=None) -> int:
    """Shton lentët e reja (INSERT OR IGNORE — unike sipas (opp, lens)).
    Kthen sa u shtuan. Nuk prek kurrë bazën `kind`."""
    import json as _json
    ts = repo.now()
    added = 0
    # Reuse an active import transaction instead of competing with its write lock.
    with (nullcontext(connection) if connection is not None else repo.db()) as con:
        existing = {r["lens"] for r in con.execute(
            "SELECT lens FROM opportunity_lenses WHERE opportunity_id=?", (op_id,))}
        for lens in detect_lenses(op):
            if lens["lens"] in existing:
                continue
            kd = op.get("kind_data")
            need = lens.get("detected_need") or (
                kd.get("detected_need", "") if isinstance(kd, dict) else "")
            con.execute(
                """INSERT OR IGNORE INTO opportunity_lenses
                   (opportunity_id, lens, source, detected_need, opportunity_type,
                    reason, created_at) VALUES (?,?,?,?,?,?,?)""",
                (op_id, lens["lens"], lens["source"], need, lens["opportunity_type"],
                 lens["reason"], ts),
            )
            added += 1
    return added


def backfill_all() -> int:
    """Migrim 5B: klasifikon mundësitë ekzistuese dhe i lidh lentët e reja."""
    with repo.db() as con:
        rows = con.execute(
            "SELECT id, kind, title, description, opportunity_type, tags, kind_data FROM opportunities"
        ).fetchall()
    total = 0
    for r in rows:
        op = dict(r)
        try:
            op["kind_data"] = json.loads(op.get("kind_data") or "{}")
        except (ValueError, TypeError):
            op["kind_data"] = {}
        total += attach_lenses(op["id"], op)
    return total


def lenses_for_ids(ids: list[int]) -> dict:
    """{opportunity_id: [lens, …]} — një query për feed-un."""
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    with repo.db() as con:
        rows = con.execute(
            f"SELECT opportunity_id, lens FROM opportunity_lenses WHERE opportunity_id IN ({ph})",
            ids,
        ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["opportunity_id"], []).append(r["lens"])
    return out
