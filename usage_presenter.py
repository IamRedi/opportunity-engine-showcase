"""Safe, localized presentation data for the OE-003 usage ledger.

User views are actor-scoped through ``usage_ledger.budget``.  The platform
diagnostic view contains aggregate counters only; request identifiers, account
identifiers, provider evidence and usage metadata never leave this module.
"""
from __future__ import annotations

from decimal import Decimal

import cloud_access
import cloud_database
import i18n
import repository as repo
import usage_ledger


METERS = (
    ("ai", "AI për profilin dhe dokumentet"),
    ("search", "Kërkim në burime të jashtme"),
)

STATUS_MESSAGES = {
    "disabled": "Shërbimet me pagesë janë përkohësisht joaktive.",
    "not_configured": "Limiti nuk është konfiguruar ende për këtë llogari.",
    "pending": "Një përdorim i mëparshëm po konfirmohet. Kërkesat e reja presin për të shmangur tejkalimin e limitit.",
    "exhausted": "Limiti i këtij muaji është përdorur.",
    "near_limit": "Po i afrohesh limitit të këtij muaji.",
    "ready": "Gati për përdorim.",
}


def _format_amount(amount: int, currency: str, scale: int) -> str:
    scale = max(1, int(scale))
    value = Decimal(int(amount)) / Decimal(scale)
    decimals = max(0, len(str(scale)) - 1) if scale in (10, 100, 1000, 10000) else 2
    text = f"{value:.{decimals}f}"
    if i18n.get_locale() in ("sq", "de"):
        text = text.replace(".", ",")
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper())
    return f"{text} {symbol or currency.upper()}"


def _meter_view(meter: str, label: str, enforcement: str, ctx=None) -> dict:
    try:
        raw = usage_ledger.budget(meter, ctx=ctx)
    except Exception:
        raw = {"configured": False, "meter": meter}
        enforcement = "disabled"

    view = {
        "meter": meter,
        "label": label,
        "configured": bool(raw.get("configured")),
        "status": "not_configured",
        "status_message": STATUS_MESSAGES["not_configured"],
        "percent": 0,
        "period_end": raw.get("period_end", ""),
    }
    if raw.get("configured"):
        limit = int(raw["limit"])
        committed = int(raw["committed"])
        available = int(raw["available"])
        if limit <= 0:
            percent = 100 if committed >= 0 else 0
        else:
            percent = round(committed * 100 / limit)
        view.update({
            "limit": limit,
            "committed": committed,
            "available": available,
            "limit_display": _format_amount(limit, raw["currency"], raw["currency_scale"]),
            "committed_display": _format_amount(committed, raw["currency"], raw["currency_scale"]),
            "available_display": _format_amount(max(0, available), raw["currency"], raw["currency_scale"]),
            "percent": max(0, min(100, percent)),
        })

        if raw.get("blocked_unknown"):
            status = "pending"
        elif available <= 0:
            status = "exhausted"
        elif percent >= 80:
            status = "near_limit"
        else:
            status = "ready"
        view["status"] = status
        view["status_message"] = STATUS_MESSAGES[status]

    if enforcement != "active":
        view["status"] = "disabled"
        view["status_message"] = STATUS_MESSAGES["disabled"]
    return view


def user_view(ctx=None, *, localized: bool = False) -> dict:
    """Return current-actor totals only, safe for the customer UI and API."""
    enforcement = usage_ledger.enforcement_state()
    view = {
        "enforcement": enforcement,
        "meters": [_meter_view(meter, label, enforcement, ctx=ctx)
                   for meter, label in METERS],
        "estimate_notice": "Shumat janë vlerësime të brendshme për kontrollin e limitit; fatura e ofruesit mund të ndryshojë.",
        "cache_notice": "Rezultatet e ruajtura ripërdoren pa konsumuar limit të ri.",
    }
    if localized:
        view["estimate_notice"] = i18n.translate(view["estimate_notice"])
        view["cache_notice"] = i18n.translate(view["cache_notice"])
        for meter in view["meters"]:
            meter["label"] = i18n.translate(meter["label"])
            meter["status_message"] = i18n.translate(meter["status_message"])
    return view


def _platform_admin(actor) -> bool:
    if cloud_database.enabled():
        return cloud_access.is_admin(actor)
    return bool(actor and (actor.is_system or actor.role in ("owner", "platform_admin")))


def can_view_admin(ctx=None) -> bool:
    """Use the same policy before rendering the optional admin diagnostic."""
    return _platform_admin(repo._actor(ctx))


def admin_view(ctx=None) -> dict:
    """Return redacted platform counters; never rows attributable to one user."""
    actor = repo._actor(ctx)
    if not _platform_admin(actor):
        raise PermissionError("platform-admin-required")
    base = {
        "enforcement": usage_ledger.enforcement_state(),
        "schema_ready": False,
        "budget_accounts": 0,
        "requests": 0,
        "settled": 0,
        "in_progress": 0,
        "unresolved": 0,
    }
    try:
        period_start, _ = usage_ledger.month_window()
        with repo.db() as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'usage_%'").fetchall()}
            required = {"usage_budget_periods", "usage_requests", "usage_attempts"}
            if not required.issubset(tables):
                return base
            base["schema_ready"] = True
            base["budget_accounts"] = int(con.execute(
                "SELECT COUNT(DISTINCT CAST(tenant_id AS TEXT)||':'||CAST(user_id AS TEXT)) "
                "FROM usage_budget_periods WHERE period_start=?", (period_start,)).fetchone()[0])
            row = con.execute("""SELECT COUNT(DISTINCT r.request_id) requests,
                COALESCE(SUM(CASE WHEN a.state='settled' THEN 1 ELSE 0 END),0) settled,
                COALESCE(SUM(CASE WHEN a.state IN ('reserved','dispatching') THEN 1 ELSE 0 END),0) in_progress,
                COALESCE(SUM(CASE WHEN a.state IN ('sent_unknown','reconciling') THEN 1 ELSE 0 END),0) unresolved
                FROM usage_requests r
                JOIN usage_budget_periods p ON p.id=r.period_id
                LEFT JOIN usage_attempts a ON a.request_id=r.request_id
                WHERE p.period_start=?""", (period_start,)).fetchone()
            base.update({key: int(row[key] or 0)
                         for key in ("requests", "settled", "in_progress", "unresolved")})
    except Exception:
        return base
    return base
