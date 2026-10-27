"""Shërbimi i aplikimeve/propozimeve (spec §16) — Faza 4A: themeli pa auto-dërgim.

RREGULLAT E PATHYESHME:
- Rishkrimet janë append-only: përmbajtja e një versioni NUK ndryshohet kurrë.
- Regjenerimi KRIJON version të re "generated" — editimet e përdoruesit
  (versionet "edited") dhe final-i mbeten të paprekura.
- Nuk ka asnjë rrugë dërgimi automatik: "submitted" është vetëm shënim i
  bërë nga përdoruesi pasi dërgoi vetë jashtë aplikacionit.
- Draft-et e kësaj faze janë template deterministike — pa AI (4B e shton si
  generator me metadata provider/model).
"""
import repository as repo

TYPES = ("job_application", "freelance_proposal", "product_outreach", "local_message")
STATUSES = ("draft", "edited", "approved", "submitted", "archived")

STATUS_LABELS = {
    "draft": "draft", "edited": "e redaktuar", "approved": "e aprovuar",
    "submitted": "e dërguar", "archived": "arkivuar",
}

KIND_DEFAULT_TYPE = {
    "job": "job_application",
    "freelance": "freelance_proposal",
    "product_lead": "product_outreach",
    "local_job": "local_message",
}

# tranzicione të lejuara: statusi aktual → setStatus i ri
TRANSITIONS = {
    "draft": {"edited", "approved", "archived"},
    "edited": {"edited", "approved", "archived"},
    "approved": {"edited", "submitted", "archived"},
    "submitted": {"archived"},
    "archived": set(),
}


def can_transition(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, set())


# ------------------------------------------------------- template deterministic

def _template(op: dict, profile: dict, app_type: str) -> tuple[str, str]:
    """Kthen (subject, content) — tekst deterministik, pa AI, me hapësira
    të shënuara [PLOTËSO: …] që përdoruesi i personalizexon."""
    title = op.get("title") or "pozicioni"
    org = op.get("org_name") or "Z. / Znj."
    name = profile.get("name") or "[EMRI JOT]"
    skills = profile.get("skills") or []
    skills_txt = "\n".join(f"- {s}" for s in skills[:5]) or "- [PLOTËSO: aftësitë kryesore]"
    loc = op.get("location") or ""
    remote = "remote" if op.get("remote") else loc

    if app_type == "job_application":
        subject = f"Aplikim — {title}"
        content = (
            f"Përshëndetje, team i {org},\n\n"
            f"me interes po aplikoj për pozicionin \"{title}\""
            f"{f' ({remote})' if remote else ''}.\n\n"
            "Përputhja ime me role-in:\n"
            f"{skills_txt}\n\n"
            f"[PLOTËSO: 2–3 fjali eksperiencë konkrete më e relevanta]\n\n"
            "Brenda skedarit të bashkangjitur gjeni CV-të e plotë.\n\n"
            f"Me respekt,\n{name}"
            + (f"\n{profile.get('email')}" if profile.get("email") else "")
        )
        return subject, content

    if app_type == "freelance_proposal":
        subject = f"Propozim — {title}"
        budget = op.get("salary_display") or "[PLOTËSO: buxheti]"
        content = (
            f"Përshëndetje,\n\n"
            f"lidhur me projektin \"{title}\" ({org}):\n\n"
            "1. Kuptimi i nevojës\n"
            f"[PLOTËSO: 1–2 fjali për problemin që zgjidh projekti]\n\n"
            "2. Çfarë do të dërgoj\n"
            f"{skills_txt}\n\n"
            "3. Afati & buxheti\n"
            f"   Afati: [PLOTËSO: javë]\n   Buxheti i referuar: {budget}\n\n"
            f"Rrezikun kryesor e shoh te [PLOTËSO: rreziku] — mbulohet me [PLOTËSO: qasje].\n\n"
            f"Mirupafshim,\n{name}"
        )
        return subject, content

    if app_type == "product_outreach":
        products = profile.get("products") or ["[PLOTËSO: produkti]"]
        product = products[0]
        subject = f"{product} — mundësi për {org}"
        content = (
            f"Përshëndetje, team i {org},\n\n"
            f"pashë nevojën tuaj për \"{title}\". Ne tashmë kemi një zgjidhje "
            f"të gatshme në këtë fushë: {product}.\n\n"
            "Çfarë mbulon tashmë:\n"
            f"{skills_txt}\n\n"
            "[PLOTËSO: 1–2 fjali përshtatje specifike për rastin e tyre]\n\n"
            f"Ndoshta vlen 15 min bisedë? Arrijmë në {profile.get('email') or '[email]'}.\n\n"
            f"{name}"
        )
        return subject, content

    # local_message
    subject = f"{title} — interes"
    content = (
        f"Përshëndetje,\n\n"
        f"jam i interesuar për \"{title}\". Jam nga zona ({profile.get('location') or '[zonja]'}) "
        "dhe mund të vij sipas orarit që ju përshtat.\n\n"
        "[PLOTËSO: 1 fjali për vete]\n\n"
        f"{name}"
        + (f"\n{profile.get('phone')}" if profile.get("phone") else "")
    )
    return subject, content


# ----------------------------------------------------------- operacionet

def create_draft(op: dict, app_type: str | None = None, platform: str = "",
                 language: str = "sq", regenerate_of: int | None = None,
                 profile: dict | None = None) -> int:
    """Krijo draft të re OSE version të re 'generated' për aplikim ekzistues
    (regjenerim). Editimet e vjetra NUK preken kurrë."""
    profile = profile if profile is not None else repo.get_profile()
    app_type = app_type or KIND_DEFAULT_TYPE.get(op.get("kind"), "job_application")
    if app_type not in TYPES:
        raise ValueError(f"application_type i panjohur: {app_type}")
    subject, content = _template(op, profile, app_type)
    generator = {"kind": "template", "template_version": 1}

    if regenerate_of:
        app = repo.get_application(regenerate_of)
        if not app or app["opportunity_id"] != op["id"]:
            raise ValueError("aplikimi për regjenerim nuk u gjet / nuk përket")
        ver = repo.add_application_version(regenerate_of, "generated", content)
        repo.set_application_fields(regenerate_of, {
            "status": "edited" if app["status"] == "approved" else "draft",
            "subject": subject,
            "generator": {**app["generator"], **generator, "regenerated_at_version": ver},
        })
        return regenerate_of

    return repo.create_application(op["id"], app_type, platform, language,
                                   subject, content, generator)


def edit_draft(app_id: int, content: str, subject: str | None = None) -> dict:
    """Krijon version të re 'edited' me përmbajtjen e përdoruesit.
    Nëse përmbajtja është njësoj me të fundit, s'krijoj version të panevojshëm."""
    app = repo.get_application(app_id, with_versions=True)
    if not app:
        raise LookupError("nuk u gjet")
    if app["status"] not in ("draft", "edited", "approved"):
        raise PermissionError(f"editim i pavlefshëm nga statusi '{app['status']}'")
    if not can_transition(app["status"], "edited"):
        raise PermissionError("tranzicion i pavlefshëm")
    latest = app.get("latest_content")
    if latest == content:
        return app  # asgjë e re — s'krijojmë version dublikant
    ver = repo.add_application_version(app_id, "edited", content)
    fields = {"status": "edited"}
    if subject is not None:
        fields["subject"] = subject
    repo.set_application_fields(app_id, fields)
    return repo.get_application(app_id, with_versions=True)


def set_status(app_id: int, new: str) -> dict:
    app = repo.get_application(app_id, with_versions=True)
    if not app:
        raise LookupError("nuk u gjet")
    current = app["status"]
    if not can_transition(current, new):
        raise PermissionError(f"tranzicion i pavlefshëm: {current} → {new}")

    fields = {"status": new}
    if new == "approved":
        # final-i vuloset si kopje e përmbajtjes së fundit (e pandryshueshme)
        repo.add_application_version(app_id, "final", app["latest_content"])
        fields["approved_at"] = repo.now()
    elif new == "submitted":
        fields["submitted_at"] = repo.now()  # vetëm shënim — dërgimi është bërë jashtë
    repo.set_application_fields(app_id, fields)
    return repo.get_application(app_id, with_versions=True)


def application_panel_data(opp_id: int) -> list[dict]:
    """Të dhënat kompakte për drawer-in: aplikimet + përmbajtja e fundit + generator."""
    out = []
    for app in repo.list_applications(opp_id):
        full = repo.get_application(app["id"], with_versions=True)
        app["latest_content"] = full["latest_content"]
        app["latest_kind"] = full["latest_kind"]
        app["generator"] = full.get("generator") or {}
        app["versions"] = [
            {"version_number": v["version_number"], "kind": v["kind"],
             "created_at": v["created_at"]}
            for v in full["versions"]
        ]
        app["status_label"] = STATUS_LABELS.get(app["status"], app["status"])
        out.append(app)
    return out
