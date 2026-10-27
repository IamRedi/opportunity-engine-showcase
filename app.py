"""Opportunity Engine — shtresa e routave (punon si "Job Search" deri në rebrand).

Nisje:  ./run.sh   ose   uvicorn app:app --port 8765

Faza 1 (spec §12, §34): e gjithë SQL-ja jeton në repository.py; emërtimi,
llojet dhe statuse vijnë nga config.py. Kontratat e routave ekzistuese
ruhen — vlerat e vjetra të statusit/llojit pranohen dhe kanonalizohen.
"""
import json
from typing import Annotated, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import BackgroundTasks, FastAPI, Form, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from starlette.concurrency import run_in_threadpool

import ai_service
import applications as apps_svc
import config
import connectors
import evaluation
import generator
import hashlib
import auth
import auth_service
import cv_merge
import cv_parser
import completeness
import tenancy
import notifier
import orchestrator
import opportunity_summary
import profile_context
import pilot_users
import discovery
import external_platforms
import usage_runtime
import usage_ledger
import usage_presenter
import background_domain
import background_jobs
import background_queue
import cloud_access
import cloud_database
import profile_intelligence
import profile_photo
import scheduler as scheduler_svc
import suggestions_rules
import repository as repo
import storage as storage_mod
import i18n
from config import (
    APP_NAME, KIND_LABELS, MODE_KIND, QUICK_ACTIONS, SOURCES, STATUS_LABELS,
    STATUS_SETS, STRIP_KEYS, STRIP_LABELS, canon_kind, canon_status,
)

BASE = config.BASE_DIR

templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals["APP_NAME"] = APP_NAME
templates.env.globals["DEMO_MODE"] = config.DEMO_MODE
templates.env.globals.update(_=i18n.translate, ui=i18n.ui_text,
    ui_locale=i18n.get_locale, ui_catalog=i18n.client_catalog,
    UI_LANGUAGES=i18n.LANGUAGES, enum_label=i18n.translate,
    ui_patterns=i18n.SYSTEM_PATTERNS,
    source_label=lambda sid: i18n.translate(connectors.REGISTRY[sid].name) if sid in connectors.REGISTRY else i18n.translate('Shtuar manualisht' if sid == 'manual' else sid),
    localized_sources=lambda: {**{sid:i18n.translate(c.name) for sid,c in connectors.REGISTRY.items()}, 'manual':i18n.translate('Shtuar manualisht')},
    localized_statuses=lambda: {k:i18n.translate(v) for k,v in STATUS_LABELS.items()})
templates.env.globals['cloud_pilot'] = cloud_database.enabled
templates.env.globals['platform_admin'] = lambda: not cloud_database.enabled() or cloud_access.is_admin(repo._actor(None))
templates.env.filters["kind_label"] = lambda kind: i18n.translate(KIND_LABELS.get(kind, kind))
templates.env.filters["capability_label"] = i18n.translate
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["STATUS_LABELS_JSON"] = json.dumps(STATUS_LABELS, ensure_ascii=False)
templates.env.globals["KIND_LABELS"] = KIND_LABELS
templates.env.globals["LOCAL_CATEGORY_LABELS"] = config.LOCAL_CATEGORY_LABELS

repo.init()

# ---- 8A: fail-fast production + logging level ----
import logging

_PROBLEMS = config.validate_production(config.DB_PATH)
if _PROBLEMS and (config.APP_ENV == "production" or config.os.environ.get('VERCEL')):
    raise RuntimeError("Konfigurim production i pavlefshëm: " + "; ".join(_PROBLEMS))
logging.basicConfig(level=config.LOG_LEVEL)
# HTTP transport diagnostics can include credential-bearing query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# 8C2: në production, admin-i nga env provisionohet si user REAL (idempotent)
# — çdo sesion i mapeohet një ActorContext i verifikuar, pa default user 1.
if config.APP_ENV == "production":
    try:
        auth_service.provision_env_user()
    except Exception as _e:  # fail-soft: login e provon sërish
        logging.getLogger("opportunity.auth").warning("provision_env_user dështoi: %s", _e)

app = FastAPI(title=APP_NAME)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

@app.middleware("http")
async def _offline_showcase_boundary(request: Request, call_next):
    if config.DEMO_MODE and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        from demo_safety import blocked_demo_write
        if blocked_demo_write(request.url.path):
            return JSONResponse({'error': 'Offline demo: external providers are disabled.'}, status_code=403)
    return await call_next(request)

@app.middleware("http")
async def _security_middleware(request: Request, call_next):
    import uuid
    request_id = uuid.uuid4().hex[:12]
    # kufi i madhësisë së trupit (25MB) — mbrojtje e përgjithshme
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > 25 * 1024 * 1024:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "trup shumë i madh"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# ---- 8A/8C2: auth boundary + ActorContext (fail-closed në production) ----
@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    import context as ctx_mod
    if auth.auth_required():
        path = request.url.path
        if not auth.is_public(path):
            token = request.cookies.get(auth.COOKIE_SESSION, "")
            username = auth.verify_session_token(token)
            if not username:
                if path.startswith("/api"):
                    return JSONResponse({"error": "kërkon autentikim"}, status_code=401)
                from fastapi.responses import RedirectResponse
                return RedirectResponse("/login", status_code=303)
            # CSRF për metoda state-changing (double-submit cookie/header)
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and \
                    not path.startswith("/api/auth"):
                cookie = request.cookies.get(auth.COOKIE_CSRF, "")
                header = request.headers.get("X-CSRF-Token", "")
                if not auth.csrf_ok(cookie, header):
                    return JSONResponse({"error": "CSRF token i pavlefshëm"}, status_code=403)
            # ActorContext VETËM nga sesioni → user real + anëtarësi e verifikuar
            # në DB (kurrë user_id/tenant_id nga browser).
            user = auth_service.find_user_by_login(username)
            if not user or user.get("status") != "active":
                return JSONResponse({"error": "user i panjohur ose joaktiv"}, status_code=401)
            try:
                actor = tenancy.resolve_actor(user["id"])
            except tenancy.TenantMembershipError:
                return JSONResponse({"error": "pa anëtarësi aktive tenant"}, status_code=403)
            ctx_mod.set_current(actor)
            request.state.actor = actor
            if cloud_database.enabled() and cloud_access.protected(path,request.method) and not cloud_access.is_admin(actor):
                return JSONResponse({'error':'Konfigurimi i shërbimeve menaxhohet nga pronari.'},status_code=403)
        # rrugë publike → pa kontekst (repo fail-closed mbi të dhëna user)
    else:
        # local/test: dev-context i shprehur (zotëruesi legacy user 1)
        actor=ctx_mod.dev_context()
        if pilot_users.enabled():
            if not pilot_users.local_request(request):return JSONResponse({'error':'Piloti lejohet vetëm në këtë kompjuter.'},status_code=403)
            try:actor=pilot_users.actor(request.cookies.get(pilot_users.COOKIE,''))
            except ValueError as e:return JSONResponse({'error':str(e)},status_code=409)
            request.state.pilot_users=pilot_users.users()
            request.state.pilot_actor=actor.user_id
            if request.method in ('POST','PUT','PATCH','DELETE'):
                origin=request.headers.get('origin')
                if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
                    return JSONResponse({'error':'Origin i pavlefshëm'},status_code=403)
                expected=request.headers.get('X-Pilot-Actor')
                if not expected and 'application/x-www-form-urlencoded' in request.headers.get('content-type',''):
                    # Read cached body; downstream form handlers still receive the original body.
                    from urllib.parse import parse_qs
                    expected=parse_qs((await request.body()).decode()).get('_pilot_actor',[''])[0]
                if not expected and 'multipart/form-data' in request.headers.get('content-type',''):
                    await request.body()
                    expected=str((await request.form()).get('_pilot_actor',''))
                if expected!=str(actor.user_id):return JSONResponse({'error':'Përdoruesi ndryshoi. Rifresko faqen përpara ruajtjes.'},status_code=409)
                if actor.user_id!=1 and request.url.path in ('/settings','/settings/discovery','/api/ai/config','/api/email/config','/api/email/test','/api/email/digest/send','/profile/email'):
                    return JSONResponse({'error':'Konfigurimi i shërbimeve menaxhohet nga pronari.'},status_code=403)
        ctx_mod.set_current(actor)
        request.state.actor=actor
    response = await call_next(request)
    if auth.auth_required() and not request.url.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'private, no-store'
    return response


@app.post('/api/pilot/switch')
async def pilot_switch(request: Request):
    if not pilot_users.enabled() or not pilot_users.local_request(request):raise HTTPException(404)
    data=await request.json()
    uid=data.get('user_id')
    if type(uid)!=int or uid not in [u['id'] for u in pilot_users.users()]:raise HTTPException(404)
    response=JSONResponse({'ok':True})
    response.set_cookie(pilot_users.COOKIE,pilot_users.token(uid),httponly=True,samesite='strict',max_age=86400)
    response.headers['Cache-Control']='private, no-store'
    return response




@app.middleware("http")
async def _locale_middleware(request: Request, call_next):
    selected = request.query_params.get("lang")
    cookie = request.cookies.get("oe_lang", "sq")
    page_locale = request.headers.get("X-UI-Language") if request.url.path.startswith('/api/') else None
    locale = next((v for v in (selected, page_locale, cookie) if v in i18n.LANGUAGES), 'sq')
    token = i18n._locale.set(locale)
    try:
        response = await call_next(request)
        if selected in i18n.LANGUAGES and not request.url.path.startswith('/api/'):
            response.set_cookie("oe_lang", locale, max_age=31536000,
                                samesite="lax", secure=request.url.scheme == "https")
        if "text/html" in response.headers.get("content-type", ""):
            response.headers["Content-Language"] = locale
            vary = response.headers.get("Vary", "")
            response.headers["Vary"] = ", ".join(dict.fromkeys([v.strip() for v in vary.split(",") if v.strip()] + ["Cookie"]))
        return response
    finally:
        i18n._locale.reset(token)


# ------------------------------------------------------------- ndihmës UI

# OE-007D: seksioni "Më të mirat tani" — 3–5 mundësi legjitime në krye.
# Rreshtat e arkivuar/refuzuar dhe parapamjet e paverifikuara s'meritojnë
# vend në krye; feed-i i plotë i mban të gjitha.
TOP_MATCHES_COUNT = 4
_TOP_ELIGIBLE_STATUS = ("archived", "rejected")


def _split_top_matches(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Kryet (top N legjitime sipas renditjes ekzistuese) + pjesa tjetër.
    Pa dublikim: feed-i fillon pas id-ve të shfaqura në krye."""
    if len(jobs) <= TOP_MATCHES_COUNT:
        return [], jobs
    top, top_ids = [], set()
    for j in jobs:
        if j.get("status") in _TOP_ELIGIBLE_STATUS or j.get("excluded"):
            continue
        if (j.get("kd") or {}).get("verification_status") == "search_preview":
            continue
        top.append(j)
        top_ids.add(j["id"])
        if len(top) == TOP_MATCHES_COUNT:
            break
    if len(top) < 2:  # s'ka kuptim një seksion theksimi me < 2 rreshta
        return [], jobs
    return top, [j for j in jobs if j["id"] not in top_ids]


def _quiet_strip(strip: list[dict], total: int) -> list[dict]:
    """OE-007D: chips me 0 fshihen; "Të reja" fshihet kur s'shton informacion
    (të rejat == të gjitha). Statuset e ruajtura s'preken. "Të gjitha" rendered
    veç te template — strip[0] është "Të reja"."""
    quiet = [s for s in strip if s["count"] > 0]
    if quiet and quiet[0]["key"] == "new" and quiet[0]["count"] == total:
        quiet = quiet[1:]
    return quiet


def _chips(params: dict) -> list[dict]:
    labels = {
        "q": lambda v: f"“{v}”",
        "status": lambda v: i18n.translate(STATUS_LABELS.get(v, v)),
        "source": lambda v: i18n.translate(connectors.REGISTRY[v].name) if v in connectors.REGISTRY else v,
        "loc": lambda v: i18n.translate("Vendi: {value}", value=v),
        "remote": lambda v: i18n.translate("Remote" if v == "remote" else "Në zyrë"),
        "mins": lambda v: i18n.translate("Përputhja ≥ {value}%", value=v),
        "days": lambda v: i18n.translate("< {n} ditë", n=v),
        "salary": lambda v: i18n.translate("≥ {value} €/vit", value=f"{int(v):,.0f}".replace(",", ".")),
    }
    chips = []
    for key, label_fn in labels.items():
        val = params.get(key)
        if val in ("", 0, "0", None):
            continue
        rest = {k: v for k, v in params.items() if k != key and v not in ("", 0, "0", None)}
        chips.append({"label": label_fn(val), "url": "/?" + urlencode(rest)})
    return chips


def _adzuna_defaults() -> dict:
    return {
        "what": repo.get_setting("adzuna_what"),
        "where": repo.get_setting("adzuna_where"),
        "country": repo.get_setting("adzuna_country", "de") or "de",
    }


def _source_cards():
    """One configured-state view for every registered connector, including local feeds."""
    labels = {s['id']: s for s in SOURCES}
    return [{**s, **labels.get(s['id'], {}), 'key': s['requires_key']}
            for s in connectors.registry_meta()]


# -------------------------------------------------------------- paneli

# Native GET forms submit unused numeric controls as empty strings.
# Normalize only blanks; retain integer validation for actual input.
FilterInt = Annotated[int, BeforeValidator(
    lambda value: 0 if isinstance(value, str) and not value.strip() else value
)]

@app.get("/")
def workspace(
    request: Request,
    q: str = "", mode: str = "jobs", status: str = "", source: str = "",
    loc: str = "", remote: str = "", mins: FilterInt = 0, days: FilterInt = 0,
    salary: FilterInt = 0, sort: str = "match",
):
    if mode not in MODE_KIND:
        mode = "jobs"
    status = canon_status(status) if status else ""
    kind = MODE_KIND[mode]

    jobs = repo.list_opportunities(
        q=q, kind=kind, status=status, source=source, loc=loc,
        remote=remote, mins=mins, days=days, salary=salary, sort=sort,
    )
    counts = repo.status_counts(kind, include_lenses=True)
    sources = repo.distinct_sources()
    total = sum(counts.values())

    strip = [
        {"key": s, "label": STRIP_LABELS.get(s, s.title()), "count": counts.get(s, 0)}
        for s in STRIP_KEYS.get(mode, STRIP_KEYS["jobs"])
    ]
    strip = _quiet_strip(strip, total)
    top_matches, feed_jobs = _split_top_matches(jobs)
    last_runs = repo.last_run_per_source()
    run_ok = sum(1 for lr in last_runs.values() if lr.get("status") == "ok")
    run_err = sum(1 for lr in last_runs.values() if lr.get("status") != "ok")
    prof = repo.get_profile()

    params = {
        "q": q, "mode": mode, "status": status, "source": source, "loc": loc,
        "remote": remote, "mins": mins if mins else "", "days": days if days else "",
        "salary": salary if salary else "",
    }
    adzuna_ready = bool(repo.get_setting("adzuna_app_id") and repo.get_setting("adzuna_app_key"))

    profiles = []
    for p in repo.list_search_profiles():
        p2 = dict(p)
        p2["kinds_labels"] = " · ".join(KIND_LABELS.get(k, k) for k in p["kinds"]) or "të gjitha"
        p2["apply_query"] = _profile_apply_query(p)
        profiles.append(p2)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "jobs": jobs,
            "top_matches": top_matches,
            "feed_jobs": feed_jobs,
            "strip": strip,
            "total": total,
            "params": params,
            "chips": _chips(params),
            "sources": sources,
            "all_sources": _source_cards(),
            "adzuna_ready": adzuna_ready,
            "sort": sort,
            "profile_empty": not prof["title"] and not prof["skills"],
            "search_profiles": profiles,
            "last_runs": last_runs,
            "run_ok": run_ok,
            "run_err": run_err,
        },
    )


def _profile_apply_query(p: dict) -> str:
    """Si aplikohet profili mbi hapësirën e punës (parametrat ekzistues)."""
    params = {}
    kind = p["kinds"][0] if len(p["kinds"]) == 1 else None
    params["mode"] = {"job": "jobs", "freelance": "freelance", "product_lead": "products", "local_job": "local"}.get(kind, "all")
    if p["min_match"]:
        params["mins"] = p["min_match"]
    if p["location"]:
        params["loc"] = p["location"]
    if p["salary_min"]:
        params["salary"] = p["salary_min"]
    if p["remote_ok"] and not (p["onsite_ok"] or p["hybrid_ok"]):
        params["remote"] = "remote"
    elif (p["onsite_ok"] or p["hybrid_ok"]) and not p["remote_ok"]:
        params["remote"] = "onsite"
    return urlencode(params)


# ------------------------------------------------- panel detajesh (drawer)

def _panel_context(job_id: int):
    j = repo.get_opportunity(job_id)
    if not j:
        return None
    product_fit = None
    if j["kind"] == config.KIND_PRODUCT_LEAD or "product_lead" in (j.get("lenses") or []):
        product_fit = __import__("product_fit").evaluate(j)
    locale = i18n.get_locale()
    cached_summary = opportunity_summary.cached(j, locale)
    return {
        "j": j,
        "breakdown": repo.match_breakdown(j),
        "quick": QUICK_ACTIONS.get(j["kind"], QUICK_ACTIONS[config.KIND_JOB]),
        "statuses": STATUS_SETS.get(j["kind"], config.ALL_STATUSES),
        "applications": apps_svc.application_panel_data(job_id),
        "app_default_type": apps_svc.KIND_DEFAULT_TYPE.get(j["kind"], "job_application"),
        "app_types": apps_svc.TYPES,
        "app_status_labels": apps_svc.STATUS_LABELS,
        "product_fit": product_fit,
        "listing_summary": cached_summary.data if cached_summary else None,
        "summary_language": i18n.LANGUAGES[locale],
    }


@app.get("/api/jobs/{job_id}/panel")
def api_panel(request: Request, job_id: int):
    ctx = _panel_context(job_id)
    if not ctx:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return templates.TemplateResponse(request=request, name="_panel.html", context=ctx)


@app.post("/api/jobs/{job_id}/summary")
def api_job_summary(job_id: int):
    """Generate once on explicit user action; later panel opens use the cache."""
    opportunity = repo.get_opportunity(job_id)
    if not opportunity:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    result = opportunity_summary.generate(opportunity, i18n.get_locale())
    if result.get("busy"):
        return JSONResponse(result, status_code=409)
    if not result.get("ok"):
        return JSONResponse(
            {"error": "Përmbledhja nuk u krijua. Provo përsëri."}, status_code=502
        )
    return result


@app.post("/api/jobs/{job_id}/update")
async def api_update(job_id: int, payload: dict):
    status = payload.get("status")
    if status:
        status = canon_status(str(status))
        if status not in config.ALL_STATUSES:
            return JSONResponse({"error": "status i pavlefshëm"}, status_code=400)
    out = repo.update_opportunity(job_id, status=status or None, notes=payload.get("notes"))
    if not out:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return {"ok": True, "status": out["status"], "status_label": out["status_label"], "notes": out["notes"]}


# ----------------------------------------------------------- kërkimet

@app.post("/api/search/{source}")
async def api_search(source: str, payload: Optional[dict] = None):
    payload = payload or {}
    query = connectors.SearchQuery(
        what=payload.get("what", "") or "",
        where=payload.get("where", "") or "",
        salary_min=int(payload.get("salary_min", 0) or 0),
        country=payload.get("country", "") or "",
    )
    res = await run_in_threadpool(connectors.run_source_search, source, query)
    if not res["ok"]:
        return JSONResponse({"error": res["error"]}, status_code=502)
    return {"source": source, "new": res["new"], "seen": res["discovered"], "dup": res["dup"]}


@app.post("/search/{source}")
def run_search(source: str, what: str = Form(""), where: str = Form(""), country: str = Form("de")):
    res = connectors.run_source_search(
        source, connectors.SearchQuery(what=what, where=where, country=country or "")
    )
    if not res["ok"]:
        return RedirectResponse("/?msg=" + quote(f"{source}: {res['error']}"), status_code=303)
    msg = f"{source}: {res['new']} punë të reja, {res['dup']} duplikate"
    return RedirectResponse("/?msg=" + quote(msg), status_code=303)


# ------------------------------------- profilet e kërkimit (spec §7)

def _profile_fields(data: dict, partial: bool) -> dict | None:
    """Validon/normalizon payload-in e profilit; None kur është i pavlefshëm."""
    out: dict = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        out["name"] = name
    if "kinds" in data:
        kinds = [k for k in (data.get("kinds") or []) if k in config.KINDS]
        out["kinds"] = json.dumps(kinds or [config.KIND_JOB])
    for f in ("keywords", "exclude_keywords"):
        if f in data:
            v = data.get(f) or []
            if isinstance(v, str):
                v = [l.strip() for l in v.splitlines() if l.strip()]
            out[f] = json.dumps([str(x) for x in v], ensure_ascii=False)
    if "sources" in data:
        srcs = [s for s in (data.get("sources") or []) if s in connectors.REGISTRY]
        out["sources"] = json.dumps(srcs)
    if "location" in data:
        out["location"] = str(data.get("location") or "").strip()
    for f in ("radius_km", "salary_min", "min_match", "budget_min"):
        if f in data and data.get(f) is not None and data.get(f) != "":
            try:
                out[f] = max(0, int(float(data[f])))
            except (TypeError, ValueError):
                return None
    if "hourly_rate_min" in data and data.get("hourly_rate_min") is not None and data.get("hourly_rate_min") != "":
        try:
            out["hourly_rate_min"] = max(0.0, float(data["hourly_rate_min"]))
        except (TypeError, ValueError):
            return None
    for f in ("remote_ok", "onsite_ok", "hybrid_ok", "active"):
        if f in data:
            out[f] = 1 if data.get(f) else 0
    return out


@app.get("/api/profiles")
def api_profiles_list():
    return repo.list_search_profiles()


@app.get("/api/profiles/{profile_id}")
def api_profile_get(profile_id: int):
    p = repo.get_search_profile(profile_id)
    if not p:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return p


@app.post("/api/profiles")
async def api_profiles_create(data: dict):
    fields = _profile_fields(data, partial=False)
    if fields is None:
        return JSONResponse({"error": "emri është i detyrueshëm (dhe vlerat të jenë të sakta)"}, status_code=400)
    pid = repo.create_search_profile(fields)
    return JSONResponse({"id": pid}, status_code=201)


@app.patch("/api/profiles/{profile_id}")
async def api_profiles_update(profile_id: int, data: dict):
    fields = _profile_fields(data, partial=True)
    if fields is None:
        return JSONResponse({"error": "vlera të pavlefshme"}, status_code=400)
    if not repo.update_search_profile(profile_id, fields):
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return {"ok": True}


@app.delete("/api/profiles/{profile_id}")
def api_profiles_delete(profile_id: int):
    if not repo.delete_search_profile(profile_id):
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return {"ok": True}


@app.post("/api/profiles/{profile_id}/run")
def api_profiles_run(profile_id: int):
    profile = repo.get_search_profile(profile_id)
    if not profile:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    results = connectors.run_profile_search(profile)
    return {
        "results": results,
        "new_total": sum(r["new"] for r in results),
        "ok_sources": sum(1 for r in results if r["ok"]),
    }


@app.get("/api/runs")
def api_runs(limit: int = 15):
    return repo.recent_search_runs(min(limit, 100))


def _profile_form_context(p: dict | None):
    p = p or {}
    return {
        "p": p,
        "is_new": not p.get("id"),
        "kinds": [(k, label) for k, label in config.KIND_LABELS.items()],
        "src_list": connectors.registry_meta(),
        "keywords_text": "\n".join(p.get("keywords") or []),
        "exclude_text": "\n".join(p.get("exclude_keywords") or []),
    }


@app.get("/api/profiles/new/panel")
def api_profile_new_panel(request: Request):
    return templates.TemplateResponse(
        request=request, name="_profile_form.html",
        context=_profile_form_context(None),
    )


@app.get("/api/profiles/{profile_id}/panel")
def api_profile_panel(request: Request, profile_id: int):
    p = repo.get_search_profile(profile_id)
    if not p:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return templates.TemplateResponse(
        request=request, name="_profile_form.html",
        context=_profile_form_context(p),
    )


# ------------------------------------------------ profil & rregullime

@app.get("/profile")
def profile_page(request: Request, msg: str = ""):
    import resume_ai
    p = repo.get_profile()
    f = repo.get_filters()
    settings = repo.get_all_settings()
    settings['local_feed_urls']=repo.local_feed_urls()
    sections = repo.all_profile_sections()
    products = repo.list_products(active_only=False)
    edit_idx = int(request.query_params.get("edit_product", -1))
    edit_product = next((dict(pr) for pr in products if pr["idx"] == edit_idx), None) if edit_idx >= 0 else None

    def sect(name):
        return _entries_to_lines(name, sections.get(name, {}).get("data", []))

    cv = repo.get_cv()
    if cv:
        cv_status_text = f"{cv['filename']} · {cv['size_bytes']//1024} KB · {cv['parse_status']}"
    else:
        cv_status_text = "CV: nuk është ngarkuar"
    canonical_context = profile_context.build_context(p)
    display_context, profile_edition_ready = resume_ai.profile_view(
        i18n.get_locale(), canonical_context,
    )
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "profile": p,
            "skills_text": "\n".join(p["skills"]),
            "languages_text": "\n".join(p["languages"]),
            "products_text": "\n".join(p["products"]),
            "filters": f,
            "keywords_text": "\n".join(f["keywords"]),
            "exclude_text": "\n".join(f["exclude_keywords"]),
            "locations_text": "\n".join(f["locations"]),
            "settings": settings,
            "adzuna": _adzuna_defaults(),
            "all_sources": _source_cards(),
            "ai_status": api_ai_status(),
            "usage_view": usage_presenter.user_view(),
            "usage_admin": usage_presenter.admin_view() if usage_presenter.can_view_admin() else None,
            "adzuna_ready": bool(settings.get("adzuna_app_id") and settings.get("adzuna_app_key")),
            "msg": msg,
            "sect_skills": sect("skills"),
            "sect_languages": sect("languages"),
            "sect_experience": sect("experience"),
            "sect_education": sect("education"),
            "sect_portfolio": sect("portfolio"),
            "sect_products": sect("products"),
            "cv_exists": bool(cv),
            "cv_status_text": cv_status_text,
            "profile_context": display_context,
            "profile_edition_ready": profile_edition_ready,
            "portrait": (repo.get_profile_section('avatar')[0] or [{}])[0],
            "local_prefs": repo.get_local_prefs(),
            "local_categories": config.LOCAL_CATEGORY_LABELS,
            "email_cfg": notifier.get_config(),
            "email_adapter": notifier.delivery_adapter(),
            "products": products,
            "edit_product": edit_product,
        },
    )


@app.post("/profile")
def profile_post(
    name: str = Form(""), title: str = Form(""), email: str = Form(""),
    phone: str = Form(""), location: str = Form(""), summary: str = Form(""),
    skills: str = Form(""), languages: str = Form(""), experience: str = Form(""),
    products: str = Form(""),
):
    scope = repo.save_profile({
        "name": name.strip(), "title": title.strip(), "email": email.strip(),
        "phone": phone.strip(), "location": location.strip(), "summary": summary.strip(),
        "skills": skills, "languages": languages, "experience": experience.strip(),
        "products": products,
    })
    # OE-007E: fusha pa konsum skorimesh (emri, email, summary) → pa rillogarim.
    n = repo.rescore_all(factors=scope) if scope != "none" else 0
    return RedirectResponse(
        "/profile?msg=" + quote(f"Profili u ruajt — pikët u rillogaritën për {n} zëra"),
        status_code=303,
    )


@app.get("/filters")
def filters_redirect():
    return RedirectResponse("/profile#preferencat", status_code=303)


@app.post("/filters")
def filters_post(
    keywords: str = Form(""), exclude_keywords: str = Form(""),
    locations: str = Form(""),
    remote_ok: bool = Form(False), onsite_ok: bool = Form(False),
    hybrid_ok: bool = Form(False), salary_min: int = Form(0),
):
    scope = repo.save_filters({
        "keywords": keywords, "exclude_keywords": exclude_keywords,
        "locations": locations, "remote_ok": remote_ok, "onsite_ok": onsite_ok,
        "hybrid_ok": hybrid_ok, "salary_min": salary_min,
    })
    # OE-007E: salary/workmode → i pjesshëm; keywords/exclude → i plotë.
    if scope != "none":
        repo.rescore_all(factors=scope)
    return RedirectResponse("/profile?msg=" + quote("Preferencat u ruajtën — pikët u rillogaritën"), status_code=303)


@app.get("/settings")
def settings_redirect():
    return RedirectResponse("/profile#burimet", status_code=303)


@app.get('/discover')
def discovery_page(request:Request,kind:str='job'):
    if kind not in config.KINDS:kind='job'
    profile=repo.get_profile()
    boards,_=repo.get_profile_section('source_boards')
    return templates.TemplateResponse(request=request,name='discovery.html',context={
        'profile':profile,'kind':kind,'products':discovery.product_choices(),
        'local_categories':config.LOCAL_CATEGORY_LABELS,'local_prefs':repo.get_local_prefs(),
        'external_platforms':external_platforms.PLATFORMS,
        'external_terms':config.LOCAL_SEARCH_TERMS,
        'source_kinds':{sid:' '.join(k for k,v in discovery.DEFAULT_SOURCES.items() if sid in v) for sid in connectors.REGISTRY},
        'sources':connectors.registry_meta(),'countries':discovery.COUNTRIES,'boards':boards},headers={'Cache-Control':'private, no-store'})


@app.post('/api/discovery/plan')
async def discovery_plan(request:Request):
    data=await request.json()
    try:return discovery.plan(kind=data.get('kind','job'),what=data.get('what',''),where=data.get('where',''),country=data.get('country','de'),product_id=data.get('product_id',''),categories=data.get('categories'),web_target=data.get('web_target',''))
    except ValueError as e:return JSONResponse({'error':str(e)},status_code=422)


@app.post('/api/discovery/run')
def discovery_run(data:dict):
    try:return discovery.run_task(data)
    except ValueError as e:return JSONResponse({'error':str(e)},status_code=422)


@app.post('/settings/discovery')
async def discovery_settings(request:Request):
    form=await request.form()
    for key in ('serpapi_key','brave_api_key'):
        value=str(form.get(key,'')).strip()
        if value:repo.set_setting(key,value)
    return RedirectResponse('/profile?msg='+quote('Rregullimet e burimeve u ruajtën')+'#burimet',status_code=303)


@app.post('/profile/source-boards')
async def discovery_boards(request:Request):
    import re
    form=await request.form();entries=[]
    for provider in discovery.BOARD_PROVIDERS:
        values=[v.strip() for v in str(form.get(provider,'')).split(',') if v.strip()]
        if len(values)>8 or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',v) for v in values):return JSONResponse({'error':'Përdor deri në 8 identifikues kompanish për burim.'},status_code=422)
        entries.extend({'provider':provider,'board':v} for v in dict.fromkeys(values))
    repo.set_profile_section('source_boards',entries)
    return RedirectResponse('/discover',status_code=303)


@app.post("/settings")
def settings_post(
    adzuna_app_id: str = Form(""), adzuna_app_key: str = Form(""),
    adzuna_what: str = Form(""), adzuna_where: str = Form(""),
    adzuna_country: str = Form("de"),
):
    for key, val in (
        ("adzuna_app_id", adzuna_app_id.strip()),
        ("adzuna_what", adzuna_what.strip()),
        ("adzuna_where", adzuna_where.strip()),
        ("adzuna_country", adzuna_country if adzuna_country in ("de", "at", "ch") else "de"),
    ):
        repo.set_setting(key, val)
    if adzuna_app_key.strip():
        repo.set_setting("adzuna_app_key", adzuna_app_key.strip())
    return RedirectResponse("/profile?msg=" + quote("Rregullimet e burimeve u ruajtën") + "#burimet", status_code=303)


# ----------------------------------------------------- shtim manual

@app.get("/jobs/add")
def job_add_page(request: Request, kind: str = config.KIND_JOB, msg: str = ""):
    kind = canon_kind(kind)
    if kind not in config.KINDS:
        kind = config.KIND_JOB
    return templates.TemplateResponse(
        request=request, name="manual_add.html",
        context={"msg": msg, "kind": kind},
    )


@app.post("/jobs/add")
def job_add(
    title: str = Form(...), url: str = Form(...),
    company: str = Form(""), location: str = Form(""),
    salary_text: str = Form(""), remote: bool = Form(False),
    description: str = Form(""), source: str = Form("manual"),
    kind: str = Form(config.KIND_JOB), opportunity_type: str = Form(""),
    local_category: str = Form(""), postcode: str = Form(""),
    hourly_rate: str = Form(""), schedule: str = Form(""),
):
    op = {
        "source": source.strip() or "manual",
        "url": url,
        "title": title,
        "company": company,
        "location": location,
        "remote": remote,
        "salary_text": salary_text,
        "description": description,
        "tags": [],
        "kind": kind,
        "opportunity_type": opportunity_type or local_category,
    }
    if canon_kind(kind) == config.KIND_LOCAL:
        if hourly_rate:
            op["hourly_rate"] = hourly_rate
        op["kind_data"] = {"postcode": postcode.strip(),
                           "schedule": schedule.strip(),
                           "transport_required": ""}
        if not op["location"]:
            op["location"] = postcode.strip()
    repo.upsert_opportunity(op)
    if canon_kind(kind) == config.KIND_LOCAL:
        mode = "local"
    elif canon_kind(kind) == config.KIND_FREELANCE:
        mode = "freelance"
    else:
        mode = "jobs"
    return RedirectResponse(f"/?mode={mode}&msg=" + quote("U shtua"), status_code=303)


# -------------------------------------------------- detaje (faqe plotë)

@app.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int):
    ctx = _panel_context(job_id)
    if not ctx:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="job_detail.html", context=ctx)


@app.post("/jobs/{job_id}/update")
def job_update(job_id: int, status: Optional[str] = Form(None), notes: Optional[str] = Form(None)):
    status = canon_status(status) if status else None
    if status and status not in config.ALL_STATUSES:
        status = None
    repo.update_opportunity(job_id, status=status, notes=notes)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/delete")
def job_delete(job_id: int):
    repo.delete_opportunity(job_id)
    return RedirectResponse("/", status_code=303)


# --------------------------------------------- AI (faqe dev — spec §15)

@app.get("/api/ai/status")
def api_ai_status():
    cfg = ai_service.get_config()
    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "enabled": usage_runtime.dispatch_available() and cfg["provider"] != "disabled" and bool(cfg["model"]) and
                   (cfg["provider"] != "openai-compatible" or bool(cfg["api_key"].strip())),
        "dispatch_state": "available" if usage_runtime.dispatch_available() else "usage_disabled",
        "requires_key": cfg["provider"] == "openai-compatible" and not cfg["api_key"].strip(),
        "timeout_s": cfg["timeout"],
        "retries": cfg["retries"],
        "max_input_chars": cfg["max_input_chars"],
        "max_output_tokens": cfg["max_output_tokens"],
        "tasks": sorted(ai_service.SCHEMAS),
        "prompt_version": ai_service.PROMPT_VERSION,
        "cache_entries": repo.ai_cache_count(),
        "note": "Skorimi deterministik (rule_score) mbetet burimi i së vërtetës; "
                "ai_score s'përdoret ende në feed.",
    }


@app.get("/api/usage")
def api_usage():
    return usage_presenter.user_view(localized=True)


# -------------------------------- durable work API (OE-004C)

async def _dispatch_work(job_id: str, background_tasks: BackgroundTasks) -> None:
    if background_queue.enabled():
        await background_queue.publish_pending(job_id=job_id)
    elif config.APP_ENV != "production":
        # Explicit local/test harness. Production requires the durable queue.
        background_tasks.add_task(background_domain.run_inline, job_id)


@app.post("/api/work", status_code=202)
async def api_work_create(data: dict, background_tasks: BackgroundTasks):
    data = data or {}
    if config.APP_ENV == "production" and not background_queue.enabled():
        return JSONResponse({"error": i18n.translate("Ky veprim nuk është aktiv për momentin.")}, status_code=503)
    try:
        job = background_domain.prepare(str(data.get("type") or ""), data.get("input") or {})
        await _dispatch_work(job["job_id"], background_tasks)
    except (ValueError, PermissionError) as exc:
        return JSONResponse({"error": i18n.ui_text(str(exc))}, status_code=422)
    except background_queue.QueueDispatchDisabled:
        return JSONResponse({"error": i18n.translate("Ky veprim nuk është aktiv për momentin.")}, status_code=503)
    return {**job, "progress_url": f"/api/work/{job['job_id']}"}


@app.get("/api/work/{job_id}")
def api_work_get(job_id: str):
    try:
        return background_domain.public(job_id)
    except background_jobs.JobNotFound:
        return JSONResponse({"error": i18n.translate("Puna nuk u gjet.")}, status_code=404)


@app.post("/api/work/{job_id}/cancel")
def api_work_cancel(job_id: str):
    try:
        background_jobs.request_cancel(job_id)
        return background_domain.public(job_id)
    except background_jobs.JobNotFound:
        return JSONResponse({"error": i18n.translate("Puna nuk u gjet.")}, status_code=404)


@app.post("/api/work/{job_id}/retry", status_code=202)
async def api_work_retry(job_id: str, background_tasks: BackgroundTasks):
    if config.APP_ENV == "production" and not background_queue.enabled():
        return JSONResponse({"error": i18n.translate("Ky veprim nuk është aktiv për momentin.")}, status_code=503)
    try:
        job = background_jobs.retry(job_id)
        await _dispatch_work(job["job_id"], background_tasks)
    except background_jobs.JobNotFound:
        return JSONResponse({"error": i18n.translate("Puna nuk u gjet.")}, status_code=404)
    except background_jobs.JobConflict as exc:
        return JSONResponse({"error": i18n.ui_text(str(exc))}, status_code=409)
    return {**job, "progress_url": f"/api/work/{job['job_id']}"}


@app.get("/api/admin/usage")
def api_admin_usage():
    actor = repo._actor(None)
    if cloud_database.enabled() and not cloud_access.is_admin(actor):
        return JSONResponse({"error": i18n.translate("Kërkon administratorin e platformës.")}, status_code=403)
    try:
        return usage_presenter.admin_view(actor)
    except PermissionError:
        return JSONResponse({"error": i18n.translate("Kërkon administratorin e platformës.")}, status_code=403)


@app.post("/api/admin/usage/reconcile")
def api_admin_usage_reconcile(data: dict):
    """Settle one unknown provider call from explicit, hashed audit evidence."""
    operator = repo._actor(None)
    if cloud_database.enabled() and not cloud_access.is_admin(operator):
        return JSONResponse({"error": i18n.translate("Kërkon administratorin e platformës.")}, status_code=403)
    data = data or {}
    attempt_id = data.get("attempt_id")
    charge = data.get("charge_amount")
    provider_evidence_id = data.get("provider_evidence_id")
    evidence = data.get("evidence")
    if (type(attempt_id) is not int or attempt_id <= 0 or
            type(charge) is not int or charge < 0 or
            not isinstance(provider_evidence_id, str) or not provider_evidence_id.strip() or
            len(provider_evidence_id) > 200 or
            not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 1_000):
        return JSONResponse({"error": i18n.translate("Kërkesa është e pavlefshme.")}, status_code=422)
    with repo.db() as con:
        row = con.execute(
            """SELECT r.request_id,r.tenant_id,r.user_id,a.state,a.reserved_amount,
                      a.settled_charge
               FROM usage_attempts a JOIN usage_requests r ON r.request_id=a.request_id
               WHERE a.id=?""", (attempt_id,),
        ).fetchone()
    if not row:
        return JSONResponse({"error": i18n.translate("Përdorimi nuk u gjet.")}, status_code=404)
    if row["state"] == "settled":
        return {"ok": True, "attempt_id": attempt_id, "state": "settled",
                "charge_amount": row["settled_charge"], "already_final": True}
    if row["state"] != "sent_unknown" or charge > row["reserved_amount"]:
        return JSONResponse({"error": i18n.translate("Përdorimi nuk mund të pajtohet.")}, status_code=409)
    import context as ctx_mod
    owner = tenancy.resolve_actor(row["user_id"], row["tenant_id"])
    try:
        with ctx_mod.run_as(owner):
            handle = usage_ledger._handle(row["request_id"], owner)
            claim = usage_ledger.claim_reconciliation(
                handle, "platform-provider-audit", lease_seconds=120, ctx=owner,
            )
            usage_ledger.add_evidence(
                claim, provider_evidence_id.strip(), "provider-audit", evidence.strip(), ctx=owner,
            )
            final = usage_ledger.settle(
                claim, charge, {"count_method": "provider-audit"},
                provider_evidence_id=provider_evidence_id.strip(),
                evidence=evidence.strip(), ctx=owner,
            )
    except (usage_ledger.UsageBusy, usage_ledger.UsageConflict,
            usage_ledger.UsageNotFound):
        return JSONResponse({"error": i18n.translate("Përdorimi nuk mund të pajtohet.")}, status_code=409)
    return {"ok": True, "attempt_id": attempt_id, "state": final["state"],
            "charge_amount": final["settled_charge"], "already_final": False}


@app.post("/api/ai/config")
async def api_ai_config(data: dict):
    changed = ai_service.set_config(data or {})
    return {"ok": True, "changed": changed, "status": api_ai_status()}


@app.post("/api/ai/enrich/{job_id}/{task}")
def api_ai_enrich(job_id: int, task: str, force: bool = False):
    """Thirrje manuale për NJË mundësi — pa endpoint bulk (spec: pa eksperiment koste live)."""
    j = repo.get_opportunity(job_id)
    if not j:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    outcome = ai_service.run_task(task, j, force=force)
    return {
        "ok": outcome.ok, "task": outcome.task, "cached": outcome.cached,
        "error": i18n.translate(outcome.error), "data": outcome.data, "meta": outcome.meta,
    }


# --------------------------------- eval kontrollor (Faza 3C — faqe dev)

@app.post("/api/eval/run")
async def api_eval_run(data: dict):
    """Nis run kontrollor — vetëm me POST të qëllimshëm; pa kufij s'niset."""
    data = data or {}
    res = evaluation.start_run(
        task=data.get("task", "semantic_match"),
        provider=data.get("provider", ""),
        model=data.get("model", ""),
        sample_size=int(data.get("sample_size", 25)),
        seed=int(data.get("seed", 42)),
        max_calls=int(data.get("max_calls", 25)),
        max_seconds=int(data.get("max_seconds", 300)),
        label=str(data.get("label", "")),
        force=bool(data.get("force", False)),
    )
    if res.get("blocked"):
        return JSONResponse(res, status_code=403)
    return res


@app.get("/api/eval/runs")
def api_eval_runs():
    return repo.list_eval_runs()


@app.get("/api/eval/runs/{run_id}")
def api_eval_run_detail(run_id: int):
    run = repo.get_eval_run(run_id, with_items=True)
    if not run:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    run["rubric"] = evaluation.RUBRIC
    return run


@app.get("/api/eval/runs/{run_id}/export")
def api_eval_export(run_id: int):
    run = repo.get_eval_run(run_id, with_items=True)
    if not run:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return {
        "run": {k: v for k, v in run.items() if k != "items"},
        "items": run.get("items", []),
        "rubric": evaluation.RUBRIC,
        "exported_at": repo.now(),
        "format_version": 1,
    }


@app.post("/api/eval/items/{item_id}/review")
async def api_eval_review(item_id: int, data: dict):
    data = data or {}
    rating = data.get("rating")
    if rating is not None and (not isinstance(rating, int) or not (1 <= rating <= 5)):
        return JSONResponse({"error": "rating duhet të jetë 1–5 ose null"}, status_code=400)
    ext = data.get("ext") if isinstance(data.get("ext"), dict) else None
    if ext:
        for f in ("extraction_accuracy", "match_judgment", "explanation_usefulness"):
            v = ext.get(f)
            if v is not None and (not isinstance(v, int) or not (1 <= v <= 5)):
                return JSONResponse({"error": f"{f} duhet 1–5 ose null"}, status_code=400)
        for f in ("hallucination", "important_missed"):
            v = ext.get(f)
            if v is not None and not isinstance(v, bool):
                return JSONResponse({"error": f"{f} duhet bool ose null"}, status_code=400)
    okk = repo.set_eval_item_review(item_id, rating, str(data.get("notes", "")), ext=ext)
    if not okk:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return {"ok": True}


@app.get("/api/eval/compare")
def api_eval_compare(ids: str = ""):
    run_ids = []
    for part in ids.split(","):
        part = part.strip()
        if part.isdigit():
            run_ids.append(int(part))
    if len(run_ids) < 1:
        return JSONResponse({"error": "jep ids=1,2,…"}, status_code=400)
    return evaluation.compare_runs(run_ids)


# --------------------------------- aplikime/propozime (Faza 4A)

@app.get("/api/jobs/{job_id}/applications")
def api_app_list(job_id: int):
    return apps_svc.application_panel_data(job_id)


@app.post("/api/jobs/{job_id}/applications")
async def api_app_create(job_id: int, data: dict):
    j = repo.get_opportunity(job_id)
    if not j:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    data = data or {}
    try:
        app_id = apps_svc.create_draft(
            j, app_type=data.get("application_type"), platform=str(data.get("platform", "")),
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": app_id}, status_code=201)


@app.get("/api/applications/{app_id}")
def api_app_detail(app_id: int):
    app = repo.get_application(app_id, with_versions=True)
    if not app:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    app["status_label"] = apps_svc.STATUS_LABELS.get(app["status"], app["status"])
    return app


@app.post("/api/applications/{app_id}/edit")
async def api_app_edit(app_id: int, data: dict):
    data = data or {}
    content = str(data.get("content", ""))
    if not content.strip():
        return JSONResponse({"error": "përmbajtja s'mund të jetë bosh"}, status_code=400)
    try:
        apps_svc.edit_draft(app_id, content, subject=data.get("subject"))
    except LookupError:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/api/applications/{app_id}/regenerate")
def api_app_regenerate(app_id: int):
    app = repo.get_application(app_id)
    if not app:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    j = repo.get_opportunity(app["opportunity_id"])
    try:
        new_id = apps_svc.create_draft(j, app_type=app["application_type"],
                                       regenerate_of=app_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "application_id": new_id,
            "version": repo.get_application(app_id)["latest_version"]}


def _app_status_route(app_id: int, new: str):
    try:
        app = apps_svc.set_status(app_id, new)
    except LookupError:
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "status": app["status"],
            "status_label": apps_svc.STATUS_LABELS.get(new, new),
            "approved_at": app["approved_at"], "submitted_at": app["submitted_at"]}


@app.post("/api/applications/{app_id}/approve")
def api_app_approve(app_id: int):
    return _app_status_route(app_id, "approved")


@app.post("/api/applications/{app_id}/submit")
def api_app_submit(app_id: int):
    """VETËM shënim manual: përdoruesi dërgoi vetë jashtë aplikacionit."""
    return _app_status_route(app_id, "submitted")


@app.post("/api/applications/{app_id}/archive")
def api_app_archive(app_id: int):
    return _app_status_route(app_id, "archived")


@app.post("/api/applications/{app_id}/generate")
async def api_app_generate(app_id: int, data: dict):
    """Gjenerim me AI ON-DEMAND — i(manual); disabled → fallback template."""
    data = data or {}
    res = generator.generate(
        app_id=app_id, style=data.get("style"), tone=data.get("tone", "professional"),
        language=data.get("language", "sq"),
    )
    if not res["application_id"]:
        return JSONResponse(res, status_code=404)
    return res


@app.post("/api/jobs/{job_id}/generate")
async def api_job_generate(job_id: int, data: dict):
    """Gjenero aplikim të re (me AI ose fallback) për mundësinë — on-demand."""
    data = data or {}
    res = generator.generate(
        opp_id=job_id, application_type=data.get("application_type"),
        style=data.get("style"), tone=data.get("tone", "professional"),
        language=data.get("language", "sq"),
    )
    if not res["application_id"]:
        return JSONResponse(res, status_code=404)
    return res


# --------------------------------- ProfileContext + CV (Faza 4C)

CV_DIR = config.BASE_DIR / "uploads"
CV_MAX_BYTES = (4 if config.os.environ.get('VERCEL') else 10) * 1024 * 1024
CV_ALLOWED_EXT = {".pdf", ".docx", ".md", ".txt"}


def _section_lines_to_entries(section: str, text: str) -> list[dict]:
    """Format rreshtor i UI: 'fusha1 | fusha2 | …' → zëra të strukturuar."""
    fields = profile_context.SECTION_SCHEMAS[section]["fields"]
    entries = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        entry = {f: (parts[i] if i < len(parts) else "") for i, f in enumerate(fields)}
        if section == "products" and entry.get("capabilities"):
            entry["capabilities"] = [c.strip() for c in entry["capabilities"].split(",") if c.strip()]
        entries.append(entry)
    return entries


def _entries_to_lines(section: str, entries: list[dict]) -> str:
    fields = profile_context.SECTION_SCHEMAS[section]["fields"]
    lines = []
    for e in entries:
        vals = []
        for f in fields:
            v = e.get(f, "")
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            vals.append(str(v or ""))
        lines.append(" | ".join(vals).rstrip(" |"))
    return "\n".join(lines)


@app.get("/api/profile/context")
def api_profile_context():
    return profile_context.build_context()


@app.get("/api/profile/sections/{section}")
def api_profile_section_get(section: str):
    if section not in profile_context.SECTION_SCHEMAS:
        return JSONResponse({"error": "seksion i panjohur"}, status_code=404)
    data, updated = repo.get_profile_section(section)
    return {"section": section, "data": data, "updated_at": updated}


@app.put("/api/profile/sections/{section}")
async def api_profile_section_put(section: str, data: dict):
    if section not in profile_context.SECTION_SCHEMAS:
        return JSONResponse({"error": "seksion i panjohur"}, status_code=404)
    entries = (data or {}).get("entries")
    if entries is None:
        return JSONResponse({"error": "duhet 'entries'"}, status_code=400)
    okk, err = profile_context.validate_section(section, entries)
    if not okk:
        return JSONResponse({"error": err}, status_code=400)
    scope = repo.set_profile_section(section, entries)
    # OE-007E: seksione pa konsum skorimesh → asnjë rillogarim.
    rescored = repo.rescore_all(factors=scope) if scope != "none" else 0
    return {"ok": True, "section": section, "count": len(entries), "rescored": rescored}


@app.post("/profile/sections")
async def profile_sections_post(request: Request):
    form = await request.form()
    for key, value in form.items():
        if key.startswith("section_"):
            section = key.removeprefix("section_")
            if section in profile_context.SECTION_SCHEMAS:
                entries = _section_lines_to_entries(section, str(value))
                okk, err = profile_context.validate_section(section, entries)
                if okk:
                    scope = repo.set_profile_section(section, entries)
                    if scope != "none":
                        repo.rescore_all(factors=scope)
    return RedirectResponse("/profile#strukture", status_code=303)


def _product_entry_from_form(form) -> dict:
    csv = lambda k: [x.strip() for x in str(form.get(k, "")).split(",") if x.strip()]
    return {
        "name": str(form.get("name", "")).strip(),
        "type": str(form.get("type", "")).strip(),
        "short_description": str(form.get("short_description", "")).strip(),
        "description": str(form.get("description", "")).strip(),
        "capabilities": csv("capabilities"),
        "technologies": csv("technologies"),
        "industries": csv("industries"),
        "languages": csv("languages"),
        "delivery_model": str(form.get("delivery_model", "")).strip(),
        "white_label": form.get("white_label") == "on",
        "customization_level": str(form.get("customization_level", "")).strip(),
        "deployment_model": str(form.get("deployment_model", "")).strip(),
        "demo_url": str(form.get("demo_url", "")).strip(),
        "website_url": str(form.get("website_url", "")).strip(),
        "repo_url": str(form.get("repo_url", "")).strip(),
        "status": str(form.get("status", "active")).strip() or "active",
        "pricing_notes": str(form.get("pricing_notes", "")).strip(),
    }


@app.post("/profile/local")
async def profile_local_post(request: Request):
    form = await request.form()
    cats = [str(c) for c in form.getlist("categories")]
    prefs = {
        "base_location": str(form.get("base_location", "")).strip(),
        "postcode": str(form.get("postcode", "")).strip(),
        "radius_km": str(form.get("radius_km", "")).strip(),
        "transport_mode": str(form.get("transport_mode", "")).strip(),
        "max_travel_min": str(form.get("max_travel_min", "")).strip(),
        "min_hourly_rate": str(form.get("min_hourly_rate", "")).strip(),
        "categories": cats,
        "available_days": str(form.get("available_days", "")).strip(),
        "no_car": form.get("no_car") == "on",
    }
    repo.set_profile_section("local", [prefs])
    if 'local_feed_urls' in form:
        repo.set_setting("local_feed_urls", str(form.get("local_feed_urls")).strip(),ctx=repo._actor(None))
    return RedirectResponse("/profile#lokale", status_code=303)


@app.post("/profile/products")
async def profile_products_post(request: Request):
    form = await request.form()
    entry = _product_entry_from_form(form)
    if not entry["name"]:
        return RedirectResponse("/profile#produkte", status_code=303)
    entries, _ = repo.get_profile_section("products")
    idx = form.get("idx")
    if idx not in (None, "") and str(idx).lstrip("-").isdigit():
        i = int(idx)
        if 0 <= i < len(entries):
            entries[i] = entry
    else:
        entries.append(entry)
    repo.save_products(entries)
    return RedirectResponse("/profile#produkte", status_code=303)


@app.post("/profile/products/action")
async def profile_products_action(request: Request):
    form = await request.form()
    try:
        idx = int(form.get("idx", -1))
    except (TypeError, ValueError):
        idx = -1
    action = str(form.get("action", ""))
    entries, _ = repo.get_profile_section("products")
    if 0 <= idx < len(entries):
        if action == "archive":
            entries[idx]["status"] = "archived"
        elif action == "reactivate":
            entries[idx]["status"] = "active"
        elif action == "delete":
            entries.pop(idx)
        elif action in ("up", "down"):
            j = idx - 1 if action == "up" else idx + 1
            if 0 <= j < len(entries):
                entries[idx], entries[j] = entries[j], entries[idx]
        repo.save_products(entries)
    return RedirectResponse("/profile#produkte", status_code=303)


@app.post("/profile/email")
async def profile_email_post(request: Request):
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}
    data["enabled"] = form.get("email_enabled") == "on"
    notifier.save_config(data)
    return RedirectResponse("/profile#email", status_code=303)


@app.post("/profile/cv")
async def profile_cv_post(request: Request):
    res = await api_profile_cv_upload(request)
    if isinstance(res, dict) and res.get("ok"):
        if res.get('processing'):
            result = res['processing']
            message = _cv_result_message(result)
            return RedirectResponse('/profile?msg=' + quote(message) + '#cv', status_code=303)
        return RedirectResponse("/profile#cv", status_code=303)
    return res


@app.post("/profile/cv/delete")
def profile_cv_delete_form():
    repo.delete_cv()
    return RedirectResponse("/profile#cv", status_code=303)


@app.post("/api/profile/cv")
async def api_profile_cv_upload(request: Request):
    """Upload CV — validim i sigurt: tip i lejuar, madhësi me kap, emër i pastruar,
    ruajtje jashtë repozitorit, sha256. Pa parsing (4C)."""
    form = await request.form()
    f = form.get("file")
    if f is None or not hasattr(f, "filename") or not f.filename:
        return JSONResponse({"error": "skedar mungon"}, status_code=400)
    import os
    safe_name = os.path.basename(f.filename).replace("\\", "_")
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in CV_ALLOWED_EXT:
        return JSONResponse({"error": f"tip i palejuar ({ext}); lejohen: {sorted(CV_ALLOWED_EXT)}"}, status_code=400)
    raw = await f.read(CV_MAX_BYTES + 1)
    if len(raw) > CV_MAX_BYTES:
        return JSONResponse({"error": "Skedari duhet të jetë deri në 4 MB." if config.os.environ.get('VERCEL') else "skedar shumë i madh (>10MB)"}, status_code=413)
    if not raw:
        return JSONResponse({'error': 'CV-ja është bosh.'}, status_code=400)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    uid = repo._uid(repo._actor(None))
    key = f"cv/{uid}/cv-{digest}{ext}"
    metadata = storage_mod.get_storage().save(key, raw)
    stored = str(metadata.get("path", key))
    cv_id = repo.save_cv(safe_name, str(stored), f.content_type or "", len(raw), digest)
    processing = None
    mode = form.get('profile_mode', 'upload')
    if mode in ('autofill', 'review'):
        processing = await run_in_threadpool(_process_cv, cv_id, mode == 'autofill', form.get('use_ai') == 'on')
    cv = repo.get_cv_by_id(cv_id)
    if not cv:
        return JSONResponse({'error': 'CV nuk u gjet'}, status_code=409)
    return {"ok": True, 'processing': processing, "cv": {k: cv[k] for k in
            ("id", "filename", "mime", "size_bytes", "sha256", "parse_status",
             "uploaded_at")}}


@app.get("/api/profile/cv")
def api_profile_cv_get():
    cv = repo.get_cv()
    if not cv:
        return {"status": "none"}
    return {k: cv[k] for k in ("id", "filename", "mime", "size_bytes", "sha256",
                               "parse_status", "uploaded_at")}


@app.delete("/api/profile/cv")
def api_profile_cv_delete():
    return {"ok": repo.delete_cv()}


# --------------------------------- import URL (Faza 5A)

@app.post("/api/import/url")
async def api_import_url(data: dict):
    """Import manual nga URL publik: merr meta bazë (title/og:description),
    normalizon përmes upsert (dedup + provenienca). Fail-soft."""
    if cloud_database.enabled():
        return JSONResponse({'error':'Në pilotin online, shtoje lidhjen dhe titullin te “Shto manualisht”.'},status_code=409)
    data = data or {}
    url = str(data.get("url") or "").strip()
    kind = canon_kind(data.get("kind") or "freelance")
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "URL jo valide"}, status_code=400)
    if kind not in config.KINDS:
        return JSONResponse({"error": "kind i pavlefshëm"}, status_code=400)
    title_hint = str(data.get("title") or "").strip()
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15,
                                     headers={"User-Agent": "Mozilla/5.0 local-import/1.0"}) as cl:
            from urllib.parse import urlparse as _urlparse
            parsed_url=_urlparse(url)
            resp=await usage_runtime.async_http_response('url-import','url-import',
                lambda:cl.get(url),fingerprint={'host':parsed_url.hostname or '',
                    'path_hash':hashlib.sha256(parsed_url.path.encode()).hexdigest()},max_redirects=20)
            resp.raise_for_status()
            html = resp.text[:500_000]
    except (usage_runtime.ProviderDispatchDisabled,usage_ledger.UsageDenied) as e:
        return JSONResponse({"error": i18n.translate(usage_runtime.message_key(e))}, status_code=429)
    except httpx.HTTPError as e:
        return JSONResponse({"error": i18n.translate("Marrja e faqes dështoi. Provo përsëri.")}, status_code=502)

    import re as _re
    def _meta(pattern):
        m = _re.search(pattern, html, _re.IGNORECASE)
        return _re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    title = title_hint or _meta(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)")
    title = title or _meta(r"<title[^>]*>([^<]+)</title>")
    desc = _meta(r"<meta[^>]+property=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)")
    desc = desc or _meta(r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']+)")
    if not title:
        return JSONResponse({"error": "s'u gjet titull në faqe — shto manualisht me titull"}, status_code=422)
    res = repo.upsert_opportunity({
        "source": "url_import", "url": url, "title": title[:200],
        "company": str(data.get("company") or ""),
        "location": str(data.get("location") or ""),
        "remote": bool(data.get("remote")),
        "description": desc[:4000] + (f"\n\n[Importuar nga: {url}]" if desc else f"[Importuar nga: {url}]"),
        "tags": [kind], "kind": kind,
        "opportunity_type": str(data.get("opportunity_type") or ""),
    })
    return {"result": res, "kind": kind, "source": "url_import"}


# --------------------------------- orkestratori (Faza 5C)

@app.post("/api/orchestrator/run")
async def api_orchestrator_run(data: dict):
    """Kërkim i orkestruar on-demand: profil i zgjedhur ose të gjitha burimet."""
    data = data or {}
    res = orchestrator.run(
        profile_id=data.get("profile_id"),
        max_items=int(data.get("max_items", 50)),
        label=str(data.get("label", "")),
    )
    if not res.get("ok"):
        return JSONResponse(res, status_code=404)
    return res


# ------------------------------ schedul + njoftime (Faza 7A)

@app.get("/api/schedules")
def api_schedules_list():
    return repo.list_schedules()


@app.put("/api/schedules/{profile_id}")
async def api_schedule_put(profile_id: int, data: dict):
    if not repo.get_search_profile(profile_id):
        return JSONResponse({"error": "profili nuk u gjet"}, status_code=404)
    data = data or {}
    fields = {}
    for k in ("cadence", "timezone", "run_window_time"):
        if k in data:
            fields[k] = str(data[k]).strip()
    if "cadence" in fields and fields["cadence"] not in scheduler_svc.CADENCES:
        return JSONResponse({"error": "kadencë e pavlefshme"}, status_code=400)
    for k in ("min_match", "radius_km"):
        if k in data:
            try:
                fields[k] = max(0, int(float(data[k])))
            except (TypeError, ValueError):
                return JSONResponse({"error": f"{k} duhet numër"}, status_code=400)
    for k in ("enabled", "notify_new_only", "no_car"):
        if k in data:
            fields[k] = 1 if data[k] else 0
    for k in ("kinds", "sources"):
        if k in data and isinstance(data[k], list):
            fields[k] = json.dumps([str(x) for x in data[k]])
    if "next_run_at" in data:  # override/admin/test
        fields["next_run_at"] = str(data["next_run_at"]).strip() or None
    if "available_days" in data:
        fields["available_days"] = str(data["available_days"]).strip()
    repo.upsert_schedule(profile_id, fields)
    sched = repo.get_schedule(profile_id)
    # rikalkulo next nëse s'është vendosur manualisht
    if "next_run_at" not in data and sched:
        from datetime import datetime, timezone as _tz
        nxt = scheduler_svc.compute_next(
            sched["cadence"], sched.get("timezone", "Europe/Berlin"),
            sched.get("run_window_time", "08:00"),
            scheduler_svc._parse(sched["last_run_at"]) or datetime.now(_tz.utc),
            datetime.now(_tz.utc))
        repo.upsert_schedule(profile_id, {"next_run_at": nxt})
        sched = repo.get_schedule(profile_id)
    return sched


@app.post("/api/schedules/{profile_id}/run-now")
def api_schedule_run_now(profile_id: int):
    """Ekzekuto tani përmes të njëjtit rrug scheduler (CAS e mbron dyfishin).
    IDOR (8C2): profili i huaj → 404 para çdo ekzekutimi."""
    if not repo.get_search_profile(profile_id):
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    res = scheduler_svc.run_scheduled_profile(profile_id)
    if res.get("skipped"):
        return JSONResponse(res, status_code=409)
    return res


@app.post("/api/scheduler/tick")
def api_scheduler_tick():
    """Për cron/systemd: ekzekuto të gjitha profilet e afërta (një tick)."""
    return scheduler_svc.run_due()


@app.get("/api/scheduler/plan")
def api_scheduler_plan():
    """OE-007F: plan DRY-RUN i ciklit të freskisë — çfarë do të ekzekutohej,
    çfarë anashkalohet (me arsye), sa kërkesa priten, çfarë pagese është
    e përjashtuar. Nuk ekzekuton asgjë."""
    import scheduling_policy
    plans = []
    for s in repo.list_schedules():
        if not s.get("enabled"):
            continue
        profile = repo.get_search_profile(s["profile_id"])
        if not profile:
            continue
        plan = scheduling_policy.plan_sources(profile)
        plan["profile_id"] = profile["id"]
        plan["profile"] = profile["name"]
        plans.append(plan)
    return {"plans": plans}


@app.get("/api/scheduler/last-cycle")
def api_scheduler_last_cycle():
    """OE-007F: përmbledhja e fundit e ciklit të freskisë per profil."""
    import scheduling_policy
    out = {}
    for s in repo.list_schedules():
        pid = s["profile_id"]
        raw = repo.get_setting(f"scheduling_last_cycle:{pid}", "")
        if raw:
            try:
                out[str(pid)] = json.loads(raw)
            except ValueError:
                continue
    return {"cycles": out}


@app.get("/api/notifications")
def api_notifications_list(unread_only: int = 0, limit: int = 30):
    items = repo.list_notifications(limit=min(limit, 100), unread_only=bool(unread_only))
    return {"items": items, "unread": repo.unread_notification_count()}


@app.post("/api/notifications/{nid}/read")
def api_notification_read(nid: int):
    return {"ok": repo.mark_notification_read(nid)}


@app.post("/api/notifications/{nid}/dismiss")
def api_notification_dismiss(nid: int):
    return {"ok": repo.dismiss_notification(nid)}


@app.post("/api/notifications/read-all")
def api_notifications_read_all():
    return {"ok": True, "marked": repo.mark_all_notifications_read()}


# ------------------------------ health/readiness/auth (Faza 8A)

@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}


@app.get("/ready")
def ready():
    checks = {}
    try:
        with repo.db() as con:
            v = con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        checks["db"] = "ok" if v else "schema_version mungon"
    except Exception:
        checks["db"] = "unavailable"
    try:
        storage = storage_mod.get_storage()
        if cloud_database.enabled():
            # Public readiness is read-only; the release smoke test validates writes.
            storage.metadata('.release-probe')
        else:
            probe_key = ".ready-probe"
            storage.save(probe_key, b"ok")
            storage.delete(probe_key)
        checks["storage"] = "ok"
    except Exception:
        checks["storage"] = "unavailable"
    okk = all(v == "ok" for v in checks.values())
    return JSONResponse({"ready": okk, "checks": checks}, status_code=200 if okk else 503)


@app.get("/login")
def login_page(request: Request):
    if not auth.auth_required():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    origin=request.headers.get('origin')
    if auth.auth_required() and origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):
        return JSONResponse({'error':'Origin i pavlefshëm'},status_code=403)
    if not cloud_database.enabled() and not auth.rate_limit_login():
        return JSONResponse({"error": "shumë provo — provoni pas 1 minute"}, status_code=429)
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if cloud_database.enabled():
        if len(username)>254 or len(password)>1024:
            return JSONResponse({'error':'kredenciale të pasakta'},status_code=401)
        client_ip=request.client.host if request.client else 'unknown'
        if not cloud_access.login_allowed(username,client_ip):
            return JSONResponse({'error':'shumë provo — provoni pas 1 minute'},status_code=429)
    # 8C2: auth multi-user i pari (users table, scrypt) → sesioni i mapeohet
    # një user-i real. Fallback: kredencialet env → user i provisionuar.
    uid = auth_service.authenticate(username, password)
    if uid is None and auth.check_credentials(username, password):
        uid = auth_service.provision_env_user()
    user = auth_service.find_user_by_login(username) if uid is not None else None
    if user is None or user.get("status") != "active":
        return JSONResponse({"error": "kredenciale të pasakta"}, status_code=401)
    with repo.db() as con:
        con.execute("UPDATE users SET last_login_at=? WHERE id=?",
                    (repo.now(), user["id"]))
    token = auth.make_session_token(username)
    csrf = auth.make_csrf_token()
    response = JSONResponse({"ok": True, "csrf_token": csrf, "user_id": user["id"]})
    import os
    secure = os.environ.get("APP_ENV") == "production"
    response.set_cookie(auth.COOKIE_SESSION, token, httponly=True,
                        samesite="lax", secure=secure, max_age=config.SESSION_TTL_HOURS * 3600)
    response.set_cookie(auth.COOKIE_CSRF, csrf, httponly=False,
                        samesite="lax", secure=secure, max_age=config.SESSION_TTL_HOURS * 3600)
    return response


@app.post("/api/auth/logout")
async def api_auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_SESSION)
    response.delete_cookie(auth.COOKIE_CSRF)
    return response


# ------------------------------ CV parse + sugjerime (Faza 4D)

def _process_cv(cv_id: int, autofill: bool, use_ai: bool, analyze_connections: bool = True) -> dict:
    result = cv_merge.parse_cv(cv_id, use_ai=use_ai)
    if result.get('ok') and autofill:
        result['autofill'] = repo.autofill_cv_suggestions(cv_id, candidate_ids=result['candidate_ids'])
        if not result['autofill'].get('ok'):
            return result['autofill']
        try:
            result['photo'] = profile_photo.autofill(cv_id)
        except Exception:
            result['photo'] = {'status': 'unavailable'}
        if result['autofill']['merged']:
            try:
                result['rescored'] = repo.rescore_all()
            except Exception:
                result['warning'] = 'Profili u plotësua, por pikët nuk u rifreskuan. Ruaj profilin për t’i rillogaritur.'
        if use_ai and analyze_connections:
            result['intelligence'] = profile_intelligence.analyze()
    return result


def _cv_result_message(result):
    if not result.get('ok'):
        return result.get('reason') or 'Nxjerrja dështoi. Provo përsëri.'
    if result.get('autofill'):
        fill = result['autofill']
        message = i18n.translate('CV: {merged} të dhëna u plotësuan; {pending} për rishikim.', merged=fill['merged'], pending=fill['pending']) + (' ' + i18n.translate(result['warning']) if result.get('warning') else '')
        if result.get('photo', {}).get('status') == 'saved':
            message += ' ' + i18n.translate('Fotoja nga CV-ja u vendos në profil. Mund ta ndryshosh te Profili im.')
        elif result.get('photo', {}).get('status') == 'choose':
            message += ' ' + i18n.translate('CV-ja ka disa imazhe. Zgjidh foton tënde te Profili im.')
        return message
    return i18n.translate('CV: {count} sugjerime për rishikim.', count=result['created']) + (' ' + i18n.translate(result['warning']) if result.get('warning') else '')


@app.post('/profile/cv/parse')
async def profile_cv_parse_form(request: Request):
    form = await request.form()
    cv = repo.get_cv()
    if not cv:
        return JSONResponse({'error': 'CV nuk u gjet'}, status_code=404)
    result = await run_in_threadpool(_process_cv, cv['id'], form.get('profile_mode') == 'autofill', form.get('use_ai') == 'on')
    return RedirectResponse('/profile?msg=' + quote(_cv_result_message(result)) + '#cv', status_code=303)


@app.post('/api/cv/{cv_id}/autofill')
def api_cv_autofill(cv_id: int, use_ai: bool = False):
    result = _process_cv(cv_id, True, use_ai)
    return result if result.get('ok') else JSONResponse(result, status_code=404 if not repo.get_cv_by_id(cv_id) else 422)

@app.post("/api/cv/{cv_id}/parse")
def api_cv_parse(cv_id: int, force: bool = False, use_ai: bool = False):
    res = cv_merge.parse_cv(cv_id, force=force, use_ai=use_ai)
    if not res.get("ok") and "nuk u gjet" in res.get("reason", ""):
        return JSONResponse(res, status_code=404)
    counts = {}
    for s in repo.cv_suggestions(cv_id):
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    res["suggestion_counts"] = counts
    return res


@app.get("/api/cv/{cv_id}/suggestions")
def api_cv_suggestions(cv_id: int, status: str = None):
    items = repo.cv_suggestions(cv_id, status)
    for item in items:
        if item['field'] == 'entry':
            values = json.loads(item['proposed_value'])
            item['display_value'] = ' · '.join(v for v in values.values() if v)
    return {"items": items, "count": len(items)}


@app.post("/api/cv/suggestions/{sid}/accept")
def api_cv_suggestion_accept(sid: int):
    return cv_merge.accept_suggestion(sid)


@app.post("/api/cv/suggestions/{sid}/reject")
def api_cv_suggestion_reject(sid: int):
    return cv_merge.reject_suggestion(sid)


@app.get("/api/cv/context")
def api_cv_context():
    """ProfileContext + prova e fakteve (debug/verifikim 4D)."""
    ctx = profile_context.build_context()
    return {"facts": ctx["facts"], "meta": ctx["meta"], "version": ctx["version"]}


# -------------------------------------------------- API (për ZCode/skripte)
def api_email_config():
    cfg = notifier.get_config()
    cfg["delivery_adapter"] = notifier.delivery_adapter()
    cfg["smtp_host_configured"] = bool(cfg["smtp_host"])
    cfg.pop("smtp_pass", None)
    return cfg


# ------------------------------ gaps API (Faza 6)

@app.get("/api/profile/gaps")
def api_gaps_list(active_only: int = 0):
    return repo.list_gaps(active_only=bool(active_only))


@app.post("/api/profile/gaps")
async def api_gaps_create(data: dict):
    data = data or {}
    if data.get("category") not in repo.GAP_CATEGORIES:
        return JSONResponse({"error": "kategoria e pavlefshme"}, status_code=400)
    gid = repo.create_gap({
        "category": data["category"], "label": str(data.get("label", "")) or data["category"],
        "current_state": str(data.get("current_state", "")),
        "target_state": str(data.get("target_state", "")),
        "severity": data.get("severity", "soft") if data.get("severity") in repo.GAP_SEVERITIES else "soft",
        "willingness": data.get("willingness", "") if data.get("willingness") in config.GAP_WILLINGNESS else "",
        "notes": str(data.get("notes", "")),
    })
    return {"id": gid}


@app.patch("/api/profile/gaps/{gid}")
async def api_gaps_update(gid: int, data: dict):
    fields = {k: v for k, v in (data or {}).items()
              if k in ("category", "label", "current_state", "target_state", "severity",
                       "willingness", "notes") and v is not None}
    if "severity" in fields and fields["severity"] not in repo.GAP_SEVERITIES:
        return JSONResponse({"error": "severity e pavlefshme"}, status_code=400)
    if "willingness" in fields and fields["willingness"] not in config.GAP_WILLINGNESS:
        return JSONResponse({"error": "willingness e pavlefshme"}, status_code=400)
    if not repo.update_gap(gid, fields):
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return {"ok": True}


@app.delete("/api/profile/gaps/{gid}")
def api_gaps_delete(gid: int):
    if not repo.delete_gap(gid):
        return JSONResponse({"error": "nuk u gjet"}, status_code=404)
    return {"ok": True}


@app.get("/api/profile/completeness")
def api_profile_completeness(mode: str = "job"):
    return completeness.compute(mode)


@app.get("/api/profile/suggestions")
def api_profile_suggestions(section: str = None):
    return suggestions_rules.suggest(repo.get_profile(),
                                     repo.list_gaps(active_only=True))


@app.post("/api/profile/suggestions/{sid}/confirm")
def api_profile_suggestion_confirm(sid: int):
    return {"ok": repo.confirm_profile_suggestion(sid)}


@app.get("/api/email/config")
def api_email_config():
    cfg = notifier.get_config()
    cfg["delivery_adapter"] = notifier.delivery_adapter()
    cfg["smtp_host_configured"] = bool(cfg["smtp_host"])
    cfg.pop("smtp_pass", None)
    return cfg


@app.post("/api/email/config")
async def api_email_config_save(data: dict):
    notifier.save_config(data or {})
    return {"ok": True, "config": {k: v for k, v in api_email_config().items() if k != "smtp_pass"}}


@app.post("/api/email/test")
def api_email_test():
    """Dërgim provë — vetëm me adapterin e konfiguruar (log në dev, smtp me env)."""
    cfg = notifier.get_config()
    if not cfg["enabled"]:
        return JSONResponse({"ok": False, "reason": "email i çaktivizuar"}, status_code=400)
    sent = notifier.send_email(
        "Opportunity Engine — email provë",
        "Kjo është një email provë nga Opportunity Engine.",
        "<p>Kjo është një <strong>email provë</strong> nga Opportunity Engine.</p>",
        cfg["recipient"])
    return sent


@app.post("/api/email/digest/send")
def api_email_digest_send():
    """Digest on-demand (dev/manual) — dedup i njëjtë si digest-i ditor."""
    res = notifier.send_digest()
    return res


@app.get("/api/notifications/unread-count")
def api_notifications_unread_count():
    return {"unread": repo.unread_notification_count()}


# -------------------------------------------------- API (për ZCode/skripte)

@app.get("/api/jobs")
def api_jobs(status: str = "", kind: str = ""):
    status = canon_status(status) if status else ""
    kind = canon_kind(kind) if kind else None
    rows = repo.list_opportunities(status=status, kind=kind, limit=500)
    # kontrata e vjetër e fushave ruhet; shtojmë vetëm fusha additive
    return [
        {
            "id": j["id"], "title": j["title"], "company": j["org_name"],
            "url": j["source_url"], "source": j["source"], "location": j["location"],
            "remote": int(j["remote"]), "salary_text": j["salary_text"],
            "score": j["rule_score"], "status": j["status"],
            "status_label": j["status_label"], "kind": j["kind"],
            "first_seen": j["discovered_at"],
        }
        for j in rows
    ]


@app.post("/api/jobs")
async def api_add_job(job: dict):
    if not job.get("url") or not job.get("title"):
        return JSONResponse({"error": "duhen fushat 'title' dhe 'url'"}, status_code=400)
    return {"result": repo.upsert_opportunity(job)}


@app.get('/api/profile/intelligence')
def api_profile_intelligence():
    return profile_intelligence.state()


@app.post('/api/profile/intelligence')
def api_analyze_profile():
    return profile_intelligence.analyze()


@app.post('/api/profile/enrich')
def api_enrich_profile():
    cv = repo.get_cv()
    extraction = _process_cv(cv['id'], True, True, analyze_connections=False) if cv else None
    if extraction and not extraction.get('ok'):
        return extraction
    analysis = profile_intelligence.analyze()
    return {'ok': analysis.get('ok',False), 'extraction': extraction, 'analysis': analysis,
            'cached':bool(analysis.get('cached') and (not extraction or extraction.get('cached'))),
            'reason': analysis.get('reason','')}


@app.post('/api/profile/intelligence/{cid}')
def api_decide_concept(cid: str, data: dict):
    if not profile_intelligence.decide(cid, data.get('status')):
        return JSONResponse({'error':'Veprimi nuk u krye. Provo përsëri.'},status_code=400)
    return {'ok':True}


@app.post('/api/profile/discover')
def api_discover_profile(deep: bool = False, expanded: bool = False):
    state=profile_intelligence.state()
    if state['stale'] or not state['concepts']:
        analysis=profile_intelligence.analyze()
        if not analysis.get('ok'): return analysis
    return profile_intelligence.discover(deep=deep,expanded=expanded)


@app.get('/api/profile/adjacent-opportunities')
def api_adjacent_opportunities(mode: str = 'jobs'):
    """OE-007D: kandidatët 'jashtë kërkimit' filtrimen sipas mënyrës aktive —
    pa rrjedhje ndër-mënyra (karta job në feed freelance etj.)."""
    return profile_intelligence.adjacent_opportunities(mode=mode)


def _resume_options(skill_limit=24,include_projects=True,include_personal=False,include_photo=True):
    return {'skill_limit':skill_limit if skill_limit in (12,24,40,200) else 24,
            'include_projects':include_projects,'include_personal':include_personal,'include_photo':include_photo}


@app.get('/profile/resume')
def profile_resume_page(request: Request, skill_limit: int = 24, configured: str = '', projects: str = '', personal: str = '', photo: str = '', cv_language: str = ''):
    import profile_resume,resume_ai
    from urllib.parse import urlencode
    language=cv_language if cv_language in i18n.LANGUAGES else i18n.get_locale()
    options=_resume_options(skill_limit,projects=='yes' if configured else True,personal=='yes',photo=='yes' if configured else True)
    edition=resume_ai.get(language,options)
    data=edition['document'] if edition else profile_resume.document_data(options['skill_limit'],options['include_projects'],options['include_personal'])
    query=urlencode({**options,'cv_language':language})
    return templates.TemplateResponse(request=request,name='resume.html',context={'resume':data,'skill_limit':options['skill_limit'],
        **options,'cv_language':language,'edition_ready':bool(edition),'ai_enabled':ai_service.get_config()['provider']!='disabled',
        'cv_label':lambda text:i18n.CATALOG.get(text,{}).get(language,text),'has_photo':bool(profile_photo.get()),'export_query':query},headers={'Cache-Control':'private, no-store'})


@app.post('/api/profile/resume/generate')
def api_resume_generate(data: dict):
    import resume_ai
    if not isinstance(data.get('skill_limit',24),int) or any(k in data and not isinstance(data[k],bool) for k in ('include_projects','include_personal','include_photo')):
        return JSONResponse({'ok':False,'reason':'Parashikimi është i pavlefshëm.'},status_code=400)
    options=_resume_options(data.get('skill_limit',24),data.get('include_projects',True),data.get('include_personal',False),data.get('include_photo',True))
    return resume_ai.generate(data.get('language'),options)


@app.get('/api/profile/resume.pdf')
def api_profile_resume_pdf(skill_limit: int = 24, include_projects: bool = True, include_personal: bool = False, include_photo: bool = True, cv_language: str = ''):
    import profile_resume,resume_ai
    options=_resume_options(skill_limit,include_projects,include_personal,include_photo)
    language=cv_language if cv_language in i18n.LANGUAGES else i18n.get_locale()
    edition=resume_ai.get(language,options)
    if not edition:
        return JSONResponse({'ok':False,'reason':'Gjenero CV-në në gjuhën e zgjedhur përpara shkarkimit.'},status_code=409,headers={'Cache-Control':'private, no-store'})
    token=i18n._locale.set(language)
    try:
        raw=profile_resume.pdf(**options,document=edition['document'])
    finally:
        i18n._locale.reset(token)
    return Response(raw,media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="cv-{language}.pdf"','Content-Language':language,'Cache-Control':'private, no-store'})


@app.get('/api/profile/intake')
def api_profile_intake():
    import profile_intake
    entries,_=repo.get_profile_section(profile_intake.PREVIEW_SECTION)
    import cv_ai
    return {'defaults':profile_intake.defaults(),'preview':entries[0] if entries else None,
            'schemas':{'contact':sorted(profile_intake.CORE_FIELDS),**cv_ai.ENTRY_FIELDS}}


@app.post('/api/profile/intake/preview')
def api_profile_intake_preview(data: dict):
    import profile_intake
    return profile_intake.preview(data.get('text'))


@app.post('/api/profile/intake/commit')
def api_profile_intake_commit(data: dict):
    import profile_intake
    try: result=profile_intake.commit(data.get('preview_id'),data.get('items'),allow_updates=data.get('allow_updates') is True)
    except ValueError:return JSONResponse({'error':'Parashikimi është i pavlefshëm.'},status_code=400)
    if result.get('ok'):
        result['analysis']=profile_intelligence.analyze()
    return result


@app.post('/api/applications/{app_id}/profile-suggestions')
def api_application_profile_suggestions(app_id: int):
    import profile_intake
    return profile_intake.application_preview(app_id)


@app.get('/api/profile/photo')
def api_get_photo():
    raw = profile_photo.get()
    return Response(raw or b'',status_code=200 if raw else 404,media_type='image/jpeg',
                    headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})


@app.post('/api/profile/photo')
async def api_upload_photo(request: Request):
    form=await request.form(); photo=form.get('file')
    if photo is None or not hasattr(photo,'read'):
        return JSONResponse({'error':'Zgjidh një foto.'},status_code=400)
    try:
        return await run_in_threadpool(profile_photo.save, await photo.read(profile_photo.MAX_BYTES+1))
    except ValueError as error:
        return JSONResponse({'error':str(error)},status_code=400)


@app.delete('/api/profile/photo')
def api_delete_photo():
    profile_photo.delete()
    return {'ok':True}


@app.get('/api/profile/photo/candidates')
def api_cv_photo_candidates():
    cv=repo.get_cv()
    return JSONResponse({'cv_id':cv['id'] if cv else None,'items':profile_photo.previews()},headers={'Cache-Control':'private, no-store'})


@app.post('/api/profile/photo/from-cv')
def api_photo_from_cv(data: dict):
    cv=repo.get_cv()
    if not cv or data.get('cv_id') != cv['id']:
        return JSONResponse({'error':'CV-ja ndryshoi. Rifresko fotot.'},status_code=409)
    items=profile_photo.cv_candidates(); index=data.get('index')
    current=repo.get_cv()
    if not current or current['id'] != cv['id']:
        return JSONResponse({'error':'CV-ja ndryshoi. Rifresko fotot.'},status_code=409)
    if type(index) is not int or not 0 <= index < len(items):
        return JSONResponse({'error':'Zgjidh një foto.'},status_code=400)
    return profile_photo.save(items[index])
