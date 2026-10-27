"""Indexed web previews, not direct marketplace scraping or verified vacancies."""
import re
from urllib.parse import urlparse

import external_platforms
import search_apis as api

PROVIDERS = ('google_web', 'brave_web')
KINDS = ('job', 'local_job', 'freelance', 'product_lead')


def validate_target(site, kind, country):
    if not site:
        return
    if not isinstance(site, str) or not any(
        p['site'] == site and kind in p['kinds']
        and (not p['countries'] or country in p['countries'])
        for p in external_platforms.PLATFORMS
    ):
        raise ValueError('Platforma nuk mbështet këtë lloj kërkimi ose vend.')


def query_text(query):
    kind = next((k for k in query.kinds if k in KINDS), 'job')
    validate_target(query.site, kind, query.country or 'de')
    german = query.country in ('', 'de', 'at', 'ch')
    suffix = ({'job':'(Stellenangebot OR gesucht OR hiring)',
               'local_job':'(gesucht OR suchen OR Minijob OR Teilzeit)',
               'freelance':'(Projekt OR Auftrag) (gesucht OR Ausschreibung)',
               'product_lead':'(Ausschreibung OR "Auftrag zu vergeben" OR "request for proposal")'}
              if german else
              {'job':'(job OR vacancy OR hiring)', 'local_job':'(wanted OR hiring OR "part time")',
               'freelance':'(project OR contract) (hiring OR wanted)',
               'product_lead':'("request for proposal" OR "seeking software" OR tender)'})[kind]
    return ' '.join(filter(None, ['site:'+query.site if query.site else '', query.what,
                                  query.where, suffix]))


def matches_site(url, site):
    if not site:
        return True
    target = urlparse('https://' + site)
    actual = urlparse(url)
    host = actual.hostname or ''
    return (host == target.hostname or host.endswith('.' + target.hostname)) and actual.path.startswith(target.path)


def preview(item, source, query):
    """Require listing intent; don't fabricate location, salary or posting dates."""
    from extended_sources import public_url
    url = item.get('link') if source == 'google_web' else item.get('url')
    title = api._strip_html(str(item.get('title') or ''))[:500]
    snippet = api._strip_html(str(item.get('snippet' if source == 'google_web' else 'description') or ''))[:4000]
    if not title or not public_url(url) or not matches_site(url, query.site):
        return None
    text = title + ' ' + snippet
    kind = next((k for k in query.kinds if k in KINDS), 'job')
    # Negative applicant/service-provider intent takes precedence over "gesucht".
    if re.search(r'(?i)\b(ich suche (?:einen? |eine? )?(?:job|arbeit|minijob)|stellengesuch|'
                 r'suche arbeit|biete (?:meine |unsere )?(?:dienste|hilfe|betreuung)|'
                 r'for hire|seeking work|looking for (?:a )?job)\b', text):
        return None
    demand = r'(?i)\b(gesucht|suchen|ausschreibung|auftrag zu vergeben|request for proposal|hiring|wanted|seeking freelancer)\b'
    jobs = r'(?i)\b(stellenangebot\w*|job|jobs|hiring|vacancy|minijob|stellenanzeige|teilzeit)\b'
    if kind in ('freelance', 'product_lead'):
        if not re.search(demand, text):
            return None
    elif not re.search(jobs, text) and not (kind == 'local_job' and re.search(demand, text)):
        return None
    # Broad category/search pages are navigation, not individual opportunities.
    path = urlparse(url).path.rstrip('/')
    if path in ('', '/jobs', '/jobs/search', '/projekte', '/search', '/s-jobs/c102'):
        return None
    return {'source':source, 'url':url, 'external_id':'', 'title':title,
            'company':'', 'location':'', 'remote':False, 'kind':kind,
            'opportunity_type':'Për t’u verifikuar', 'description':snippet,
            'tags':[], 'posted_at':'', 'kind_data':{
                'verification_status':'search_preview', 'source_domain':urlparse(url).hostname,
                'location_verified':False, 'availability_verified':False}}
