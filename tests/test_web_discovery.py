"""Synthetic provider → persistence → localized drawer; never calls paid APIs."""
import httpx
import pytest
from fastapi.testclient import TestClient

import app, config, connectors, context, discovery, i18n, repository as repo

SITE = 'kleinanzeigen.de/s-anzeige/'
URL = 'https://www.kleinanzeigen.de/s-anzeige/gassi-hilfe/123-107-1'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'web.db')
    monkeypatch.setenv('APP_ENV', 'test')
    repo.init()


def configured():
    repo.set_setting('serpapi_key', 'fixture-private-key')


def fixture_google(monkeypatch, entries=None):
    calls=[]
    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert url == 'https://serpapi.com/search'
        assert kwargs['params']['engine'] == 'google'
        return httpx.Response(200,request=httpx.Request('GET',url),json={'organic_results': entries if entries is not None else [
            {'title':'Gassihilfe gesucht', 'link':URL, 'snippet':'Suche zuverlässige Hilfe für meinen Hund in Essen.'},
            {'title':'Ich suche Arbeit', 'link':URL+'2','snippet':'Minijob als Tierbetreuer gesucht'},
            {'title':'Gassihilfe gesucht', 'link':'https://kleinanzeigen.de.evil.example/s-anzeige/fake','snippet':'Minijob'},
            {'title':'All jobs','link':'https://www.kleinanzeigen.de/s-jobs/c102','snippet':'Jobs in Essen'},
        ]})
    monkeypatch.setattr(httpx, 'get', get)
    return calls


def test_target_plan_only_one_provider_preserves_categories_and_country(monkeypatch):
    configured();repo.set_setting('brave_api_key','another-fixture')
    p=discovery.plan('local_job',where='Essen',categories=['dog_walking','cat_sitting'],web_target=SITE)
    assert len(p['tasks'])==2
    assert {t['source'] for t in p['tasks']}=={'google_web'}
    assert all(t['site']==SITE and t['where']=='Essen' for t in p['tasks'])
    assert len([t for t in discovery.plan('job',what='Engineer')['tasks'] if t['source'] in ('brave_web','google_web')])==1


def test_missing_credentials_and_invalid_targets_fail_before_http(monkeypatch):
    monkeypatch.setattr(httpx,'get',lambda *a,**kw:pytest.fail('No network'))
    with TestClient(app.app) as client:
        r=client.post('/api/discovery/plan',json={'kind':'local_job','web_target':SITE})
        assert r.status_code==422 and 'SerpApi' in r.json()['error']
        for target,country,kind in [(SITE,'ch','local_job'),(SITE,'de','product_lead'),('http://127.0.0.1','de','job'),(['bad'],'de','job')]:
            assert client.post('/api/discovery/plan',json={'web_target':target,'country':country,'kind':kind}).status_code==422
        assert client.post('/api/discovery/run',json={'source':'adzuna','site':SITE,'kinds':['local_job']}).status_code==422


@pytest.mark.parametrize('language',['sq','en','de'])
def test_import_dedup_local_lens_and_localized_panel(language,monkeypatch):
    configured();calls=fixture_google(monkeypatch)
    with TestClient(app.app) as client:
        task=client.post('/api/discovery/plan',json={'kind':'local_job','web_target':SITE,'where':'Essen','categories':['dog_walking']}).json()['tasks'][0]
        r=client.post('/api/discovery/run',json=task).json()
        assert r['ok'] and r['new']==1 and r['discovered']==1
        again=client.post('/api/discovery/run',json=task).json()
        assert again['new']==0 and again['dup']==1
        rows=repo.list_opportunities(kind='local_job')
        assert len(rows)==1
        item=rows[0]
        assert item['source_url']==URL and item['location']==''
        assert item['kd']['verification_status']=='search_preview'
        assert item['kd']['source_domain']=='www.kleinanzeigen.de'
        panel=client.get(f"/api/jobs/{item['id']}/panel?lang={language}").text
        assert URL in panel
        for key in ('Rezultat nga kërkimi në web','Fragmenti nga burimi'):
            assert i18n.CATALOG.get(key,{}).get(language,key) in panel
        assert 'WEB SEARCH PREVIEW' not in panel and 'fixture-private-key' not in panel
        assert 'title="'+i18n.CATALOG.get('Përputhja:',{}).get(language,'Përputhja:')+' 0%"' not in panel
        board=client.get('/?mode=local&source=google_web&lang='+language).text
        assert i18n.CATALOG.get('Data e postimit nuk është verifikuar',{}).get(language,'Data e postimit nuk është verifikuar') in board
        assert 'Google_web' not in board
        with context.run_as(context.ActorContext(user_id=2,tenant_id=2)):
            assert repo.list_opportunities(kind='local_job')==[]
    assert len(calls)==2
    q=calls[0][1]['params']['q']
    assert 'site:'+SITE in q and 'Gassi gehen' in q and 'Essen' in q


@pytest.mark.parametrize('body',[{'error':'fixture-private-key'}, {'organic_results':{}}, []])
def test_provider_error_not_successful_empty_search(body,monkeypatch):
    configured()
    monkeypatch.setattr(httpx,'get',lambda url,**kw:httpx.Response(200,request=httpx.Request('GET',url),json=body))
    result=discovery.run_task({'source':'google_web','kinds':['job'],'what':'Engineer'})
    assert not result['ok'] and 'fixture-private-key' not in result['error']


def test_no_keys_no_calls_and_brave_fallback(monkeypatch):
    monkeypatch.setattr(httpx,'get',lambda *a,**kw:pytest.fail('No HTTP'))
    assert connectors.REGISTRY['google_web'].search(connectors.SearchQuery(what='Jobs')).error
    repo.set_setting('brave_api_key','fixture')
    p=discovery.plan('local_job',categories=['dog_walking'],web_target=SITE)
    assert [t['source'] for t in p['tasks']]==['brave_web']
    broad=discovery.plan('local_job',categories=[],web_target=SITE)
    assert len(broad['tasks'])==1 and broad['tasks'][0]['source']=='brave_web'


def test_preview_does_not_change_full_listing_score_or_user_status(monkeypatch):
    configured();fixture_google(monkeypatch)
    _,oid=repo.upsert_opportunity_full({'source':'fixture','url':URL,'title':'Pet care job',
        'description':'Complete original listing','company':'Employer','location':'Essen','remote':True})
    before=repo.get_opportunity(oid)
    result=discovery.run_task({'source':'google_web','kinds':['local_job'],'site':SITE,'what':'Gassi'})
    assert result['dup']==1
    after=repo.get_opportunity(oid)
    for key in ('description','score','match_factors','status','source'):
        assert after[key]==before[key]
    assert len(repo.list_opportunities(kind='local_job'))==1


def test_preview_freshness_and_workmode_remain_unknown_after_persistence(monkeypatch):
    import matching
    configured();fixture_google(monkeypatch)
    r=discovery.run_task({'source':'google_web','kinds':['local_job'],'site':SITE,'what':'Gassi'})
    item=repo.get_opportunity(r['new_ids'][0])
    assert item['discovered_at']
    assert matching._age_days(item) is None
    # OE-007E: funksionet e faktorëve marrin (PreparedOp, PreparedContext).
    p = matching.PreparedOp(item)
    pctx = matching.prepare_context({}, {"onsite_ok": True})
    assert matching._workmode_factor(p, pctx, 1).status == 'unknown'
