"""Orkestratori i zbulimit (Faza 5C) — një shërbim për kërkimet manuale.

Rrjedha (spec §4):
    Search Profile → përzgjedhja e burimeve → konektorët → normalizimi →
    dedup → lentët/classification → deterministic matching → product fit ku
    i përket → përmbledhje.

- Ripërdor run_source_search (konektorë + normalizim + upsert + search_runs)
  — zero logjikë e dyfishtë.
- Dështimi i një burimi është i izoluar; përmbledhja kthehet gjithmonë.
- Sinkron, modular — ekzekutimi progresif mbetet punë e ardhshme (spec §5).
- Kufij sipas burimit ku API-t e mbështesin (SearchQuery.limit).
"""
import connectors
import repository as repo


def run(profile_id: int | None = None, sources: list[str] | None = None,
        max_items: int = 50, label: str = "") -> dict:
    """Ekzekuto kërkimin e orkestruar — kthen përmbledhje të plotë (fail-soft).

    profile_id → burimet e atij profili; None → të gjitha burimet e
    konfiguruara (sjellja e butonit 'Gjej mundësi').
    """
    profile = None
    if profile_id:
        profile = repo.get_search_profile(profile_id)
        if not profile:
            return {"ok": False, "error": "profili nuk u gjet"}

    results = connectors.run_profile_search(profile, limit=max_items,
                                            sources_override=sources) if profile \
        else _run_all_configured(max_items)

    per_source = [{
        "source": r["source"], "ok": r["ok"], "discovered": r["discovered"],
        "new": r["new"], "dup": r["dup"], "error": r["error"],
        "kinds": r.get("kinds") or {}, "new_ids": r.get("new_ids") or [],
    } for r in results]
    ok_sources = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]

    kinds = {"job": 0, "freelance": 0, "product_lead": 0}
    for r in results:
        for k, n in (r.get("kinds") or {}).items():
            kinds[k] = kinds.get(k, 0) + (n or 0)

    summary = {
        "ok": True,
        "profile": profile["name"] if profile else "Të gjitha burimet",
        "label": label,
        "sources_attempted": len(results),
        "sources_ok": len(ok_sources),
        "sources_failed": len(failed),
        "checked": sum(r["discovered"] for r in results),
        "new": sum(r["new"] for r in results),
        "dup": sum(r["dup"] for r in results),
        "kinds_new": kinds,
        "errors": [{"source": r["source"], "error": r["error"]} for r in failed],
        "results": per_source,
    }
    summary["headline"] = (f"{summary['checked']} checked · {summary['new']} new · "
                           f"{len(ok_sources)} sources")
    return summary


def _run_all_configured(max_items: int) -> list[dict]:
    return [connectors.run_source_search(sid, connectors.SearchQuery(limit=max_items))
            for sid, c in connectors.REGISTRY.items() if c.is_configured()]
