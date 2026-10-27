"""Sugjerime deterministic për profilin (Faza 6) — propozime, jo fakte.

Rregullat janë të thjeshta e të shpjegueshme; AI mund të shtojë burime të reja
më vonë përmes të njëjtës kontratë ( lista {section, kind, value, reason} ).
"""
ROLE_SKILLS = {
    "software developer": ("python", "git", "sql", "rest apis", "testing"),
    "backend developer": ("python", "sql", "rest apis", "docker", "queues"),
    "frontend developer": ("javascript", "react", "css", "html", "testing"),
    "data analyst": ("sql", "excel", "python", "dashboards", "statistics"),
    "devops engineer": ("docker", "kubernetes", "ci/cd", "terraform", "linux"),
    "ai engineer": ("python", "machine learning", "llm", "data pipelines"),
}
ROLE_CERTS = {
    "software developer": ("AWS Certified Developer", "Scrum Fundamentals"),
    "devops engineer": ("AWS Solutions Architect", "CKA"),
    "data analyst": ("Google Data Analytics",),
}
PROJECT_CATEGORIES = ("website", "booking", "automation", "internal tool",
                      "saas", "integration")


def suggest(profile: dict, gaps: list[dict] | None = None) -> list[dict]:
    """Kthen propozime {section, kind, value, reason} — DETERMINISTIK."""
    out: list[dict] = []
    role = (profile.get("title") or "").lower()
    skills_now = {s.lower() for s in (profile.get("skills") or [])}
    role_key = next((r for r in ROLE_SKILLS if r in role), None)

    if role_key:
        for sk in ROLE_SKILLS[role_key]:
            if sk not in skills_now:
                out.append({"section": "skills", "kind": "skill", "value": sk.title(),
                            "reason": f"tipare për rolin {role_key}"})
        for cert in ROLE_CERTS.get(role_key, ()):
            have = any(cert.split()[0].lower() in str(c).lower()
                       for c in (profile.get("certifications") or []))
            if not have:
                out.append({"section": "certifications", "kind": "certification",
                            "value": cert, "reason": f"certifikatë e zakonshme për {role_key}"})

    # TECHNOLOGJI/trends të përbashkëta për developerë (nëse roli s'i mbulon)
    for tech in ("docker", "rest apis", "git"):
        if tech not in skills_now and role_key:
            out.append({"section": "skills", "kind": "technology", "value": tech.title(),
                        "reason": "teknologji kërkuar shpesh"})
            break

    # nga gaps aktive → propozo target-state si aftësi për t'u mësuar
    for g in (gaps or []):
        if g.get("active") and g.get("willingness") in ("learning", "willing_to_learn"):
            out.append({"section": "skills", "kind": "skill",
                        "value": (g.get("target_state") or g.get("label") or "").title(),
                        "reason": "nga gaps: po mësohet"})
    return out[:12]
