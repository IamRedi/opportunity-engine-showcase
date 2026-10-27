"""Gjeneratori i aplikimeve sipas platformës (spec §16) — Faza 4B.

Shtohet MBI modelin e rishkrimeve 4A dhe shtresën e furnizuesve 3B:
- Thirrje vetëm me kërkesë të përdoruesit (POST); default disabled → fallback
  template deterministic (asnjëherë s'thyn drawer-in).
- Output-i i AI-së i kalon gardat deterministiche:
  1. schema-validim (subject/body/facts/missing_context),
  2. gjurmueshmëria e pretendimeve: çdo `used_profile_facts` duhet të gjendet
     në profil (aftësi/gjuhë/produkte/eksperiencë/përmbledhje/titull);
     pretendim i pambështetur → HUM dështon gjenerimi → fallback template.
     Kjo kontrollon referencat e deklaruara, jo çdo pretendim brenda trupit.
  3. missing_context = vlerësim deterministic i profile-gaps + lista e AI-së.
- Rezultati: version i re "generated" mbi aplikimin ekzistues (ose aplikim i
  re), me metadata provider/model/prompt_version/style/tone/fallback.
- Pa dërgim automatik: asnjë rrugë submit gjatë gjenerimit.
"""
import re

import ai_service
import applications as apps_svc
import profile_context
import repository as repo

STYLE_INSTRUCTIONS = {    "application": (
        "Traditional job application: professional letter, greeting, why-fit with "
        "evidence from profile only, close politely. 150–250 words."
    ),
    "linkedin": (
        "LinkedIn-style outreach: short professional message, 60–100 words, "
        "one hook line referencing the role, 2–3 profile facts, polite ask."
    ),
    "kleinanzeigen": (
        "Classifieds/local platform style: direct, human, concise. 40–80 words, "
        "no corporate language, state availability plainly."
    ),
    "freelance": (
        "Freelance marketplace proposal: project-focused. Structure: understanding "
        "of the need → relevant evidence from profile → deliverables → timeline "
        "placeholder [PLOTËSO] → budget placeholder [PLOTËSO]. 120–200 words."
    ),
    "product": (
        "Product-lead commercial outreach: focus on the CLIENT's problem, present "
        "the user's product as ready solution, name concrete gaps it covers, ask "
        "for a short call. 80–150 words."
    ),
    "local": (
        "Local/mini-job message: short, friendly, practical. 30–60 words: interest, "
        "proximity, availability question."
    ),
}

TYPE_DEFAULT_STYLE = {
    "job_application": "application",
    "freelance_proposal": "freelance",
    "product_outreach": "product",
    "local_message": "local",
}

TONES = ("professional", "concise", "friendly")
LANGUAGES = ("sq", "en", "de")

# E matur në 3D: me profil pa aftësi, modeli i atribuon kandidatit kërkesat e
# listimit (hallucinim body-level). Prag minimal dëshmie për gjenerim me AI.
MIN_PROFILE_SKILLS_GENERATION = 3


# ------------------------------------------------- konteksti & gardat

def missing_context(profile: dict, op: dict, app_type: str) -> list[str]:
    """Vlerësim DETERMINISTIK i boshllëqeve — gjithmonë i përfshirë në rezultat."""
    m = []
    if not (profile.get("skills") or []):
        m.append("aftësitë e profilit janë bosh")
    if not (profile.get("experience") or "").strip():
        m.append("eksperienca nuk është plotësuar")
    if not (profile.get("languages") or []):
        m.append("gjuhët nuk janë plotësuar")
    if app_type == "product_outreach" and not (profile.get("products") or []):
        m.append("katalogu i produkteve është bosh")
    if not profile.get("location"):
        m.append("vendndodhja e profilit mungon")
    if len((op.get("description") or "").strip()) < 200:
        m.append("përshkrimi i mundësisë është shumë i shkurtër")
    return m


def _known_facts(profile: dict) -> list[str]:
    """Fakte të vërteta — tani nga ProfileContext (dëshmi me seksion burim, 4C).
    Rritet me tekst i lirë (eksperiencë/përmbledhje) që AI të citojë nga atje."""
    values = profile_context.fact_values(profile_context.build_context(profile))
    # Include the literal facts actually sent to the provider, including job title.
    for value in ai_service._profile_content(profile).values():
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, str) and entry.strip():
                values.append(entry.lower())
    # shto edhe tekstet e plota si "containere" (fact-in e plotë e kemi te context)
    if profile.get("experience"):
        values.append(profile["experience"].lower())
    if profile.get("summary"):
        values.append(profile["summary"].lower())
    # Location is supplied to the model too; an existing profile location is evidence.
    if profile.get("location"):
        values.append(profile["location"].lower())
    if profile.get("name"):
        values.append(profile["name"].lower())
    return values


def _fact_supported(fact: str, known: list[str]) -> bool:
    f = fact.lower().strip()
    f = re.sub(r"^(?:name|title|skills?|languages?|products?|location|experience|summary)\s*:\s*", "", f)
    if not f:
        return False
    return any(f == k.strip() for k in known if k.strip())


def _op_facts(op: dict) -> list[str]:
    """Fakte të mundësisë — referimi i tyre NUK është hallucinim mbi përdoruesin."""
    facts = [op.get("title") or "", op.get("org_name") or "", op.get("location") or ""]
    if op.get("remote"):
        facts.append("remote")
    facts += [str(t).lower() for t in (op.get("tags") or [])]
    if op.get("opportunity_type"):
        facts.append(op["opportunity_type"].lower())
    return [f.lower().strip() for f in facts if f and len(f.strip()) > 2]


def _claims_supported(data: dict, profile: dict, op: dict) -> tuple[bool, list[str]]:
    """Referencat e profilit duhet të vijnë vetëm nga profili, jo nga kërkesat e punës."""
    known = _known_facts(profile)
    unsupported = [f for f in (data.get("used_profile_facts") or [])
                   if isinstance(f, str) and f.strip() and not _fact_supported(f, known)]
    return (not unsupported), unsupported


# ------------------------------------------------------- gjenerimi

def generate(app_id: int | None = None, opp_id: int | None = None,
             application_type: str | None = None, style: str | None = None,
             tone: str = "professional", language: str = "sq") -> dict:
    """Gjenero draft me AI (fallback template). Manual-only. Kurrë s'hedh exception.

    Kthen {ok, application_id, version, fallback, reason?, missing_context[],
           used_profile_facts[], unsupported_claims[], meta{}}."""
    outcome = {"ok": False, "application_id": app_id, "version": None,
               "fallback": False, "reason": "", "missing_context": [],
               "used_profile_facts": [], "unsupported_claims": [], "meta": {},
               "language": language}
    try:
        if app_id:
            app = repo.get_application(app_id)
            if not app:
                # IDOR: aplikim i huaj/panjohur → asnjë shkrim, pa ID në përgjigje
                outcome["application_id"] = None
                outcome["reason"] = "aplikimi nuk u gjet"
                return outcome
            opp_id = app["opportunity_id"]
            application_type = application_type or app["application_type"]
            platform = app.get("platform", "")
        else:
            platform = ""
        op = repo.get_opportunity(opp_id) if opp_id else None
        if not op:
            outcome["reason"] = "mundësia nuk u gjet"
            return outcome

        app_type = application_type or apps_svc.KIND_DEFAULT_TYPE.get(op["kind"], "job_application")
        if app_type not in apps_svc.TYPES:
            outcome["reason"] = f"lloj i panjohur: {app_type}"
            return outcome
        style = style or TYPE_DEFAULT_STYLE.get(app_type, "application")
        if style not in STYLE_INSTRUCTIONS:
            style = "application"
        if tone not in TONES:
            tone = "professional"
        if language not in LANGUAGES:
            language = "sq"

        profile = repo.get_profile()
        missing = missing_context(profile, op, app_type)
        outcome["missing_context"] = missing

        # porta e plotësisë së profilit (dëshmi 3D) — lexon ProfileContext
        # (seksionet strukturore, me fallback fushat legacy)
        ctx = profile_context.build_context(profile)
        profile = dict(profile)
        profile['education'] = [' | '.join(str(e.get(k) or '') for k in ('title','org','period')).strip(' |') for e in ctx['education']]
        profile['certifications'] = [' | '.join(str(e.get(k) or '') for k in ('name','details','org','year')).strip(' |') for e in ctx['certifications']]
        profile['interests_not_experience'] = [e['name'] for e in ctx.get('interests',[])]
        # get_profile also includes current local/search settings; do not replace them with a partial copy.
        profile['stated_preferences'] = profile.get('stated_preferences',[])
        n_skills = len(ctx["skills"])
        if n_skills < MIN_PROFILE_SKILLS_GENERATION:
            return _fallback(outcome, op, profile, app_type, platform,
                             f"profil i pamjaftueshëm ({n_skills} aftësi < "
                             f"{MIN_PROFILE_SKILLS_GENERATION}) — AI mund të shpikë sfondin")

        cfg = ai_service.get_config()
        if cfg["provider"] == "disabled" or not cfg["model"]:
            return _fallback(outcome, op, profile, app_type, platform,
                             "AI është çaktivizuar — u përdor template deterministic")

        extra = {"style": STYLE_INSTRUCTIONS[style] + ' Hobbies are interests, never professional experience. Do not invent availability, car access, recognized qualifications or proficiency. Use stated preferences literally.', "tone": tone,
                 "language": language, "platform": platform,
                 "missing_context": missing}
        out = ai_service.run_task("draft_application", op, profile=profile,
                                  force=True, cfg=cfg, extra=extra)
        if not out.ok:
            return _fallback(outcome, op, profile, app_type, platform,
                             f"AI dështoi ({out.error}) — template deterministic")

        data = out.data or {}
        body = (data.get("body") or "").strip()
        if len(body) < 40:
            return _fallback(outcome, op, profile, app_type, platform,
                             "trupi i shkurtër/gabim — template deterministic")
        supported, unsupported = _claims_supported(data, profile, op)
        outcome["unsupported_claims"] = unsupported
        if not supported:
            # pretendimet e shpikura NUK persistohen → fallback template
            return _fallback(outcome, op, profile, app_type, platform,
                             "pretendime të pambështuarta nga profili — template deterministic")

        ai_missing = [m for m in (data.get("missing_context") or []) if isinstance(m, str)]
        outcome["missing_context"] = sorted(set(missing) | set(ai_missing))
        outcome["used_profile_facts"] = [f for f in (data.get("used_profile_facts") or [])
                                         if isinstance(f, str)]

        subject = (data.get("subject") or "").strip() or f"Aplikim — {op['title']}"
        gen_meta = {
            "kind": "ai", "provider": out.meta.get("provider"),
            "model": out.meta.get("model"),
            "prompt_version": out.meta.get("prompt_version"),
            "style": style, "tone": data.get("tone") or tone,
            "platform_style": data.get("platform_style") or style,
            "language": language,
            "cached": out.cached,
        }
        ver, app_id = _persist(op, app_id, app_type, platform, language,
                               subject, body, gen_meta)
        outcome.update(ok=True, application_id=app_id, version=ver, meta=gen_meta)
        return outcome
    except Exception as e:  # fail-soft absolut
        try:
            op = repo.get_opportunity(opp_id) if opp_id else None
            if op:
                return _fallback(outcome, op, repo.get_profile(), 
                                 application_type or "job_application", "",
                                 f"e papritur: {e}")
        except Exception:
            pass
        outcome["reason"] = f"e papritur: {e}"
        return outcome


def _fallback(outcome: dict, op: dict, profile: dict, app_type: str,
              platform: str, reason: str) -> dict:
    """Template deterministic — rruga e sigurt, e njëjta si 4A."""
    subject, content = apps_svc._template(op, profile, app_type)
    gen_meta = {"kind": "template", "template_version": 1, "fallback_reason": reason}
    ver, app_id = _persist(op, outcome.get("application_id"), app_type, platform,
                           outcome.get("language") or "sq",
                           subject, content, gen_meta)
    outcome.update(ok=True, application_id=app_id, version=ver, fallback=True,
                   reason=reason, meta=gen_meta)
    return outcome


def _persist(op: dict, app_id: int | None, app_type: str, platform: str,
             language: str, subject: str, content: str, gen_meta: dict) -> tuple[int, int]:
    """Krijon version të re 'generated' — kurrë mbivendosje; historia immutable."""
    if app_id:
        ver = repo.add_application_version(app_id, "generated", content)
        app = repo.get_application(app_id)
        repo.set_application_fields(app_id, {
            "subject": subject,
            "generator": {**app["generator"], **gen_meta},
        })
        return ver, app_id
    new_id = repo.create_application(op["id"], app_type, platform, language,
                                     subject, content, gen_meta)
    return 1, new_id
