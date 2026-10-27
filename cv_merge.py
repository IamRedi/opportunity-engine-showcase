"""Merge i sugjerimeve të CV-së (Faza 4D) — kurrë mbivendosje e të dhënave.

RREGULLAT (spec 4D):
- Pending NUK prek asgjë.
- Accept → zë i re/plotësim në seksionin strukturuar, me provenancë
  `cv:<cv_id>:<sugg_id>`; exact-dup s'krijohej; idempotent.
- Manual data fiton: zëri me të njëjtin emër (case-insensitive) ekzistues
  NUK mbivendoset — sugjerimi shënohet accepted pa efekt (skip-duplicate).
- Reject → pa efekt; mbetet i regjistruar (inaktiv).
- Rikonvertimi: sugjerime me të njëjtin (section, field, normalized) që
  ekzistojnë (në çdo status) nuk riparqen.
"""
import json

import cv_parser
import repository as repo
import ai_requests
from cv_identity import identity as entry_identity


def _sections():
    data = repo.all_profile_sections()
    return {k: v.get("data", []) for k, v in data.items()}


def _norm(v: str) -> str:
    import re
    return re.sub(r"\s+", " ", (v or "").lower()).strip()


def _existing_names(entries: list[dict], field: str) -> set:
    return {_norm(e.get(field, "")) for e in entries}


# --------------------------------------------------------------- parse

@ai_requests.serialized('cv-parse')
def parse_cv(cv_id: int, force: bool = False, use_ai: bool = False) -> dict:
    """Ekstrakto + parso CV-në → sugjerime pending (pa duplikate të vjetra).
    Riparsimi: propozimet ekzistuese (çdo status) nuk riparqen.
    IZOLIM (8C2): leximi/përditësimi i CV-së kalon përmes repository me
    pronësi — CV i një user-i tjetër nuk gjendet kurrë."""
    cv = repo.get_cv_by_id(cv_id)
    if not cv:
        return {"ok": False, "reason": "CV nuk u gjet"}
    import cv_ai
    expected=cv_parser.PARSER_VERSION+('+'+cv_ai.VERSION if use_ai else '')
    receipt_key=ai_requests.cache_key('cv-parse',cv_parser.PARSER_VERSION,[cv_id,cv['sha256'],cv_ai.VERSION],{})
    if cv.get('extract_status')=='ok' and cv.get('parser_version') in (expected,cv_parser.PARSER_VERSION+'+'+cv_ai.VERSION) and not force:
        items=repo.cv_suggestions(cv_id)
        receipt=repo.ai_cache_get(receipt_key)
        candidates=set(receipt['output'].get('candidate_ids',[])) if receipt else set()
        return {'ok':True,'cached':True,'created':0,'skipped_existing':len(items),
                'candidate_ids':[s['id'] for s in items if s['id'] in candidates],'extract_status':'ok',
                'parser_version':cv['parser_version'],'warning':''}
    path = cv["stored_path"]
    extraction = cv_parser.extract_text(__import__("pathlib").Path(path))
    parser_version = cv_parser.PARSER_VERSION
    parse_status = "parsed" if extraction["status"] == "ok" else extraction["status"]

    repo.update_cv_extract(cv_id, {
        "extract_status": extraction["status"], "extract_method": extraction["method"],
        "parser_version": parser_version,
        "extracted_text": extraction["text"][:200000],
        "extracted_at": repo.now(), "extract_error": extraction["error"],
        "parse_status": parse_status,
    })

    if extraction["status"] != "ok":
        return {"ok": False, "reason": extraction["error"],
                "extract_status": extraction["status"]}

    suggestions = cv_parser.parse_suggestions(extraction["text"])
    ai_warning = ''
    if use_ai:
        import cv_ai
        ai_text = extraction['text']
        if extraction.get('layout_text'):
            ai_text += '\n\nVISUAL COLUMN ALIGNMENT (same CV, use to associate dates and roles):\n' + extraction['layout_text']
        repo.update_cv_extract(cv_id, {'extracted_text': ai_text[:200000]})
        ai_result = cv_ai.extract(ai_text)
        if ai_result.get('busy'):
            return ai_result
        if ai_result['ok']:
            # AI entries preserve separate organization/date/description fields.
            # Keep local suggestions only for sections absent from the AI result.
            def identity(s):
                if s['section'] == 'contact':
                    return (s['section'], s['field'])
                if s['section'] in ('skills', 'languages'):
                    entry = repo._entry_for_cv_section({**s, 'cv_id': cv_id, 'id': 0})
                    return (s['section'], entry_identity(s['section'], entry.get('name', '')))
                return (s['section'], '')
            covered = {identity(s) for s in ai_result['suggestions']}
            suggestions = ai_result['suggestions'] + [s for s in suggestions if identity(s) not in covered]
            parser_version += '+' + cv_ai.VERSION
            repo.update_cv_extract(cv_id, {'parser_version': parser_version})
            if ai_result['rejected']:
                ai_warning = 'Disa të dhëna pa dëshmi të vlefshme u përjashtuan.'
            if ai_result.get('warning'):
                ai_warning = ai_result['warning']
        else:
            ai_warning = ai_result['reason']
    if not repo.get_cv_by_id(cv_id):
        return {'ok': False, 'reason': 'CV nuk u gjet'}
    existing = {}
    for s in repo.cv_suggestions(cv_id):
        existing[(s["section"], s["field"], s["normalized_value"])] = s["status"]

    created = skipped = 0
    for s in suggestions:
        key = (s["section"], s["field"], s["normalized_value"])
        if key in existing:
            skipped += 1  # i njëjti propozim ekziston (çdo status) → pa dyfish
            continue
        source_ref = f"cv:{cv_id}"
        nid = repo.create_cv_suggestion(
            cv_id, s["section"], s["field"], s["proposed_value"],
            s["normalized_value"], s["evidence"], s["confidence"], source_ref)
        if nid:
            created += 1
        else:
            skipped += 1
    candidate_keys = {(s['section'], s['field'], s['normalized_value']) for s in suggestions}
    candidate_ids = [s['id'] for s in repo.cv_suggestions(cv_id) if (s['section'], s['field'], s['normalized_value']) in candidate_keys]
    repo.ai_cache_put(receipt_key,'cv-parse','local','parser',parser_version,{'candidate_ids':candidate_ids},{})
    return {"ok": True, "created": created, "skipped_existing": skipped,
            "extract_status": extraction["status"], "parser_version": parser_version, 'warning': ai_warning, 'candidate_ids': candidate_ids}


# --------------------------------------------------------------- merge

_MERGE_TARGETS = {
    "interests": ("interests", "name"),
    "skills": ("skills", "name"),
    "languages": ("languages", "name"),
    "experience": ("experience", "title"),
    "education": ("education", "title"),
    "certifications": ("certifications", "name"),
    "portfolio": ("portfolio", "title"),
    "projects": ("projects", "title"),
}


def accept_suggestion(sid: int) -> dict:
    """Apro sugjerimin → merge i sigurt në seksionin strukturuar.
    Idempotent: aprovoj dy herë → i njëjti gjendje."""
    sugg = repo.get_cv_suggestion(sid)
    if not sugg:
        return {"ok": False, "reason": "nuk u gjet"}
    if sugg["status"] == "accepted":
        return {"ok": True, "merged": False, "reason": "tashmë e aprovuar (idempotent)"}
    if sugg["status"] == "rejected":
        repo.set_cv_suggestion_status(sid, "rejected")  # mbetet inaktiv
        return {"ok": True, "merged": False, "reason": "e refuzuar më parë — mbetet inaktiv"}

    section = sugg["section"]
    value = sugg["proposed_value"].strip()
    provenance = f"cv:{sugg['cv_id']}:{sid}"

    if section in ("contact", "summary"):
        merged = _merge_contact_or_summary(section, sugg["field"], value)
        repo.set_cv_suggestion_status(sid, 'accepted')
        return {'ok': True, 'merged': merged}
    elif section in ('personal', 'preferences'):
        return repo.autofill_cv_suggestions(sugg['cv_id'], candidate_ids=[sid])
    elif section in _MERGE_TARGETS:
        target_section, field = _MERGE_TARGETS[section]
        entries, _ = repo.get_profile_section(target_section)
        entry = _entry_for(section, sugg, value, provenance)
        names = {entry_identity(section, e.get(field, '')) for e in entries}
        if entry_identity(section, entry.get(field, value)) in names:
            # manual data fiton — asnjë mbivendosje
            repo.set_cv_suggestion_status(sid, "accepted")
            return {"ok": True, "merged": False,
                    "reason": "ekziston tashmë në profil (manual fiton)"}
        entries.append(entry)
        repo.set_profile_section(target_section, entries)
    else:
        repo.set_cv_suggestion_status(sid, "accepted")
        return {"ok": True, "merged": False, "reason": f"seksion {section} pa merge-target"}

    repo.set_cv_suggestion_status(sid, "accepted")
    return {"ok": True, "merged": True}


def _merge_contact_or_summary(section: str, field: str, value: str) -> bool:
    """Kontakt/summary: plotësoj VETËM fusha bosh të profilit të aktorit —
    manual data fiton (8C2: pa id=1 hardcoded)."""
    prof = repo.get_profile()
    fields = {}
    if section == "contact" and field == "email" and not prof.get("email"):
        fields["email"] = value
    elif section == "contact" and field == "phone" and not prof.get("phone"):
        fields["phone"] = value
    elif section == "summary" and not (prof.get("summary") or "").strip():
        fields["summary"] = value
    elif section == 'contact' and field in ('name', 'title', 'location') and not prof.get(field):
        fields[field] = value
    if fields:
        repo.update_profile_fields(fields)
    return bool(fields)


def _entry_for(section: str, sugg: dict, value: str, provenance: str) -> dict:
    """Zë i strukturuar — I NJËJTI format si repository._entry_for_cv_section."""
    import repository as repo
    fake = {"section": section, "proposed_value": value, 'field': sugg.get('field'),
            "cv_id": sugg.get("cv_id") or 0, "id": sugg.get("id") or 0}
    return repo._entry_for_cv_section(fake)


def reject_suggestion(sid: int) -> dict:
    """Refuzo → status rejected, profili i pandryshuar. Idempotent."""
    sugg = repo.get_cv_suggestion(sid)
    if not sugg:
        return {"ok": False, "reason": "nuk u gjet"}
    repo.set_cv_suggestion_status(sid, "rejected")
    return {"ok": True, "merged": False}
