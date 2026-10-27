"""Klientë për API-t e portaleve të punës (të gjitha falas).

- Arbeitnow: punë në Gjermani, pa celes — https://www.arbeitnow.com/api/job-board-api
- Remotive:  punë remote, pa celes    — https://remotive.com/api/remote-jobs
- Jobicy:    punë remote, pa celes    — https://jobicy.com/api/v2/remote-jobs
- Adzuna:    punë DACH, celes falas   — https://developer.adzuna.com

Çdo burim kthen dicts të normalizuara: source, url, title, company, location,
remote, salary_text, description, tags, posted_at ("YYYY-MM-DD HH:MM" UTC, ose ""),
salary_min/salary_max (numerike vjetore ku ka, ose None).
"""
import html as _html
import re
from datetime import datetime, timezone

import httpx
import source_http
import usage_runtime

TIMEOUT = 25
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) local-job-search/1.0"}


class MissingKeyError(RuntimeError):
    """Ndez kur mungojnë celesat e Adzuna."""


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    for ent, ch in (("&amp;", "&"), ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(ent, ch)
    text = _html.unescape(text)  # entitetet numerike/emërore të mbetura (p.sh. &#x2F;)
    return re.sub(r"\s+", " ", text).strip()


def _norm_posted(val) -> str:
    """ISO/unix/ tekst → "YYYY-MM-DD HH:MM" (UTC); "" nëse nuk parseohet."""
    try:
        if val is None:
            return ""
        if isinstance(val, (int, float)) or str(val).replace(".", "").isdigit():
            return datetime.fromtimestamp(float(val), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


def _get(url: str, params: dict | None = None) -> dict:
    return source_http.get_json(url, params, headers=UA, timeout=TIMEOUT)


def _tags(*vals) -> list[str]:
    """Burimet dërgojnë tag-ja herë string, herë listë — normalizojmë."""
    out: list[str] = []
    for v in vals:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out.extend(str(x) for x in v)
    return out


def arbeitnow(pages: int = 2) -> list[dict]:
    jobs: list[dict] = []
    for page in range(1, pages + 1):
        data = _get("https://www.arbeitnow.com/api/job-board-api", {"page": page}).get("data", [])
        if not data:
            break
        for j in data:
            jobs.append({
                "source": "arbeitnow",
                "external_id": j.get("slug") or "",
                "url": j.get("url") or ("https://www.arbeitnow.com/" + (j.get("slug") or "")),
                "title": j.get("title", ""),
                "company": j.get("company_name", ""),
                "location": j.get("location", ""),
                "remote": bool(j.get("remote_work")),
                "salary_text": "",
                "description": _strip_html(j.get("description", "")),
                "tags": _tags(j.get("tags")),
                "posted_at": _norm_posted(j.get("created_at")),
            })
    return jobs


def remotive(limit: int = 50, job_types: tuple = (), search: str = '') -> list[dict]:
    """job_types: filtrim opsional (p.sh. ('contract','freelance')) — për
    konektorët freelance; bosh = të gjitha (sjellja legacy)."""
    jobs = _get("https://remotive.com/api/remote-jobs", {"limit": limit,**({'search':search} if search else {})}).get("jobs", [])
    out = []
    for j in jobs:
        jt = str(j.get("job_type") or "").lower()
        if job_types and jt not in job_types:
            continue
        out.append({
            "source": "remotive",
            "external_id": str(j.get("id") or ""),
            "url": j.get("url", ""),
            "title": j.get("title", ""),
            "company": j.get("company_name", ""),
            "location": (j.get("candidate_required_location") or "Remote") + " (remote)",
            "remote": True,
            "salary_text": j.get("salary") or "",
            "description": _strip_html(j.get("description", "")),
            "tags": _tags(j.get("tags")),
            "posted_at": _norm_posted(j.get("publication_date")),
            "raw_job_type": jt,
        })
    return out


def remotive_contract(limit: int = 50) -> list[dict]:
    """Kontrata/freelance nga Remotive → kind=freelance (Faza 5A)."""
    items = remotive(limit, job_types=("contract", "freelance"))
    for it in items:
        it["kind"] = config_kind_freelance()
        it["opportunity_type"] = "Kontratë"
    return items


def config_kind_freelance() -> str:
    import config
    return config.KIND_FREELANCE


def jobicy(count: int = 50, contract_only: bool = False, tag: str = '') -> list[dict]:
    jobs = _get("https://jobicy.com/api/v2/remote-jobs", {"count": count,**({'tag':tag[:50]} if len(tag)>=3 else {})}).get("jobs", [])
    out = []
    for j in jobs:
        jt = str(j.get("jobType") or "")
        if contract_only and "contract" not in jt.lower():
            continue
        smin, smax = j.get("salaryMin"), j.get("salaryMax")
        cur = j.get("salaryCurrency") or ""
        period = str(j.get("salaryPeriod") or "").upper()
        annual = period.startswith("YEAR")
        salary = f"{smin}–{smax} {cur}/{j.get('salaryPeriod', '')}".strip() if smin else ""
        out.append({
            "source": "jobicy",
            "external_id": str(j.get("id") or ""),
            "url": j.get("url", ""),
            "title": j.get("jobTitle", ""),
            "company": j.get("companyName", ""),
            "location": (j.get("jobGeo") or "Remote") + " (remote)",
            "remote": True,
            "salary_text": salary,
            "salary_min": smin if annual else None,
            "salary_max": smax if annual else None,
            "description": _strip_html(j.get("jobDescription") or j.get("jobExcerpt") or ""),
            "tags": _tags(j.get("jobIndustry"), j.get("jobLevel"), j.get("jobType")),
            "posted_at": _norm_posted(j.get("pubDate")),
            "raw_job_type": jt,
            **({"kind": config_kind_freelance(),
                "opportunity_type": "Kontratë"} if contract_only else {}),
        })
    return out


# ------------------------------------------------ HN Algolia (Faza 5A)

_HN_TECH_VOCAB = ("python", "javascript", "typescript", "react", "vue", "angular", "node",
                  "rust", "golang", "go", "java", "kotlin", "swift", "php", "ruby", "rails",
                  "django", "fastapi", "laravel", ".net", "c#", "c++", "sql", "postgres",
                  "mysql", "mongodb", "aws", "azure", "gcp", "docker", "kubernetes", "terraform",
                  "ai", "ml", "llm", "gpt", "machine learning", "devops", "react native",
                  "flutter", "ios", "android", "wordpress", "shopify", "next.js", "svelte")


def _hn_latest_thread(thread_query: str):
    """Gjej thread-in mujor më të fundit HN (Algolia API publik)."""
    data = _get("https://hn.algolia.com/api/v1/search_by_date",
                {"tags": "story", "query": thread_query, "hitsPerPage": 1})
    hits = data.get("hits", [])
    return hits[0]["objectID"] if hits else None


def _hn_comment_to_item(c: dict, source: str) -> dict | None:
    text = _strip_html(c.get("text") or "")
    if len(text) < 40:
        return None
    lower = text.lower()
    first_line = text.split("\n")[0].strip()[:140]
    tags = [t for t in _HN_TECH_VOCAB if t in lower]
    remote = "remote" in lower
    return {
        "source": source,
        "external_id": f"hn-{c.get('id')}",
        "url": f"https://news.ycombinator.com/item?id={c.get('id')}",
        "title": first_line or "HN post",
        "company": "",
        "location": "Remote" if remote else "",
        "remote": remote,
        "salary_text": "",
        "description": text[:4000],
        "tags": tags,
        "posted_at": _norm_posted(c.get("created_at")),
    }


def hackernews_freelance(limit: int = 30) -> list[dict]:
    """Komentet e thread-it mujor 'Freelancer? Seeking freelancer?' → projekte
    freelance. API publik HN Algolia (legal, pa scraping HTML)."""
    story = _hn_latest_thread("Freelancer Seeking freelancer")
    if not story:
        return []
    item = _get(f"https://hn.algolia.com/api/v1/items/{story}")
    out = []
    for c in item.get("children", []):
        if len(out) >= limit:
            break
        if not _hn_is_hiring(_strip_html(c.get("text") or "")):
            continue
        it = _hn_comment_to_item(c, "hackernews")
        if it:
            it["kind"] = "freelance"
            out.append(it)
    return out


_SEEK_PATTERNS = ("seeking", "looking for", "we need", "need a", "need an",
                  "hiring a", "searching for", "seeking a")


def _hn_is_hiring(text: str) -> bool:
    """Accept hiring demand, never SEEKING WORK self-promotion as an opportunity."""
    lower = text.lower().strip()
    if re.match(r"^[\W_]*(?:seeking\s+work|looking\s+for\s+work|for\s+hire)\b", lower):
        return False
    if re.match(r"^[\W_]*(?:seeking\s+(?:a\s+)?freelancers?|hiring)\b", lower):
        return True
    return bool(re.search(
        r"\b(?:we(?:\s+are|'re)?\s+(?:looking\s+for|seeking|hiring)|"
        r"(?:looking\s+for|seeking|need(?:ing)?|hiring)\s+(?:an?\s+)?"
        r"(?:[a-z0-9+#.-]+\s+){0,3}(?:freelancer|developer|engineer|designer|consultant|agency|contractor|"
        r"software|website|web\s+app|automation|ai\s+chatbot))\b", lower))


def hackernews_leads(limit: int = 25) -> list[dict]:
    """Ana 'seek' e thread-it: biznese që kërkojnë ndërtim software →
    product_lead me keyword-gate deterministic (pa AI)."""
    story = _hn_latest_thread("Freelancer Seeking freelancer")
    if not story:
        return []
    item = _get(f"https://hn.algolia.com/api/v1/items/{story}")
    out = []
    for c in item.get("children", []):
        if len(out) >= limit:
            break
        text = _strip_html(c.get("text") or "")
        lower = text.lower()
        if not _hn_is_hiring(text):
            continue  # ofertues vetë — jo lead
        it = _hn_comment_to_item(c, "hn_leads")
        if not it:
            continue
        # detected_need: fjalia e parë me pattern
        detected = ""
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if any(p in sentence.lower() for p in _SEEK_PATTERNS):
                detected = sentence[:200]
                break
        otype = ""
        for kw, t in (("website", "Website"), ("web app", "Website"), ("booking", "Website"),
                      ("app", "SaaS"), ("saas", "SaaS"), ("automat", "Automatizim"),
                      ("ai ", "Zgjidhje AI"), ("chatbot", "Zgjidhje AI")):
            if kw in lower:
                otype = t
                break
        it.update(kind="product_lead", opportunity_type=otype or "Kërkesë software",
                  kind_data={"detected_need": detected})
        out.append(it)
    return out


def adzuna(app_id: str, app_key: str, country: str = "de", what: str = "", where: str = "", salary_min: int = 0, distance: int = 0) -> list[dict]:
    if not app_id or not app_key:
        raise MissingKeyError("Adzuna: vendos celesat te Profili › Burimet (developer.adzuna.com — falas)")
    params: dict = {"app_id": app_id, "app_key": app_key, "results_per_page": 50}
    if what:
        params["what"] = what
    if where:
        params["where"] = where
        if distance: params['distance']=max(1,min(500,int(distance)))
    if salary_min:
        params["salary_min"] = salary_min
    results = _get(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params).get("results", [])
    out = []
    for j in results:
        smin, smax = j.get("salary_min"), j.get("salary_max")
        salary = f"{smin:,.0f}–{smax:,.0f} €/vit".replace(",", ".") if smin and smax else ""
        out.append({
            "source": "adzuna",
            "external_id": "",
            "url": j.get("redirect_url", ""),
            "title": j.get("title", ""),
            "company": (j.get("company") or {}).get("display_name", ""),
            "location": (j.get("location") or {}).get("display_name", ""),
            "remote": False,
            "salary_text": salary,
            "salary_min": smin,
            "salary_max": smax,
            "description": _strip_html(j.get("description", "")),
            "tags": [],
            "posted_at": _norm_posted(j.get("created")),
        })
    return out


# ------------------------------------------------ local feed (Faza 5D)

def _local_category(text_lower: str):
    import config
    for cat, keywords in config.LOCAL_CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return cat
    return "other_local"


def _extract_hourly(text: str):
    """Tarifa orare VETËM nëse shkruajtur qartë (€/h, €/Std) — pa shpikje."""
    m = re.search(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|eur|euro)\s*(?:pro\s*)?/?\s*(?:h|std|stunde|hour)\b",
                  text.lower())
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _extract_postcode(text: str):
    m = re.search(r"\b(\d{5})\b", text)
    return m.group(1) if m else ""


def local_feed(feed_urls: list[str], limit: int = 30) -> list[dict]:
    """Konektor gjenerik RSS/Atom për feed-e PUBLIKE lokale (konfigurohen nga
    përdoruesi te Profili › Preferencat lokale). Pa scraping-platformash."""
    import config
    import xml.etree.ElementTree as ET
    out = []
    for feed_url in feed_urls[:5]:
        try:
            from urllib.parse import urlparse
            parsed=urlparse(feed_url)
            r=usage_runtime.http_response('search-local-feed','local-feed',
                lambda:httpx.get(feed_url,headers=UA,timeout=TIMEOUT,follow_redirects=True),
                fingerprint={'host':parsed.hostname or '',
                    'path_hash':__import__('hashlib').sha256(parsed.path.encode()).hexdigest()},max_redirects=20)
            r.raise_for_status()
            raw = r.text
        except (httpx.HTTPError, ValueError):
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for it in items[:limit]:
            def _t(tag, attr="text"):
                el = it.find(tag) or it.find(f".//{tag}")
                if el is None:
                    ns = it.find(f".//{{http://www.w3.org/2005/Atom}}{tag}")
                    el = ns
                if el is None:
                    return ""
                return (el.text or el.get(attr) or "").strip()
            title = _t("title")
            desc = _t("description") or _t("summary")
            link = _t("link", "href") or _t("guid")
            pub = _t("pubDate") or _t("published") or _t("updated")
            text = f"{title} {desc}"
            lower = text.lower()
            if not title:
                continue
            cat = _local_category(lower)
            out.append({
                "source": "local_feed",
                "external_id": link or title[:60],
                "url": link,
                "title": title[:200],
                "company": "",
                "location": "",
                "postcode": _extract_postcode(text),
                "remote": False,
                "description": desc[:4000] or title,
                "tags": [cat],
                "kind": config.KIND_LOCAL,
                "opportunity_type": cat,
                "hourly_rate": _extract_hourly(lower),
                "kind_data": {"category": cat,
                              "schedule": (_extract_schedule(text) or "")},
                "posted_at": _norm_posted(pub),
            })
            if len(out) >= limit:
                return out
    return out


def _extract_schedule(text: str):
    import re as _re
    m = _re.search(r"(?:schedule|zeit|when\?|orarin|orari|orar)[:\s\-]+([^.<\n]{5,80})", text,
                   _re.IGNORECASE)
    return m.group(1).strip() if m else ""
