"""ProfileContext kanonik (spec §6.2) — model faktesh me dëshmi (Faza 4C).

Profili bëhet të dhënë e strukturuar: seksione JSON-dokumente
(profile_sections) të mbushura nga migrimi legacy ose CRUD-i i UI.
Çdo FAKT ka dëshmi: {key, value, source} ku source = seksioni stor që e mban.

Konsumatorët (gjeneratori 4B+, matching i ardhshëm) lexojnë VETËM këtu —
fushat legacy të tabelës profile mbeten si kopje kompatibiliteti.

Garanci kundër hallucinimit: `fact_values()` kthen VETËM stringje që ekzistojnë
të store — gjeneratori i lejon vetëm pretendime të gjurmueshme këtu.
"""
import repository as repo

CONTEXT_VERSION = 1

# skema minimale e fushave për seksion (validim CRUD)
SECTION_SCHEMAS = {
    "interests": {"required": ["name"], "fields": ["name", "description"]},
    "personal": {"required": [], "fields": ["address", "birth_date", "age", "nationality"]},
    "preferences": {"required": [], "fields": ["desired_work", "availability", "travel", "salary", "transport"]},
    "skills": {"required": ["name"], "fields": ["name", "level"]},
    "languages": {"required": ["name"], "fields": ["name", "level"]},
    "experience": {"required": ["title"], "fields": ["title", "org", "period", "description"]},
    "education": {"required": ["title"], "fields": ["title", "org", "period"]},
    "certifications": {"required": ["name"], "fields": ["name", "org", "year", "details"]},
    "portfolio": {"required": ["title"], "fields": ["title", "url", "description"]},
    "projects": {"required": ["title"], "fields": ["title", "url", "description", "stack"]},
    "local": {"required": [], "fields": [
        "base_location", "postcode", "radius_km", "transport_mode",
        "max_travel_min", "min_hourly_rate", "categories", "available_days", "no_car"]},
    "products": {"required": ["name"], "fields": [
        "name", "type", "short_description", "description", "capabilities",
        "technologies", "industries", "languages", "delivery_model",
        "white_label", "customization_level", "deployment_model", "demo_url",
        "website_url", "repo_url", "status", "pricing_notes", "priority"]},
}


def validate_section(section: str, entries: list) -> tuple[bool, str]:
    schema = SECTION_SCHEMAS.get(section)
    if not schema:
        return False, "seksion i panjohur"
    if not isinstance(entries, list):
        return False, "duhet listë"
    for e in entries:
        if not isinstance(e, dict):
            return False, "çdo zë duhet të jetë objekt"
        for r in schema["required"]:
            v = e.get(r)
            if not isinstance(v, str) or not v.strip():
                return False, f"fusha e detyrueshme mungon: {r}"
    return True, ""


def build_context(profile: dict | None = None) -> dict:
    """ProfileContext kanonik — strukturë + fakte me dëshmi. Pa shpikje."""
    profile = profile if profile is not None else repo.get_profile()
    sections_raw = repo.all_profile_sections()

    def sec(name):
        return sections_raw.get(name, {}).get("data", [])

    skills = sec("skills")
    languages = sec("languages")
    experience = sec("experience")
    education = sec("education")
    certifications = sec("certifications")
    portfolio = sec("portfolio")
    projects = sec("projects")
    products = sec("products")

    # fallback kompatibiliteti: seksion bosh → fusha legacy si zëra minimalë
    if not skills and (profile.get("skills") or []):
        skills = [{"name": s, "level": ""} for s in profile["skills"]]
    if not languages and (profile.get("languages") or []):
        languages = [{"name": str(l), "level": ""} for l in profile["languages"]]
    if not products and (profile.get("products") or []):
        products = [{"name": p, "kind": "", "capabilities": [], "link": ""}
                    for p in profile["products"]]

    import geo as _geo
    local_raw, _local_upd = repo.get_profile_section("local")
    local = dict(local_raw[0]) if local_raw and isinstance(local_raw[0], dict) else {}
    local.setdefault("categories", [])
    local["base_coords"] = _geo.coords_for(local.get("postcode"))

    cv = repo.get_cv()
    cv_info = ({"status": cv["parse_status"], "filename": cv["filename"]}
               if cv else {"status": "none"})

    # 4D: zërat e aprovuar nga CV-ja janë TASHMË të mergeuar te seksionet
    # (në momentin e accept, me provenancë cv:<cv_id>:<sid>) — build_context
    # i lexon aty dhe nuk i shton dyfish.

    context = {
        "version": CONTEXT_VERSION,
        "core": {
            "name": profile.get("name") or "",
            "title": profile.get("title") or "",
            "email": profile.get("email") or "",
            "phone": profile.get("phone") or "",
            "location": profile.get("location") or "",
        },
        "summary": profile.get("summary") or "",
        "experience_text": profile.get("experience") or "",
        "skills": skills,
        "languages": languages,
        "experience": experience,
        "education": education,
        "certifications": certifications,
        "portfolio": portfolio,
        "projects": projects,
        "interests": sec("interests"),
        "personal": sec("personal"),
        "preferences": sec("preferences"),
        "products": products,
        "local": local,
        "cv": cv_info,
        "meta": {"sections_present": sorted(sections_raw)},
    }
    context["facts"] = _facts(context)
    return context


def _facts(ctx: dict) -> list[dict]:
    """Fakte me dëshmi — provenanca: seksioni, ose cv:<cv_id>:<sid> për zërat
    e坟mblrë nga CV (Faza 4D)."""
    f: list[dict] = []

    def add(key, value, source, **extra):
        fact = {"key": key, "value": value, "source": source}
        fact.update(extra)
        f.append(fact)

    for s in ctx["skills"]:
        add(f"skill:{s['name'].lower()}", s["name"],
            s.get("source") or "skills", level=s.get("level", ""))
    for item in ctx.get("interests", []):
        add(f"interest:{item['name'].lower()}", item['name'], item.get('source') or 'interests', kind='interest')
    for item in ctx.get('preferences', []):
        for key in SECTION_SCHEMAS['preferences']['fields']:
            if item.get(key): add('preference:'+key, item[key], item.get('source') or 'preferences', kind='preference')
    for l in ctx["languages"]:
        add(f"language:{l['name'].lower()}", l["name"],
            l.get("source") or "languages", level=l.get("level", ""))
    for p in ctx["products"]:
        add(f"product:{p['name'].lower()}", p["name"],
            p.get("source") or "products", kind=p.get("kind", ""))
    for e in ctx["experience"]:
        add(f"experience:{e['title'].lower()}",
            f"{e['title']} @ {e.get('org', '')}".strip(),
            e.get("source") or "experience")
    for ed in ctx["education"]:
        add(f"education:{ed['title'].lower()}", ed["title"],
            ed.get("source") or "education")
    for c in ctx["certifications"]:
        add(f"certification:{c['name'].lower()}", c["name"],
            c.get("source") or "certifications")
    for p in ctx["portfolio"]:
        add(f"portfolio:{p['title'].lower()}", p["title"],
            p.get("source") or "portfolio")
    for p in ctx["projects"]:
        add(f"project:{p['title'].lower()}", p["title"],
            p.get("source") or "projects")
    if ctx["experience_text"].strip():
        add("experience:summary", ctx["experience_text"], "experience_text")
    if ctx["summary"].strip():
        add("profile:summary", ctx["summary"], "summary")
    # dedup me key — i pari fiton (deterministik)
    seen, out = set(), []
    for fact in f:
        if fact["key"] in seen:
            continue
        seen.add(fact["key"])
        out.append(fact)
    return out


def fact_values(ctx: dict | None = None) -> list[str]:
    """Vlerat e fakteve si stringje lowercase — baza e gardës së gjeneratorit.
    Bosh = s'ka dëshmi (abstain, jo negative)."""
    ctx = ctx or build_context()
    return [f["value"].lower() for f in ctx["facts"] if f.get("value")]
