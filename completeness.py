"""Completeness e profilit sipas mënyrës së kërkimit (Faza 6) — deterministic.

Çdo dimension ka peshë + status ok|partial|missing. Skori = Σ(w·s)/Σw mbi
të gjitha dimensionet e mënyrës; top_missing renditet sipas peshës.
"""
import repository as repo

DIMENSIONS = {
    "job": [
        ("roles", 3, "Titulli/roli i synuar"),
        ("skills", 5, "Të paktën 3 aftësi"),
        ("languages_level", 3, "Gjuhët me nivele"),
        ("experience", 4, "Eksperienca (tekst ose zëra)"),
        ("location_remote", 2, "Vendndodhja/remote"),
        ("salary", 2, "Paga minimale"),
        ("education", 1, "Edukimi"),
        ("certifications", 1, "Certifikimet"),
        ("cv", 2, "CV e ngarkuar"),
    ],
    "freelance": [
        ("skills", 5, "Aftësi/teknologji"),
        ("products", 4, "Produkte/afërsi zgjidhjesh"),
        ("portfolio", 4, "Portfolio/projekte"),
        ("rate", 3, "Tarifa orare/buxheti"),
        ("experience", 3, "Eksperienca"),
        ("languages_level", 2, "Gjuhët"),
    ],
    "product_lead": [
        ("products", 6, "Katalogu i produkteve me aftësi"),
        ("industries", 2, "Industritë e synuara"),
        ("summary", 3, "Përmbledhje komerciale"),
        ("skills", 4, "Aftësitë kryesore"),
    ],
    "local_job": [
        ("local_geo", 6, "Baza + kodi postar/rrezja"),
        ("local_transport", 3, "Transporti/no-car"),
        ("local_rate", 3, "Paga minimale €/orë"),
        ("local_categories", 4, "Kategoritë e preferuara"),
        ("availability", 2, "Disponueshmëria"),
    ],
}


def _entries(section: str) -> list[dict]:
    data, _ = repo.get_profile_section(section)
    return [e for e in data if isinstance(e, dict) and (e.get("name") or e.get("title"))]


def compute(mode: str = "job") -> dict:
    profile = repo.get_profile()
    prefs = repo.get_local_prefs()
    cv = repo.get_cv()

    have = {
        "roles": bool(profile.get("title")),
        "skills": len(_entries("skills")) >= 3,
        "languages_level": any(e.get("level") for e in _entries("languages")),
        "experience": bool((profile.get("experience") or "").strip()) or len(_entries("experience")) > 0,
        "location_remote": bool(profile.get("location")),
        "salary": bool(repo.get_filters().get("salary_min")),
        "education": len(_entries("education")) > 0,
        "certifications": len(_entries("certifications")) > 0,
        "cv": bool(cv),
        "products": any(len(p.get("capabilities") or []) > 0 for p in _entries("products")),
        "portfolio": len(_entries("portfolio")) > 0 or len(_entries("projects")) > 0,
        "rate": bool(prefs.get("min_hourly_rate")),
        "local_geo": bool(prefs.get("base_location") or prefs.get("postcode")),
        "local_transport": bool(prefs.get("transport_mode")),
        "local_categories": bool(prefs.get("categories")),
        "availability": bool(prefs.get("available_days")),
        "industries": any(p.get('industries') for p in _entries('products')),
        "summary": bool((profile.get('summary') or '').strip()),
    }

    dims = DIMENSIONS.get(mode if mode in DIMENSIONS else "job", DIMENSIONS["job"])
    out, total_w, got = [], 0, 0.0
    for key, weight, label in dims:
        okk = have.get(key, False)
        status = "ok" if okk else "missing"
        out.append({"key": key, "label": label, "weight": weight, "status": status})
        total_w += weight
        if okk:
            got += weight

    missing = sorted((d for d in out if d["status"] == "missing"),
                     key=lambda d: -d["weight"])
    score = round(got / total_w * 100) if total_w else 0
    return {"mode": mode, "score": min(100, score), "dimensions": out,
            "missing": [{"key": m["key"], "label": m["label"], "weight": m["weight"]}
                        for m in missing]}


def top_missing(mode: str = "job", n: int = 3) -> list[str]:
    return [m["label"] for m in compute(mode)["missing"][:n]]
