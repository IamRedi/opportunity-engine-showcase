import json
import httpx
import pytest
from fastapi.testclient import TestClient
import app,config,context,repository as repo,pilot_users,extended_sources,connectors,discovery,source_http

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'db.sqlite');monkeypatch.setattr(config,'DATA_DIR',tmp_path)
    monkeypatch.setenv('APP_ENV','test');monkeypatch.delenv('LOCAL_PILOT_USERS',raising=False)
    repo.init()


def pilot(monkeypatch):
    monkeypatch.setenv('APP_ENV','local');monkeypatch.setenv('LOCAL_PILOT_USERS','1')
    return pilot_users.provision()


def test_pilot_switch_isolates_data_and_rejects_stale_tabs(monkeypatch):
    friend=pilot(monkeypatch)
    repo.update_profile_fields({'name':'Original Owner','phone':'111'})
    _,oid=repo.upsert_opportunity_full({'source':'fixture','url':'https://jobs.example/original','title':'Owner job','description':'Private'})
    client=TestClient(app.app)
    assert client.get('/profile').status_code==200
    assert client.post('/api/pilot/switch',json={'user_id':friend}).status_code==409
    assert client.post('/api/pilot/switch',json={'user_id':friend},headers={'X-Pilot-Actor':'1'}).status_code==200
    html=client.get('/profile').text
    assert 'Original Owner' not in html and 'value="111"' not in html
    assert client.get(f'/api/jobs/{oid}/panel').status_code==404
    assert client.post('/profile',data={'name':'Overwrite'},headers={'X-Pilot-Actor':'1'}).status_code==409
    assert client.post('/settings/discovery',data={'brave_api_key':'blocked'},headers={'X-Pilot-Actor':str(friend)}).status_code==403
    assert not repo.get_setting('brave_api_key')
    # Ordinary form and multipart paths preserve downstream request bodies.
    r=client.post('/profile/source-boards',data={'_pilot_actor':str(friend),'greenhouse':'friend-company'},follow_redirects=False)
    assert r.status_code==303
    r=client.post('/profile/source-boards',files={'_pilot_actor':(None,str(friend)),'ashby':(None,'friend-company')},follow_redirects=False)
    assert r.status_code==303
    with context.run_as(pilot_users.actor(client.cookies.get(pilot_users.COOKIE))):
        assert extended_sources.boards('ashby')==['friend-company']
        assert repo.get_profile()['name']==''
    assert extended_sources.boards('ashby')==[]
    assert repo.get_profile()['name']=='Original Owner'


def test_pilot_never_available_in_production_or_remote_requests(monkeypatch):
    friend=pilot(monkeypatch);client=TestClient(app.app)
    assert client.post('/api/pilot/switch',json={'user_id':friend},headers={'X-Pilot-Actor':'1','Origin':'https://outside.example'}).status_code==403
    assert TestClient(app.app,base_url='http://outside.example').get('/profile').status_code==403
    monkeypatch.setenv('APP_ENV','production')
    assert client.post('/api/pilot/switch',json={'user_id':friend}).status_code==401
    assert not pilot_users.enabled()


def test_pilot_tamper_and_unknown_user_rejected(monkeypatch):
    pilot(monkeypatch)
    with pytest.raises(ValueError):pilot_users.actor('2:9999999999:forged')
    client=TestClient(app.app)
    assert client.post('/api/pilot/switch',json={'user_id':9999},headers={'X-Pilot-Actor':'1'}).status_code==404


def test_sources_are_scoped_and_board_ids_cannot_be_urls():
    c=TestClient(app.app)
    assert c.post('/profile/source-boards',data={'greenhouse':'https://127.0.0.1'}).status_code==422
    repo.set_profile_section('source_boards',[{'provider':'greenhouse','board':'test-board'}])
    assert connectors.REGISTRY['greenhouse'].is_configured()
    with context.run_as(context.ActorContext(user_id=2)):
        assert not connectors.REGISTRY['greenhouse'].is_configured()


def test_board_normalization_skips_unlisted_ashby_and_preserves_unknowns(monkeypatch):
    monkeypatch.setattr(source_http,'get_json',lambda *a,**k:{'jobs':[
        {'isListed':False,'jobUrl':'https://jobs.example/private','title':'Private'},
        {'isListed':True,'jobUrl':'https://jobs.example/public','title':'Public','descriptionPlain':'Text','compensation':{'scrapeableCompensationSalarySummary':None}}]})
    jobs=extended_sources.board_jobs('ashby','test')
    assert len(jobs)==1 and jobs[0]['title']=='Public' and jobs[0]['location']==''
    assert jobs[0]['salary_text']==''
    assert repo.upsert_opportunity_full(jobs[0])[0]=='new'


def test_partial_board_failure_is_visible(monkeypatch):
    repo.set_profile_section('source_boards',[{'provider':'greenhouse','board':'one'},{'provider':'greenhouse','board':'two'}])
    def fetch(provider,board):
        if board=='two':raise httpx.ConnectError('private secret URL')
        return []
    monkeypatch.setattr(extended_sources,'board_jobs',fetch)
    result=connectors.REGISTRY['greenhouse'].search(connectors.SearchQuery())
    assert '1/2' in result.error and 'private secret' not in result.error


def test_product_plan_uses_owned_capabilities_and_only_demand_sources(monkeypatch):
    repo.save_products([{'name':'Product','capabilities':['Booking system','Invoicing']}])
    monkeypatch.setattr(connectors.REGISTRY['brave_web'],'is_configured',lambda:True)
    plan=discovery.plan('product_lead',product_id='0',where='Essen')
    assert plan['query']=='Booking system Invoicing'
    assert {t['source'] for t in plan['tasks']}=={'hn_leads','bund_tenders','brave_web'}
    with context.run_as(context.ActorContext(user_id=2)):
        with pytest.raises(ValueError):discovery.plan('product_lead',product_id='0')


def test_brave_rejects_seller_ad_and_marks_demand_as_unverified(monkeypatch):
    repo.set_setting('brave_api_key','fixture-key')
    def get(url,**kwargs):
        assert kwargs['headers']['X-Subscription-Token']=='fixture-key'
        assert 'Essen' in kwargs['params']['q']
        return httpx.Response(200,request=httpx.Request('GET',url),json={'web':{'results':[
            {'title':'I will build a website','description':'Buy my service','url':'https://fiverr.com/seller/service'},
            {'title':'Software gesucht','description':'Wir suchen eine Buchungslösung','url':'https://buyer.example/request'}]}})
    monkeypatch.setattr(httpx,'get',get)
    result=connectors.REGISTRY['brave_web'].search(connectors.SearchQuery(what='booking',where='Essen',kinds=('product_lead',)))
    assert not result.error and len(result.items)==1
    assert result.items[0]['opportunity_type']=='Për t’u verifikuar'
    assert result.items[0]['location']==''


def test_google_jobs_does_not_invent_apply_url_or_expose_key(monkeypatch):
    repo.set_setting('serpapi_key','fixture-secret')
    def get(url,**kwargs):
        return httpx.Response(200,request=httpx.Request('GET',url),json={'jobs_results':[
            {'title':'No URL'}, {'title':'Developer','apply_options':[{'link':'https://employer.example/job'}]}]})
    monkeypatch.setattr(httpx,'get',get)
    result=connectors.REGISTRY['google_jobs'].search(connectors.SearchQuery(what='Developer'))
    assert len(result.items)==1 and result.items[0]['title']=='Developer'
    assert 'fixture-secret' not in TestClient(app.app).get('/profile').text


def test_search_filters_forwarded(monkeypatch):
    seen=[]
    monkeypatch.setattr(connectors.search_apis,'_get',lambda url,params=None:seen.append(params) or {'jobs':[]})
    connectors.REGISTRY['remotive'].search(connectors.SearchQuery(what='Python'))
    connectors.REGISTRY['jobicy'].search(connectors.SearchQuery(what='Python'))
    assert seen[0]['search']=='Python' and seen[1]['tag']=='Python'


def test_country_choice_is_not_silently_changed_to_germany(monkeypatch):
    repo.set_setting('adzuna_app_id','fixture');repo.set_setting('adzuna_app_key','fixture')
    repo.update_profile_fields({'location':'Essen'})
    seen={}
    monkeypatch.setattr(connectors.search_apis,'adzuna',lambda **kwargs:seen.update(kwargs) or [])
    connectors.REGISTRY['adzuna'].search(connectors.SearchQuery(what='Python',where='',country='nl'))
    assert seen['country']=='nl' and seen['where']==''


def test_project_matching_uses_stated_description_without_claiming_ready_for_sale():
    import product_fit
    repo.set_profile_section('projects',[{'title':'My App','description':'Booking website with automation','stack':'Python'}])
    before=repo.get_profile_section('projects')[0]
    plan=discovery.plan('product_lead',product_id='project:0')
    assert plan['query']=='Booking website with automation'
    fit=product_fit.evaluate({'title':'Booking website needed','description':'We need booking automation'})
    assert fit['best']['product_name']=='My App'
    assert any('shitje' in x for x in fit['best']['unknown'])
    assert repo.get_profile_section('projects')[0]==before and repo.list_products()==[]


def test_official_feed_separates_procurement_from_employment(monkeypatch):
    def get(url,**kwargs):
        return httpx.Response(200,request=httpx.Request('GET',url),text='<rss><channel><item><title>IT project</title><link>http://www.bundesnetzagentur.de/job</link><description>Details</description><pubDate>Sun, 06 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')
    monkeypatch.setattr(httpx,'get',get)
    for sid,kind in [('bund_jobs','job'),('bund_tenders','product_lead')]:
        item=connectors.REGISTRY[sid].search(connectors.SearchQuery()).items[0]
        assert item['kind']==kind and item['posted_at']=='2026-09-06 10:00'
        assert item['url']=='https://www.bundesnetzagentur.de/job'


def test_local_feed_urls_do_not_leak_between_pilot_users():
    repo.set_setting('local_feed_urls','https://owner.example/feed')
    assert repo.local_feed_urls()=='https://owner.example/feed'
    with context.run_as(context.ActorContext(user_id=2)):
        assert repo.local_feed_urls()==''
        repo.set_setting('local_feed_urls','https://friend.example/feed',ctx=context.current())
        assert repo.local_feed_urls()=='https://friend.example/feed'
    assert repo.local_feed_urls()=='https://owner.example/feed'


def test_persistence_failure_is_reported_without_leaving_running_search(monkeypatch):
    import sqlite3
    monkeypatch.setattr(connectors.REGISTRY['remoteok'],'search',lambda query:connectors.ConnectorResult('remoteok',[{'url':'https://example.test/job'}]))
    monkeypatch.setattr(repo,'upsert_opportunity_full',lambda item:(_ for _ in ()).throw(sqlite3.IntegrityError('sensitive detail')))
    result=connectors.run_source_search('remoteok')
    assert not result['ok'] and 'sensitive' not in result['error']
    with repo.db() as c:
        row=c.execute('select status,finished_at from search_runs where id=?',(result['run_id'],)).fetchone()
    assert row['status']=='error' and row['finished_at']


def test_real_minijob_appears_in_local_view_without_becoming_product_demand():
    _,oid=repo.upsert_opportunity_full({'source':'adzuna','url':'https://employer.example/minijob','title':'Aushilfe Minijob Essen','description':'Computer support','company':'Employer','kind':'job'})
    assert oid in [j['id'] for j in repo.list_opportunities(kind='local_job')]
    assert oid not in [j['id'] for j in repo.list_opportunities(kind='product_lead')]
