"""Product Fit deterministik (spec §5.3, Faza 5B) — pa AI, pa shpikje.

Hyrja: sinjalet e mundësisë (classifier.detect_signals) + teksti i saj, dhe
katalogu I STRUKTURAR i produkteve (profile_sections 'products', Faza 4C).

Kontrata e kandidatit:
    {product_name, fit_score 0–100 | None, matched[], missing[], unknown[],
     evidence[], customization_level, hard_mismatch, reasons[]}

Rregulla:
- Vetëm dëshmi: kategorritë e nevojave vijnë nga teksti i mundësisë;
  aftësitë e produktit VETËM nga lista e deklaruar `capabilities` —
  asgjë e shpikur.
- UNKNOWN s'llogaritet negative: pa sinjale nevoje → abstain (fit None).
- hard_mismatch = ka nevoja të detektuara por produkti s'mbulon asnjë.
- Deterministik: e njëjta hyrje → i njëjti rezultat.
"""
import repository as repo


# kategoria → fjalë-kyçe (për të dyja anët: nevojat e mundësisë dhe aftësitë
# e deklaruara të produktit). Konfigurueshme (spec §5B).
CAPABILITY_CATEGORIES = {
    "website": ("website", "web site", "web", "landing", "portal"),
    "booking": ("booking", "appointment", "rezervim", "scheduling"),
    "automation": ("automation", "automate", "automatizim"),
    "ai": ("ai", "chatbot", "llm", "gpt", "machine learning"),
    "internal_tool": ("internal tool", "internal software", "admin tool", "intranet", "internal"),
    "saas": ("saas", "software as a service", "subscription"),
    "white_label": ("white-label", "white label"),
    "workflow": ("workflow", "process management", "processes"),
    "social": ("social", "instagram", "facebook", "linkedin"),
    "crm": ("crm", "customer management", "lead management"),
    "custom_software": ("custom software", "bespoke", "custom development", "customizable",
                        "tailored", "custom internal"),
}


def _categories_of(text: str) -> dict:
    """{kategoria: frazat e gjetura} brenda një teksti."""
    lower = (text or "").lower()
    out = {}
    for cat, phrases in CAPABILITY_CATEGORIES.items():
        hits = [p for p in phrases if p in lower]
        if hits:
            out[cat] = hits
    return out


def _catalog(active_only: bool = True) -> list[dict]:
    """Produkte nga seksioni strukturuar 'products' (Faza 4C/5C)."""
    products=repo.list_products(active_only=active_only)
    projects,_=repo.get_profile_section('projects')
    names={p.get('name','').casefold() for p in products}
    for project in projects:
        name=project.get('title','')
        if not name or name.casefold() in names:continue
        products.append({'name':name,'capabilities':[project.get('description','')],
                         'source_project':True,'technologies':[project.get('stack','')],'status':'active'})
    return products


def product_categories(product: dict) -> dict:
    """Kategorritë që mbulon produkti — VETËM nga capabilities/tip/emër të deklaruara."""
    parts = [product.get("name") or "", product.get("kind") or ""]
    parts += [str(c) for c in (product.get("capabilities") or [])]
    return _categories_of(" | ".join(parts))


def opportunity_categories(op: dict) -> dict:
    """Kategorritë e nevojave të detektuara nga përmbajtja e mundësisë."""
    text = " ".join([
        op.get("title") or "", op.get("description") or "",
        op.get("opportunity_type") or "",
        " ".join(str(t) for t in (op.get("tags") or [])),
    ])
    kd = op.get("kind_data") if isinstance(op.get("kind_data"), dict) else {}
    if kd.get("detected_need"):
        text += " " + kd["detected_need"]
    return _categories_of(text)


def evaluate(op: dict, products: list[dict] | None = None) -> dict:
    """Vlerësim deterministic mbi produktet AKTIVE të katalogut.
    Kthen {abstain, catalog_empty, needed_categories, candidates[ranked],
    best, alternatives}."""
    products = products if products is not None else _catalog(active_only=True)
    needed = opportunity_categories(op)
    if not needed:
        return {"abstain": True, "catalog_empty": not products,
                "needed_categories": {}, "candidates": [], "best": None,
                "alternatives": [],
                "reason": "pa sinjale nevoje — product fit UNKNOWN (jo negative)"}
    if not products:
        return {"abstain": True, "catalog_empty": True,
                "needed_categories": needed, "candidates": [], "best": None,
                "alternatives": [],
                "reason": "katalog bosh — shto produkt te Profili (pa fit të shpikur)"}

    candidates = []
    for product in products:
        cats = product_categories(product)
        matched = sorted(set(needed) & set(cats))
        missing = sorted(set(needed) - set(cats))
        unknown = []  # categories s'që i përjashtojmë: evidenca mungon → nuk gjykojmë
        hard_mismatch = (len(matched) == 0)
        fit = round(len(matched) / len(needed) * 100) if needed else None
        customization = ("low" if not missing else
                         "medium" if len(missing) <= 1 else "high")
        evidence = [
            {"category": c, "opportunity_phrases": needed[c], "product_capability_hits": cats.get(c, [])}
            for c in matched
        ]
        reasons = []
        if matched:
            reasons.append("mbulon: " + ", ".join(matched))
        if missing:
            reasons.append("nuk mbulon: " + ", ".join(missing) + " (personalizim i nevojshëm)")
        if hard_mismatch:
            reasons.append("asnjë kategori e nevojave nuk mbulohet nga produkti")
        unknown = []
        if product.get('source_project'):
            unknown.append('gatishmëria për shitje dhe dorëzim e projektit')
        if not (product.get("languages") or product.get("technologies")):
            unknown.append("gjuhët/teknologjitë e produktit")
        if not product.get("deployment_model"):
            unknown.append("modeli i deploy-mentit")
        if not product.get("industries"):
            unknown.append("industritë e synuara")

        candidates.append({
            "product_name": product.get("name") or "",
            "type": product.get("type") or product.get("kind") or "",
            "fit_score": fit,
            "matched": matched,
            "missing": missing,
            "unknown": unknown,
            "evidence": evidence,
            "customization_level": customization,
            "hard_mismatch": hard_mismatch,
            "reasons": reasons,
        })

    candidates.sort(key=lambda c: (-c["fit_score"], c["product_name"]))
    best = candidates[0] if candidates else None
    return {"abstain": False, "catalog_empty": not products,
            "needed_categories": needed, "candidates": candidates,
            "best": best, "alternatives": candidates[1:]}
