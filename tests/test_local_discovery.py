import pytest
from fastapi.testclient import TestClient
import app,config,discovery
import repository as repo

@pytest.fixture(autouse=True)
def isolate(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'local-discovery.db');repo.init()
    for source in ('adzuna','google_jobs','brave_web'):
        monkeypatch.setattr(discovery.connectors.REGISTRY[source],'is_configured',lambda:True)
    monkeypatch.setattr(discovery.connectors.REGISTRY['local_feed'],'is_configured',lambda:False)

def test_categories_generate_specific_queries_without_changing_profile():
    before=repo.get_profile()
    p=discovery.plan('local_job',where='Essen',categories=['dog_walking','senior_companion'])
    assert len(p['tasks'])==6
    assert {t['category'] for t in p['tasks']}=={'dog_walking','senior_companion'}
    assert any(t['what']=='Gassi gehen' for t in p['tasks'])
    assert all(t['where']=='Essen' for t in p['tasks'])
    assert before==repo.get_profile()

def test_invalid_and_excess_categories_rejected():
    for categories in ('dog_walking',['invented'],list(config.LOCAL_CATEGORIES[:4])):
        with pytest.raises(ValueError):discovery.plan('local_job',categories=categories)

def test_explicit_empty_means_general_search_and_other_countries_use_english():
    assert discovery.plan('local_job',categories=[])['categories']==[]
    p=discovery.plan('local_job',country='gb',categories=['tutoring'])
    assert any(t['what']=='tutoring' for t in p['tasks'])

def test_profile_preferences_fallback(monkeypatch):
    monkeypatch.setattr(repo,'get_local_prefs',lambda **kwargs:{'categories':['cat_sitting'],'base_location':'Dortmund'})
    p=discovery.plan('local_job')
    assert p['categories']==['cat_sitting'] and p['where']=='Dortmund'
    assert any(t['what']=='Katzenbetreuung' for t in p['tasks'])

@pytest.mark.parametrize('language',['sq','en','de'])
def test_local_page_and_api(language):
    with TestClient(app.app) as c:
        r=c.get('/discover',params={'kind':'local_job','lang':language})
        assert r.status_code==200
        assert 'name="categories" value="dog_walking"' in r.text
        assert 'name="categories" value="babysitting"' in r.text
        assert 'https://www.pawshake.de/become-a-sitter' in r.text
        r=c.post('/api/discovery/plan',json={'kind':'local_job','categories':['house_sitting'],'where':'Essen'})
        assert r.status_code==200 and r.json()['categories']==['house_sitting']


def test_local_salary_does_not_inherit_professional_floor(monkeypatch):
    calls=[]
    monkeypatch.setattr(repo,'get_filters',lambda:{'salary_min':4000,'keywords':[]})
    monkeypatch.setattr(discovery.connectors.search_apis,'adzuna',lambda **kw:calls.append(kw) or [])
    connector=discovery.connectors.REGISTRY['adzuna']
    connector.search(discovery.connectors.SearchQuery(what='Haushaltshilfe',kinds=('local_job',)))
    connector.search(discovery.connectors.SearchQuery(what='Engineer',kinds=('job',)))
    assert [c['salary_min'] for c in calls]==[0,4000]


def test_local_search_visible_on_first_and_duplicate_import(monkeypatch):
    connectors=discovery.connectors
    item={'source':'adzuna','url':'https://example.org/local-care','title':'Tierbetreuung','description':'Care for pets','company':'Test','location':'Essen'}
    monkeypatch.setattr(connectors.REGISTRY['adzuna'],'search',lambda q:connectors.ConnectorResult('adzuna',[item]))
    connectors.run_source_search('adzuna',connectors.SearchQuery(kinds=('job',)))
    assert not repo.list_opportunities(kind='local_job')
    result=connectors.run_source_search('adzuna',connectors.SearchQuery(kinds=('local_job',)))
    assert result['ok'] and result['dup']==1
    rows=repo.list_opportunities(kind='local_job')
    assert len(rows)==1 and rows[0]['kind']=='job'
    assert not rows[0].get('opportunity_type')


def test_first_local_import_attaches_lens_before_connection_closes(monkeypatch):
    import classifier
    original=classifier.attach_lenses
    def checked(opp_id,op,connection=None):
        assert connection is not None
        return original(opp_id,op,connection=connection)
    monkeypatch.setattr(classifier,'attach_lenses',checked)
    repo.upsert_opportunity_full({'url':'https://example.org/first-local','title':'Haushaltshilfe','_local_search':True})
    assert len(repo.list_opportunities(kind='local_job'))==1
