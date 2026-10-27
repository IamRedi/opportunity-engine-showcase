"""One bounded search plan for employment, freelance, local work and product demand."""
import re
import config
import connectors
import repository as repo
import web_discovery

COUNTRIES=config.SEARCH_COUNTRIES
BOARD_PROVIDERS=('greenhouse','lever','ashby')


DEFAULT_SOURCES={
        'job':['adzuna','google_jobs','arbeitnow','remotive','jobicy','remoteok','hn_hiring',*BOARD_PROVIDERS,'bund_jobs','google_web','brave_web'],
        'freelance':['hackernews_freelance','remotive_contract','jobicy_contract','google_web','brave_web'],
        'local_job':['adzuna','google_jobs','local_feed','google_web','brave_web'],
        'product_lead':['hn_leads','bund_tenders','google_web','brave_web'],
}


def product_choices():
    choices=[{**p,'choice_id':str(p['idx'])} for p in repo.list_products(active_only=True)]
    projects,_=repo.get_profile_section('projects')
    choices.extend({'name':p.get('title',''),'choice_id':f'project:{i}','short_description':p.get('description',''),
                    'capabilities':[]} for i,p in enumerate(projects) if p.get('title'))
    return choices


def plan(kind,what='',where='',country='de',product_id='',sources=None,categories=None,web_target=''):
    if kind not in ('job','freelance','product_lead','local_job'):raise ValueError('Lloj i pavlefshëm')
    if country not in COUNTRIES:raise ValueError('Vend i pavlefshëm')
    site='' if web_target in ('','*') else web_target
    web_discovery.validate_target(site,kind,country)
    profile=repo.get_profile()
    what=str(what).strip()[:250];where=str(where).strip()[:150]
    if product_id:
        product=next((p for p in product_choices() if p['choice_id']==str(product_id)),None)
        if not product:raise ValueError('Produkti nuk u gjet')
        if not what:what=' '.join(product.get('capabilities',[])[:3]) or product.get('short_description','')[:200]
    if not what and kind in ('job','freelance'):what=profile.get('title','')[:200]
    selected=[]
    if kind=='local_job':
        prefs=repo.get_local_prefs()
        if categories is None: categories=(prefs.get('categories') or [])[:3]
        if not isinstance(categories,list) or any(not isinstance(c,str) or c not in config.LOCAL_SEARCH_TERMS for c in categories):
            raise ValueError('Kategori e pavlefshme.')
        selected=list(dict.fromkeys(categories))
        if len(selected)>3:raise ValueError('Zgjidh deri në 3 kategori për kërkim.')
        if not where and country=='de':where=str(prefs.get('base_location') or prefs.get('postcode') or profile.get('location') or '')[:150]
    defaults=DEFAULT_SOURCES[kind]
    chosen=defaults if sources is None else [s for s in sources if s in defaults]
    web_provider=next((s for s in web_discovery.PROVIDERS if connectors.REGISTRY[s].is_configured()),None)
    if web_target:
        if not web_provider:raise ValueError('Lidh SerpApi ose Brave te Burimet për rezultate web brenda aplikacionit.')
        chosen=[web_provider]
    elif sources is None:
        # One web provider, not duplicate paid Google + Brave requests.
        chosen=[s for s in chosen if s not in web_discovery.PROVIDERS]
        if web_provider:chosen.append(web_provider)
    tasks=[]
    terms=[('',what)]
    if selected:
        language_index=0 if country in ('de','at','ch') else 1
        terms=[(category,' '.join(filter(None,[config.LOCAL_SEARCH_TERMS[category][language_index],what]))) for category in selected]
    for category,term in terms:
        for sid in chosen:
            if not connectors.REGISTRY[sid].is_configured():continue
            query=term
            if kind=='local_job' and not term and sid in ('adzuna','google_jobs'):query=(term+' Minijob').strip() if country in ('de','at','ch') else (term+' part time').strip()
            if sid in ('adzuna','google_jobs') and not query:continue
            tasks.append({'source':sid,'what':query[:250],'where':where,'country':country,'kinds':[kind],'limit':30,'category':category,'site':site})
    return {'tasks':tasks,'query':what,'where':where,'country':country,'kind':kind,'categories':selected}


def run_task(data):
    sid=data.get('source')
    if sid not in connectors.REGISTRY:raise ValueError('Burim i panjohur')
    country=data.get('country','de')
    if country not in COUNTRIES:raise ValueError('Vend i pavlefshëm')
    kinds=data.get('kinds',['job'])
    if not isinstance(kinds,list) or len(kinds)!=1 or kinds[0] not in ('job','freelance','local_job','product_lead'):raise ValueError('Lloj i pavlefshëm')
    site=data.get('site','')
    web_discovery.validate_target(site,kinds[0],country)
    if site and sid not in web_discovery.PROVIDERS:raise ValueError('Platforma nuk mbështet këtë lloj kërkimi ose vend.')
    what=str(data.get('what',''))[:250];where=str(data.get('where',''))[:150]
    # Radius comes from current owner preferences, never another pilot's defaults.
    prefs=repo.get_local_prefs()
    try:radius=max(0,min(500,int(prefs.get('radius_km') or 0)))
    except (TypeError,ValueError):radius=0
    return connectors.run_source_search(sid,connectors.SearchQuery(what=what,where=where,country=country,kinds=tuple(kinds),limit=30,radius_km=radius,site=site))
