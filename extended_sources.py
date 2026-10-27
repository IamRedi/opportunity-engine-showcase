"""Public feeds and employer boards; explicit key-backed web discovery adapters."""
import hashlib
import re
from urllib.parse import urlparse

import httpx
import repository as repo
import search_apis as api
import source_http
import web_discovery
import usage_runtime


def public_url(value):
    p=urlparse(str(value or ''))
    return p.scheme=='https' and bool(p.hostname) and not p.username and not p.password and p.hostname not in ('localhost','127.0.0.1','::1')


def relevant(items,query):
    """Soft keyword retrieval from broad feeds; suitability is scored downstream."""
    words=[w for w in re.findall(r'[\w+#.-]+',query.what.casefold()) if len(w)>2]
    if not words:return items
    return [j for j in items if any(w in (' '.join([j.get('title',''),j.get('description',''),' '.join(j.get('tags',[]))])).casefold() for w in words)]


def boards(provider):
    entries,_=repo.get_profile_section('source_boards')
    return [e['board'] for e in entries if e.get('provider')==provider and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',str(e.get('board','')))][:8]


def board_jobs(provider,board):
    if provider=='greenhouse':
        data=source_http.get_json(f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs',{'content':'true'},usage_operation='search-greenhouse',usage_provider='greenhouse')
        return [{'source':'greenhouse','external_id':f"{board}:{j['id']}",'url':j.get('absolute_url',''),
                 'title':j.get('title',''),'company':board,'location':(j.get('location') or {}).get('name',''),
                 'description':api._strip_html(j.get('content','')),'tags':[],'posted_at':'','remote':False} for j in data.get('jobs',[]) if j.get('id') and public_url(j.get('absolute_url'))]
    if provider=='lever':
        data=source_http.get_json(f'https://api.lever.co/v0/postings/{board}',{'mode':'json','limit':100},allow_list=True,usage_operation='search-lever',usage_provider='lever')
        return [{'source':'lever','external_id':f"{board}:{j['id']}",'url':j.get('hostedUrl',''),'title':j.get('text',''),
                 'company':board,'location':(j.get('categories') or {}).get('location',''),
                 'description':api._strip_html(j.get('descriptionPlain') or j.get('description',''))+' '+api._strip_html(' '.join(x.get('content','') for x in j.get('lists',[]))),
                 'tags':[(j.get('categories') or {}).get('commitment','')],'posted_at':api._norm_posted(j.get('createdAt',0)/1000) if j.get('createdAt') else '',
                 'remote':j.get('workplaceType')=='remote'} for j in data if j.get('id') and public_url(j.get('hostedUrl'))]
    data=source_http.get_json(f'https://api.ashbyhq.com/posting-api/job-board/{board}',{'includeCompensation':'true'},usage_operation='search-ashby',usage_provider='ashby')
    return [{'source':'ashby','external_id':j.get('id') or j.get('jobUrl'),'url':j.get('jobUrl',''),
             'title':j.get('title',''),'company':board,'location':j.get('location',''),
             'description':j.get('descriptionPlain') or api._strip_html(j.get('descriptionHtml','')),
             'tags':[j.get('department',''),j.get('employmentType','')],'posted_at':api._norm_posted(j.get('publishedAt')),
             'salary_text':(j.get('compensation') or {}).get('scrapeableCompensationSalarySummary') or '',
             'remote':bool(j.get('isRemote'))} for j in data.get('jobs',[]) if j.get('isListed') is True and public_url(j.get('jobUrl'))]


def register(base,result,error):
    class FederalFeed(base):
        note='Gjermani · njoftime zyrtare publike'
        def __init__(self,tenders=False):
            self.tenders=tenders;self.id='bund_tenders' if tenders else 'bund_jobs'
            self.name='Bundesnetzagentur · Tendera' if tenders else 'Bundesnetzagentur · Punë'
        def search(self,query):
            import xml.etree.ElementTree as ET
            from email.utils import parsedate_to_datetime
            suffix='Ausschreibungen' if self.tenders else 'Stellenangebote'
            url=f'https://www.bundesnetzagentur.de/SiteGlobals/Functions/RSSFeed/DE/RSSNewsfeed/RSSNewsfeed_{suffix}.xml'
            try:
                response=usage_runtime.http_response('search-'+self.id,self.id,
                    lambda:httpx.get(url,timeout=25,follow_redirects=False),
                    fingerprint={'source':self.id,'feed':'official'},max_redirects=0)
                response.raise_for_status()
                if len(response.content)>2*1024*1024:raise ValueError('Feed too large')
                root=ET.fromstring(response.content);jobs=[]
                for j in root.findall('.//item')[:50]:
                    title=j.findtext('title') or '';link=j.findtext('link') or ''
                    if link.startswith('http://www.bundesnetzagentur.de/'):
                        link='https://'+link[len('http://'):]
                    if not title or not public_url(link):continue
                    posted=''
                    try:posted=api._norm_posted(parsedate_to_datetime(j.findtext('pubDate') or '').isoformat())
                    except (TypeError,ValueError):pass
                    jobs.append({'source':self.id,'url':link,'external_id':j.findtext('guid') or link,'title':title,
                        'company':'Bundesnetzagentur','location':'','remote':False,'description':api._strip_html(j.findtext('description') or title),
                        'posted_at':posted,'kind':'product_lead' if self.tenders else 'job','opportunity_type':'Tender' if self.tenders else 'Punë',
                        'tags':[]})
                return result(self.id,relevant(jobs,query))
            except (httpx.HTTPError,ValueError,TypeError,ET.ParseError) as e:return result(self.id,error=error(e))

    class RemoteOK(base):
        id='remoteok';name='Remote OK';note='Punë remote · burim publik'
        def search(self,query):
            try:
                data=source_http.get_json('https://remoteok.com/api',allow_list=True,usage_operation='search-remoteok',usage_provider='remoteok')
                jobs=[{'source':self.id,'external_id':str(j['id']),'url':j.get('url',''),'title':j.get('position',''),
                       'company':j.get('company',''),'location':j.get('location') or 'Remote','remote':True,
                       'description':api._strip_html(j.get('description','')),'tags':api._tags(j.get('tags')),
                       'posted_at':api._norm_posted(j.get('date'))} for j in data if j.get('id') and public_url(j.get('url'))]
                return result(self.id,relevant(jobs,query))
            except (httpx.HTTPError,ValueError,TypeError,KeyError) as e:return result(self.id,error=error(e))

    class Board(base):
        note='Faqet e punëdhënësve · shto kompanitë te Burimet'
        def __init__(self,provider):self.id=provider;self.name={'greenhouse':'Greenhouse','lever':'Lever','ashby':'Ashby'}[provider]
        def is_configured(self):return bool(boards(self.id))
        def search(self,query):
            selected=boards(self.id)
            if not selected:return result(self.id,error='Shto të paktën një faqe punëdhënësi te Burimet.')
            jobs=[];errors=[]
            for board in selected:
                try:jobs.extend(board_jobs(self.id,board))
                except (httpx.HTTPError,ValueError,TypeError,KeyError) as e:errors.append(error(e))
            # Never label partial board coverage a complete successful collection.
            if errors:return result(self.id,error=f'{len(errors)}/{len(selected)} faqe punëdhënësi dështuan: '+errors[0])
            return result(self.id,relevant(jobs,query))

    class HNHiring(base):
        id='hn_hiring';name='HN Who is hiring';note='Punësim nga kompanitë · thread mujor publik'
        def search(self,query):
            try:
                story=api._hn_latest_thread('Ask HN: Who is hiring?')
                if not story:return result(self.id,[])
                data=api._get(f'https://hn.algolia.com/api/v1/items/{story}')
                jobs=[]
                for entry in data.get('children',[]):
                    item=api._hn_comment_to_item(entry,self.id)
                    if item and not re.match(r'(?i)^[\W_]*(seeking work|for hire)',item['description']):jobs.append(item)
                return result(self.id,relevant(jobs,query))
            except (httpx.HTTPError,ValueError,TypeError,KeyError) as e:return result(self.id,error=error(e))

    class GoogleJobs(base):
        id='google_jobs';name='Google Jobs · SerpApi';note='Kërkim Google Jobs · kërkon çelës SerpApi';requires_key=True
        def is_configured(self):return bool(repo.get_setting('serpapi_key'))
        def search(self,query):
            if not self.is_configured():return result(self.id,error='Konfiguro çelësin SerpApi te Burimet.')
            if not query.what:return result(self.id,error='Shkruaj profesionin ose përdor kërkimin sipas profilit.')
            try:
                params={'engine':'google_jobs','api_key':repo.get_setting('serpapi_key'),
                    'q':' '.join([query.what,query.where]),'gl':query.country or 'de','hl':{'de':'de','at':'de','ch':'de','fr':'fr','nl':'nl','pl':'pl','it':'it','es':'es'}.get(query.country,'en')}
                response=usage_runtime.http_response('search-google-jobs','serpapi',
                    lambda:httpx.get('https://serpapi.com/search',params=params,timeout=25),
                    fingerprint={'engine':'google_jobs','query_hash':hashlib.sha256(params['q'].encode()).hexdigest(),'country':query.country},max_redirects=0)
                response.raise_for_status();data=response.json()
                if data.get('error'):return result(self.id,error='Google Jobs nuk e përfundoi kërkimin. Kontrollo çelësin ose kuotën.')
                jobs=[]
                for j in data.get('jobs_results',[]):
                    link=next((x.get('link') for x in j.get('apply_options',[]) if public_url(x.get('link'))),'')
                    if not link:continue
                    jobs.append({'source':self.id,'external_id':j.get('job_id',''),'url':link,'title':j.get('title',''),
                        'company':j.get('company_name',''),'location':j.get('location',''),'description':j.get('description',''),
                        'tags':j.get('extensions',[]),'posted_at':'','remote':False})
                return result(self.id,jobs)
            except (httpx.HTTPError,ValueError,TypeError,KeyError) as e:return result(self.id,error=error(e))

    class GoogleWeb(base):
        id='google_web';name='Google Web · SerpApi';note='Njoftime të indeksuara në web · kërkon çelës SerpApi';requires_key=True
        def is_configured(self):return bool(repo.get_setting('serpapi_key'))
        def search(self,query):
            if not self.is_configured():return result(self.id,error='Konfiguro çelësin SerpApi te Burimet.')
            try:
                params={
                    'engine':'google','api_key':repo.get_setting('serpapi_key'),
                    'q':web_discovery.query_text(query),'gl':query.country or 'de',
                    'hl':'de' if query.country in ('','de','at','ch') else 'en'}
                response=usage_runtime.http_response('search-google-web','serpapi',
                    lambda:httpx.get('https://serpapi.com/search',params=params,timeout=httpx.Timeout(60,connect=10)),
                    fingerprint={'engine':'google','query_hash':hashlib.sha256(params['q'].encode()).hexdigest(),'country':query.country},max_redirects=0)
                response.raise_for_status();data=response.json()
                if not isinstance(data,dict) or data.get('error'):
                    return result(self.id,error='Google Web nuk e përfundoi kërkimin. Kontrollo çelësin ose kuotën.')
                entries=data.get('organic_results',[])
                if not isinstance(entries,list):raise ValueError('Invalid results')
                jobs=[web_discovery.preview(j,self.id,query) for j in entries[:min(10,query.limit)] if isinstance(j,dict)]
                return result(self.id,[j for j in jobs if j])
            except (httpx.HTTPError,ValueError,TypeError,KeyError) as e:return result(self.id,error=error(e))

    class BraveWeb(base):
        id='brave_web';name='Web · Brave';note='Punë dhe kërkesa projektesh në web · kërkon çelës';requires_key=True
        def is_configured(self):return bool(repo.get_setting('brave_api_key'))
        def search(self,query):
            if not self.is_configured():return result(self.id,error='Konfiguro çelësin Brave te Burimet.')
            try:
                params={
                    'q':web_discovery.query_text(query),'country':(query.country or 'de').upper(),'search_lang':{'de':'de','at':'de','ch':'de','fr':'fr','nl':'nl','pl':'pl','it':'it','es':'es'}.get(query.country,'en'),'count':min(20,query.limit)}
                response=usage_runtime.http_response('search-brave-web','brave',
                    lambda:httpx.get('https://api.search.brave.com/res/v1/web/search',params=params,
                        headers={'X-Subscription-Token':repo.get_setting('brave_api_key'),'Accept':'application/json'},timeout=25),
                    fingerprint={'query_hash':hashlib.sha256(params['q'].encode()).hexdigest(),'country':query.country},max_redirects=0)
                response.raise_for_status();data=response.json()
                if not isinstance(data,dict) or data.get('error'):raise ValueError('Invalid response')
                web=data.get('web') or {}
                if not isinstance(web,dict):raise ValueError('Invalid web response')
                entries=web.get('results',[])
                if not isinstance(entries,list):raise ValueError('Invalid results')
                jobs=[web_discovery.preview(j,self.id,query) for j in entries[:min(20,query.limit)] if isinstance(j,dict)]
                return result(self.id,[j for j in jobs if j])
            except (httpx.HTTPError,ValueError,TypeError,KeyError) as e:return result(self.id,error=error(e))

    return [RemoteOK(),HNHiring(),Board('greenhouse'),Board('lever'),Board('ashby'),FederalFeed(),FederalFeed(True),GoogleJobs(),GoogleWeb(),BraveWeb()]
