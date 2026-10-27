"""Shtresa e konektorëve (spec §11) + orkestrimi i kërkimeve me search_runs.

Kontrata e konektorit:
- `search(query) -> ConnectorResult` — KURRË s'hedh exception; gabimet kthehen
  si `ConnectorResult.error`. Një burim i thyer s'bën dështuar kërkimin total.
- `is_configured()` — gjendja e autentikimit/konfigurimit (p.sh. celesat e Adzuna).
- `health()` — status i lehtë pa fetch.
- metadata: id / name / note / requires_key.

Rezultatet rrjedhin të njëjtin rrugë si më parë: normalizim (në search_apis)
→ repo.upsert_opportunity (URL kanonik + dedup L2 + opportunity_sources).
"""
from dataclasses import dataclass, field

import httpx
import sqlite3

import repository as repo
import search_apis
import config
import i18n


@dataclass
class SearchQuery:
    """Parametrat e kërkimit — çdo konektor përdor ç'mbështet."""
    what: str = ""
    where: str = ""
    salary_min: int = 0
    country: str = ""          # vetëm Adzuna (de/at/ch)
    kinds: tuple[str, ...] = ()
    limit: int = 50            # kufi i rezultateve ku burimi e mbështet
    radius_km: int = 0         # explicit profile travel radius; 0 keeps provider default
    site: str = ""             # curated web-search target; never a URL to fetch


@dataclass
class ConnectorResult:
    source: str
    items: list = field(default_factory=list)
    error: str = ""


class Connector:
    id: str = ""
    name: str = ""
    note: str = ""
    requires_key: bool = False

    def is_configured(self) -> bool:
        return True

    def health(self) -> dict:
        ok = self.is_configured()
        return {
            "id": self.id,
            "name": self.name,
            "ok": ok,
            "detail": "" if ok else "duhet konfigurim (celes)",
            "requires_key": self.requires_key,
        }

    def search(self, query: SearchQuery) -> ConnectorResult:
        raise NotImplementedError


def _fetch_error(e: Exception) -> str:
    # HTTP exceptions can embed API keys in their URL. Never persist that text.
    detail = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
    return f"burimi i papërdorshëm: {detail}"


class ArbeitnowConnector(Connector):
    id = "arbeitnow"
    name = "Arbeitnow"
    note = "Punë në Gjermani · pa celes"

    def search(self, query: SearchQuery) -> ConnectorResult:
        try:
            return ConnectorResult(self.id, extended_sources.relevant(search_apis.arbeitnow(pages=2 if query.what else 1),query))
        except (httpx.HTTPError, ValueError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


class RemotiveConnector(Connector):
    id = "remotive"
    name = "Remotive"
    note = "Punë remote · pa celes"

    def search(self, query: SearchQuery) -> ConnectorResult:
        try:
            return ConnectorResult(self.id, search_apis.remotive(**({'search':query.what} if query.what else {})))
        except (httpx.HTTPError, ValueError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


class JobicyConnector(Connector):
    id = "jobicy"
    name = "Jobicy"
    note = "Punë remote · pa celes"

    def search(self, query: SearchQuery) -> ConnectorResult:
        try:
            return ConnectorResult(self.id, search_apis.jobicy(**({'tag':query.what} if query.what else {})))
        except (httpx.HTTPError, ValueError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


class AdzunaConnector(Connector):
    id = "adzuna"
    name = "Adzuna"
    note = "Punë DACH · kërkon celes"
    requires_key = True

    def is_configured(self) -> bool:
        return bool(repo.get_setting("adzuna_app_id") and repo.get_setting("adzuna_app_key"))

    def search(self, query: SearchQuery) -> ConnectorResult:
        if not self.is_configured():
            return ConnectorResult(
                self.id,
                error="Adzuna: vendos celesat te Profili › Burimet (developer.adzuna.com — falas)",
            )
        flt = repo.get_filters()
        what = (query.what or repo.get_setting("adzuna_what")).strip() or " ".join(flt["keywords"])
        where = (query.where if query.country else (query.where or repo.get_profile().get('location') or (repo.get_setting("adzuna_where") if repo._uid(repo._actor(None))==1 else ''))).strip()
        country = query.country or repo.get_setting("adzuna_country", "de") or "de"
        if country not in config.SEARCH_COUNTRIES:
            country = "de"
        try:
            return ConnectorResult(self.id, search_apis.adzuna(
                app_id=repo.get_setting("adzuna_app_id"),
                app_key=repo.get_setting("adzuna_app_key"),
                country=country,
                what=what,
                where=where,
                salary_min=(query.salary_min if query.kinds == ("local_job",) else query.salary_min or flt["salary_min"]),
                **({'distance':query.radius_km} if query.radius_km else {}),
            ))
        except search_apis.MissingKeyError as e:
            return ConnectorResult(self.id, error=str(e))
        except (httpx.HTTPError, ValueError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


# ----------------------------------------------- burime freelance/leads (5A)

class HackerNewsFreelanceConnector(Connector):
    id = "hackernews_freelance"
    name = "HN Freelance"
    note = "Thread-i mujor HN Freelancer · API publik Algolia"

    def search(self, query: SearchQuery) -> ConnectorResult:
        try:
            return ConnectorResult(self.id, search_apis.hackernews_freelance())
        except (httpx.HTTPError, ValueError, OSError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


class RemotiveContractConnector(Connector):
    id = "remotive_contract"
    name = "Remotive Kontrata"
    note = "Kontrata/freelance nga Remotive · pa celes"

    def search(self, query: SearchQuery) -> ConnectorResult:
        try:
            return ConnectorResult(self.id, search_apis.remotive_contract())
        except (httpx.HTTPError, ValueError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


class JobicyContractConnector(Connector):
    id = "jobicy_contract"
    name = "Jobicy Kontrata"
    note = "Kontrata nga Jobicy · pa celes"

    def search(self, query: SearchQuery) -> ConnectorResult:
        try:
            return ConnectorResult(self.id, search_apis.jobicy(contract_only=True))
        except (httpx.HTTPError, ValueError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


class LocalFeedConnector(Connector):
    id = "local_feed"
    name = "Feed-e lokale"
    note = "RSS/Atom publike lokale · konfigurohen te Profili"

    def is_configured(self) -> bool:
        import cloud_database
        if cloud_database.enabled(): return False
        urls = repo.local_feed_urls()
        return bool([u for u in urls.split(",") if u.strip()])

    def search(self, query: SearchQuery) -> ConnectorResult:
        import cloud_database
        if cloud_database.enabled():
            return ConnectorResult(self.id,error='Feed-et personale aktivizohen pas verifikimit të burimit.')
        urls = [u.strip() for u in repo.local_feed_urls().split(",") if u.strip()]
        if not urls:
            return ConnectorResult(
                self.id, error="pa feed-e lokale — konfiguro URL-t te Profili › Lokale")
        try:
            return ConnectorResult(self.id, search_apis.local_feed(
                urls, limit=query.limit or 30))
        except (httpx.HTTPError, ValueError, OSError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


class HNLeadsConnector(Connector):
    id = "hn_leads"
    name = "HN Leads"
    note = "Kërkesa software nga HN · keyword-gate deterministic"

    def search(self, query: SearchQuery) -> ConnectorResult:
        try:
            return ConnectorResult(self.id, search_apis.hackernews_leads())
        except (httpx.HTTPError, ValueError, OSError) as e:
            return ConnectorResult(self.id, error=_fetch_error(e))


REGISTRY: dict[str, Connector] = {
    c.id: c for c in (ArbeitnowConnector(), RemotiveConnector(),
                      JobicyConnector(), AdzunaConnector(),
                      HackerNewsFreelanceConnector(), RemotiveContractConnector(),
                      JobicyContractConnector(), HNLeadsConnector(),
                      LocalFeedConnector())
}

import extended_sources
import usage_ledger
import usage_runtime
REGISTRY.update({c.id:c for c in extended_sources.register(Connector,ConnectorResult,_fetch_error)})


# OE-007F: klasa e ekzekutimit për politikën e freskisë — METADATA QENDRORE
# (kurrë kushte të shpërndara te scheduleri). Panjohur → MANUAL_ONLY (konservativ).
EXECUTION_CLASSES = {
    # API/feed publike pa çelës — të përshtatshme për polling të planifikuar.
    "arbeitnow": "FREE_SAFE", "remotive": "FREE_SAFE", "jobicy": "FREE_SAFE",
    "remoteok": "FREE_SAFE", "hn_hiring": "FREE_SAFE",
    "hackernews_freelance": "FREE_SAFE", "hn_leads": "FREE_SAFE",
    "remotive_contract": "FREE_SAFE", "jobicy_contract": "FREE_SAFE",
    "bund_jobs": "FREE_SAFE", "bund_tenders": "FREE_SAFE",
    # Me çelës të user-it / kuotë të kufizuar falas — polling me kujdes.
    "adzuna": "CONFIGURED_FREE_OR_QUOTA",
    "greenhouse": "CONFIGURED_FREE_OR_QUOTA", "lever": "CONFIGURED_FREE_OR_QUOTA",
    "ashby": "CONFIGURED_FREE_OR_QUOTA",
    # Paguajnë për kërkesë — KURRË pa opt-in të shprehur + buxhet aktiv.
    "google_jobs": "METERED_PAID", "google_web": "METERED_PAID",
    "brave_web": "METERED_PAID",
    # Personal/pa mbikëqyrje — vetëm ekzekutim manual.
    "local_feed": "MANUAL_ONLY",
}


def execution_class(source_id: str) -> str:
    """Klasa e ekzekutimit të burimit (OE-007F)."""
    return EXECUTION_CLASSES.get(source_id, "MANUAL_ONLY")


def registry_meta() -> list[dict]:
    """Metadata + gjendja e konfigurimit për UI."""
    return [
        {**c.health(), "note": c.note, "exec_class": execution_class(c.id)}
        for c in REGISTRY.values()
    ]


# ------------------------------------------------- orkestrimi i kërkimeve

def run_source_search(source_id: str, query: SearchQuery | None = None,
                      profile: dict | None = None) -> dict:
    """Një kërkim në një burim, me regjistrim të plotë search_run.
    Kurrë s'hedh exception; kthen {ok, source, discovered, new, dup, error}."""
    query = query or SearchQuery()
    profile_id = profile["id"] if profile else None
    run_id = repo.start_search_run(source_id, profile_id=profile_id)

    connector = REGISTRY.get(source_id)
    if connector is None:
        repo.finish_search_run(run_id, error="burim i panjohur", status="error")
        return {"ok": False, "source": source_id, "discovered": 0, "new": 0,
                "dup": 0, "kinds": {}, "new_ids": [], "error": "burim i panjohur",
                "run_id": run_id}

    try:
        result = connector.search(query)
    except (usage_runtime.ProviderDispatchDisabled,usage_ledger.UsageDenied) as exc:
        message=i18n.translate(usage_runtime.message_key(exc))
        repo.finish_search_run(run_id,error=message,status='error')
        return {'ok':False,'source':source_id,'discovered':0,'new':0,'dup':0,
                'kinds':{},'new_ids':[],'error':message,'run_id':run_id}
    if result.error:
        repo.finish_search_run(run_id, error=result.error, status="error")
        return {"ok": False, "source": source_id, "discovered": 0, "new": 0,
                "dup": 0, "kinds": {}, "new_ids": [], "error": result.error,
                "run_id": run_id}

    new = dup = 0
    new_ids: list = []
    kinds: dict = {}
    items = result.items[:max(1, min(200, int(query.limit)))]
    # OE-007E: kontekst i pikimit i përgatitur NJË herë për gjithë kërkimin —
    # pa pyetje profili/filtrash për çdo rresht (kosto e madhe në Turso).
    scoring = repo.scoring_context() if items else None
    for item in items:
        # Preserve provider facts; the search context only adds a board lens.
        item = {**item, "_local_search": query.kinds == ("local_job",)}
        try:status, opp_id = repo.upsert_opportunity_full(item, scoring=scoring)
        except (sqlite3.Error,ValueError,TypeError,KeyError) as e:
            message=f'Ruajtja e burimit dështoi ({type(e).__name__}). Provo përsëri.'
            repo.finish_search_run(run_id,discovered=len(items),new=new,dup=dup,error=message,status='error')
            return {'ok':False,'source':source_id,'discovered':len(items),'new':new,'dup':dup,'kinds':kinds,'new_ids':new_ids,'error':message,'run_id':run_id}
        if status == "new":
            new += 1
            new_ids.append(opp_id)
            k = item.get("kind") or "job"
            kinds[k] = kinds.get(k, 0) + 1
        else:
            dup += 1
    repo.finish_search_run(run_id, discovered=len(items),
                           new=new, dup=dup, status="ok")
    return {"ok": True, "source": source_id, "discovered": len(items),
            "new": new, "dup": dup, "kinds": kinds, "new_ids": new_ids,
            "error": "", "run_id": run_id}


def run_profile_search(profile: dict, limit: int | None = None,
                       sources_override: list[str] | None = None) -> list[dict]:
    """Të gjitha burimet e profilit, një nga një (pa infrastrukturë async — spec §30 F2).
    sources_override: nëngrup burimesh (scheduli 7A)."""
    sources = sources_override or profile.get("sources") or [
        cid for cid, c in REGISTRY.items() if c.is_configured()
    ]
    query = SearchQuery(
        what=" ".join(profile.get("keywords") or []),
        where=profile.get("location") or "",
        salary_min=int(profile.get("salary_min") or 0),
        kinds=tuple(profile.get("kinds") or ()),
        limit=int(limit or 50),
    )
    return [run_source_search(s, query, profile=profile) for s in sources]
