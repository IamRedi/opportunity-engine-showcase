"""Opportunity Engine domain handlers and public progress projection (OE-004C)."""
from __future__ import annotations

import asyncio

import background_jobs
import background_worker
import context
import discovery
import i18n
import repository as repo
import work_requests

PUBLIC_ERRORS = {
    "provider_unavailable": "Burimet e jashtme nuk u përgjigjën. Provo përsëri.",
    "provider_outcome_unknown": "Një kërkesë po verifikohet ende. Mos e përsërit tani.",
    "budget_exhausted": "Limiti i këtij muaji është përdorur.",
    "invalid_output": "Rezultati nuk kaloi kontrollin. Provo përsëri.",
    "timeout": "Puna zgjati shumë. Provo përsëri.",
    "worker_error": "Puna u ndërpre. Provo përsëri.",
    "poison_message": "Puna dështoi pas disa provash.",
    "dispatch_failed": "Puna nuk hyri në radhë. Provo përsëri.",
    "handler_missing": "Ky veprim nuk është ende aktiv.",
}


def _actor(explicit=None):
    actor = context.require_context(explicit)
    if actor.is_system:
        raise PermissionError("Background work requires one authenticated user.")
    return actor


def _language(value) -> str:
    return value if value in i18n.LANGUAGES else i18n.get_locale()


def _profile_discovery_plan() -> dict:
    import connectors
    import profile_intelligence
    state = profile_intelligence.state()
    if state.get("stale") or not state.get("concepts"):
        raise ValueError("Rifresko lidhjet e profilit përpara kërkimit.")
    active = sorted(
        [item for item in state["concepts"] if item.get("category") == "role"
         and item.get("status") in ("active", "confirmed")],
        key=lambda item: item.get("status") != "confirmed",
    )
    primary = [item for item in active if not item.get("adjacent")]
    adjacent = [item for item in active if item.get("adjacent")]
    roles = (primary[:2] + adjacent[:4])[:6]
    roles += [item for item in active if item not in roles][:6-len(roles)]
    if not roles:
        raise ValueError("Aktivizo të paktën një drejtim kërkimi.")
    profile = repo.get_profile()
    tasks = []
    if connectors.REGISTRY["adzuna"].is_configured():
        for role in roles:
            tasks.append({
                "source": "adzuna", "what": role["labels"]["de"],
                "where": profile.get("location", ""), "country": "de",
                "kinds": ["job"], "limit": 30, "category": "", "site": "",
            })
    broad = discovery.plan(
        "job", what=roles[0]["labels"].get("en") or roles[0]["labels"]["de"],
        where=profile.get("location", ""),
    )
    tasks.extend(task for task in broad["tasks"] if task["source"] != "adzuna")
    tasks = tasks[:14]
    if not tasks:
        raise ValueError("Nuk ka burime të lidhura për këtë kërkim.")
    return {"tasks": tasks, "query": roles[0]["labels"].get("en", ""),
            "where": profile.get("location", ""), "country": "de", "kind": "job",
            "categories": []}


def prepare(job_type: str, data: dict | None, ctx=None) -> dict:
    """Validate a user action, persist its domain request, then create one job."""
    actor = _actor(ctx)
    data = data or {}
    if not isinstance(data, dict):
        raise ValueError("Veprimi është i pavlefshëm.")
    revision = background_jobs.profile_revision_token(actor)
    if job_type == "profile_discovery":
        plan = (_profile_discovery_plan() if data.get("profile_based") is True else
                discovery.plan(
                    kind=data.get("kind", "job"), what=data.get("what", ""),
                    where=data.get("where", ""), country=data.get("country", "de"),
                    product_id=data.get("product_id", ""), categories=data.get("categories"),
                    web_target=data.get("web_target", ""),
                ))
        if not plan["tasks"]:
            raise ValueError("Nuk ka burime të lidhura për këtë kërkim.")
        payload = {"plan": plan}
        request_id = work_requests.store(job_type, payload,
            profile_revision_token=revision, ctx=actor)
        values = {
            "request_id": request_id,
            "sources": [task["source"] for task in plan["tasks"]],
            "max_items": 30, "kind": plan["kind"], "language": _language(data.get("language")),
        }
    elif job_type == "profile_intelligence":
        payload = {"language": _language(data.get("language"))}
        request_id = work_requests.store(job_type, payload,
            profile_revision_token=revision, ctx=actor)
        values = {"request_id": request_id, "language": payload["language"]}
    elif job_type == "resume_generation":
        skill_limit = data.get("skill_limit", 24)
        if type(skill_limit) is not int or skill_limit not in (12, 24, 40, 200):
            raise ValueError("Parashikimi është i pavlefshëm.")
        if any(key in data and not isinstance(data[key], bool)
               for key in ("include_projects", "include_personal", "include_photo")):
            raise ValueError("Parashikimi është i pavlefshëm.")
        payload = {
            "language": _language(data.get("language")), "skill_limit": skill_limit,
            "include_projects": data.get("include_projects", True),
            "include_personal": data.get("include_personal", False),
            "include_photo": data.get("include_photo", True),
        }
        request_id = work_requests.store(job_type, payload,
            profile_revision_token=revision, ctx=actor)
        values = {"request_id": request_id, "language": payload["language"],
                  "include_photo": payload["include_photo"], "template": "professional"}
    elif job_type == "application_draft":
        language = _language(data.get("language"))
        tone = data.get("tone", "professional")
        if tone not in ("professional", "concise", "friendly"):
            raise ValueError("Toni është i pavlefshëm.")
        app_id = data.get("application_id")
        opportunity_id = data.get("opportunity_id")
        if app_id is not None:
            if type(app_id) is not int or not repo.get_application(app_id):
                raise ValueError("Aplikimi nuk u gjet.")
        if opportunity_id is not None:
            if type(opportunity_id) is not int or not repo.get_opportunity(opportunity_id):
                raise ValueError("Mundësia nuk u gjet.")
        if app_id is None and opportunity_id is None:
            raise ValueError("Mundësia nuk u gjet.")
        app_type = data.get("application_type")
        if app_type is not None:
            import applications
            if app_type not in applications.TYPES:
                raise ValueError("Lloji i aplikimit është i pavlefshëm.")
        payload = {"language": language, "tone": tone}
        if app_id is not None: payload["application_id"] = app_id
        if opportunity_id is not None: payload["opportunity_id"] = opportunity_id
        if app_type is not None: payload["application_type"] = app_type
        request_id = work_requests.store(job_type, payload,
            profile_revision_token=revision, ctx=actor)
        values = {"request_id": request_id, "language": language}
        if app_id is not None: values["application_id"] = app_id
        if opportunity_id is not None: values["opportunity_id"] = opportunity_id
    else:
        raise ValueError("Ky veprim nuk mbështetet.")
    return background_jobs.create(job_type, values, ctx=actor)


def _request(detail: dict, actor) -> dict:
    request_id = detail["input"].get("request_id")
    if not request_id:
        raise background_worker.TerminalStepError("invalid_output")
    return work_requests.load(request_id, detail["job_type"], ctx=actor)


def _discovery(detail, step, claim):
    actor = context.require_context()
    request = _request(detail, actor)
    plan = request["plan"]
    if step["step_type"] == "summarize":
        prior = [item for item in detail["steps"] if item["sequence"] < step["sequence"]]
        run_ids = [int(item["result_ref"].split(":", 1)[1]) for item in prior
                   if item["result_ref"].startswith("search-run:")]
        if not run_ids:
            raise background_worker.TerminalStepError("provider_unavailable")
        placeholders = ",".join("?" for _ in run_ids)
        with repo.db() as con:
            rows = con.execute(
                f"SELECT status FROM search_runs WHERE user_id=? AND id IN ({placeholders})",
                (actor.user_id, *run_ids),
            ).fetchall()
        if not any(row["status"] == "ok" for row in rows):
            raise background_worker.TerminalStepError("provider_unavailable")
        return f"search-batch:{detail['job_id']}"
    index = step["sequence"] - 1
    if index >= len(plan["tasks"]):
        raise background_worker.TerminalStepError("invalid_output")
    task = plan["tasks"][index]
    if step["step_type"] != f"source:{task['source']}":
        raise background_worker.TerminalStepError("invalid_output")
    result = discovery.run_task(task)
    return f"search-run:{int(result['run_id'])}"


def _intelligence(detail, step, claim):
    import profile_intelligence
    if step["step_type"] == "analyze":
        result = profile_intelligence.analyze()
        if not result.get("ok"):
            raise background_worker.TerminalStepError("provider_unavailable")
        return "profile-intelligence:analyzed"
    state = profile_intelligence.state()
    if state.get("stale") or not state.get("concepts"):
        raise background_worker.TerminalStepError("invalid_output")
    return "profile-intelligence:current"


def _resume(detail, step, claim):
    import resume_ai
    request = _request(detail, context.require_context())
    options = {key: request[key] for key in (
        "skill_limit", "include_projects", "include_personal", "include_photo"
    )}
    if step["step_type"] == "draft":
        result = resume_ai.generate(request["language"], options)
        if not result.get("ok"):
            raise background_worker.TerminalStepError("provider_unavailable")
        return f"resume:{request['language']}"
    if not resume_ai.get(request["language"], options):
        raise background_worker.TerminalStepError("invalid_output")
    return f"resume:{request['language']}"


def _application(detail, step, claim):
    import generator
    request = _request(detail, context.require_context())
    if step["step_type"] == "draft":
        result = generator.generate(
            app_id=request.get("application_id"), opp_id=request.get("opportunity_id"),
            application_type=request.get("application_type"), tone=request["tone"],
            language=request["language"],
        )
        if not result.get("application_id"):
            raise background_worker.TerminalStepError("invalid_output")
        return f"application:{int(result['application_id'])}"
    prior = next((item["result_ref"] for item in detail["steps"]
                  if item["sequence"] < step["sequence"] and item["result_ref"].startswith("application:")), "")
    app_id = int(prior.split(":", 1)[1]) if prior else int(request.get("application_id") or 0)
    if not app_id or not repo.get_application(app_id, with_versions=True):
        raise background_worker.TerminalStepError("invalid_output")
    return f"application:{app_id}"


HANDLERS = {
    "profile_discovery": _discovery,
    "profile_intelligence": _intelligence,
    "resume_generation": _resume,
    "application_draft": _application,
}

# Queue consumers use the worker registry; the local harness passes this same
# mapping explicitly so both execution paths run identical domain handlers.
for _job_type, _handler in HANDLERS.items():
    background_worker.register(_job_type, _handler)


async def run_inline(job_id: str) -> dict:
    """Local/test harness. Production uses the queue adapter, never this loop."""
    actor = background_jobs.actor_for_job(job_id)
    with context.run_as(actor):
        initial = background_jobs.get(job_id, ctx=actor)
        # Each step can be attempted up to three times. Keep this bounded while
        # allowing the largest accepted discovery plan to finish locally.
        max_iterations = min(132, initial["progress_total"] * 3 + 4)
        for _ in range(max_iterations):
            current = background_jobs.get(job_id, ctx=actor)
            if current["status"] in background_jobs.TERMINAL_STATES:
                return current
            for entry in background_jobs.pending_outbox(job_id=job_id):
                background_jobs.mark_outbox_sent(entry["id"], f"inline:{entry['step_sequence']}")
            try:
                await background_worker.process(
                    job_id, handlers=HANDLERS, publish_next=False
                )
            except background_worker.RetryableStepError:
                await asyncio.sleep(0)
        current = background_jobs.get(job_id, ctx=actor)
        if current["status"] not in background_jobs.TERMINAL_STATES:
            claim = background_jobs.claim(job_id, "inline-poison", ctx=actor)
            if claim:
                return background_jobs.fail(claim, "poison_message", ctx=actor)
        return background_jobs.get(job_id, ctx=actor)


def public(job_id: str, ctx=None) -> dict:
    actor = _actor(ctx)
    detail = background_jobs.public_detail(job_id, ctx=actor)
    result = {}
    if detail["job_type"] == "profile_discovery":
        run_ids = [int(step["result_ref"].split(":", 1)[1]) for step in detail["steps"]
                   if step["result_ref"].startswith("search-run:")]
        rows = []
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            with repo.db() as con:
                rows = con.execute(
                    f"""SELECT id,source,status,discovered,new_count,duplicate_count,error
                         FROM search_runs WHERE user_id=? AND id IN ({placeholders}) ORDER BY id""",
                    (actor.user_id, *run_ids),
                ).fetchall()
        result = {
            "runs": [{**dict(row), "error": i18n.ui_text(row["error"])} for row in rows],
            "discovered": sum(row["discovered"] for row in rows),
            "new": sum(row["new_count"] for row in rows),
            "duplicates": sum(row["duplicate_count"] for row in rows),
        }
    elif detail["job_type"] == "resume_generation" and detail["result_ref"].startswith("resume:"):
        language = detail["result_ref"].split(":", 1)[1]
        result = {"language": language, "url": f"/profile/resume?cv_language={language}"}
    elif detail["job_type"] == "application_draft" and detail["result_ref"].startswith("application:"):
        result = {"application_id": int(detail["result_ref"].split(":", 1)[1])}
    elif detail["job_type"] == "profile_intelligence" and detail["status"] == "succeeded":
        import profile_intelligence
        state = profile_intelligence.state()
        result = {"count": len(state.get("concepts", []))}
    detail["result"] = result
    detail["message"] = i18n.translate(PUBLIC_ERRORS.get(detail["error_code"], "")) if detail["error_code"] else ""
    return detail
