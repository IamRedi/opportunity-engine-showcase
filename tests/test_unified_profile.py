import io
import json

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

import app
import config
import context
import cv_ai
import profile_context
import profile_intake as intake
import profile_intelligence as intelligence
import profile_preferences
import profile_resume
import repository as repo
import storage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'profile.db')
    monkeypatch.setattr(storage,'get_storage',lambda:storage.LocalStorage(tmp_path/'uploads'))
    repo.init()


def fake_extraction(monkeypatch):
    def extract(text, **kwargs):
        items=[{'section':'contact','fields':{'name':'Test Owner'}},
               {'section':'interests','fields':{'name':'hiking'}},
               {'section':'personal','fields':{'nationality':'Albanian','age':'35'}},
               {'section':'preferences','fields':{'transport':'no car','travel':'30 km'}}]
        suggestions,rejected=cv_ai.checked_suggestions({'items':items},text)
        return {'ok':True,'suggestions':suggestions,'rejected':rejected}
    monkeypatch.setattr(cv_ai,'extract',extract)


TEXT='Test Owner, Albanian, 35. I enjoy hiking, no car, within 30 km.'


def test_description_preview_is_not_a_profile_change_and_commit_is_reviewed(monkeypatch):
    fake_extraction(monkeypatch)
    result=intake.preview(TEXT)
    assert result['ok']
    assert not repo.get_profile()['name']
    assert not repo.get_profile_section('interests')[0]
    items=result['preview']['items']
    # User removes optional nationality/age and corrects the displayed name.
    selected=[i for i in items if i['section']!='personal']
    selected[0]['fields']['name']='Reviewed Owner'
    out=intake.commit(result['preview']['id'],selected)
    assert out['ok'] and repo.get_profile()['name']=='Reviewed Owner'
    assert repo.get_profile_section('interests')[0][0]['name']=='hiking'
    assert repo.get_profile_section('personal')[0]==[]
    assert repo.get_local_prefs()['no_car'] is True
    assert repo.get_local_prefs()['radius_km']==30
    assert not intake.commit(result['preview']['id'],selected)['ok']


def test_changed_profile_and_other_user_cannot_apply_preview(monkeypatch):
    fake_extraction(monkeypatch);result=intake.preview(TEXT)
    with context.run_as(context.ActorContext(user_id=2)):
        assert not intake.commit(result['preview']['id'],result['preview']['items'])['ok']
    repo.update_profile_fields({'phone':'new phone'})
    assert not intake.commit(result['preview']['id'],result['preview']['items'])['ok']
    assert repo.get_profile()['phone']=='new phone'


def test_invalid_manual_field_rolls_back_entire_commit(monkeypatch):
    fake_extraction(monkeypatch);result=intake.preview(TEXT)
    items=result['preview']['items']+[{'section':'contact','fields':{'unexpected':'bad'}}]
    with pytest.raises(ValueError):intake.commit(result['preview']['id'],items)
    assert not repo.get_profile()['name']
    assert not repo.get_profile_section('interests')[0]


def test_account_prefill_never_asks_for_existing_profile_contact():
    with repo.db() as c:c.execute("UPDATE users SET display_name='Account Name',email='account@example.org' WHERE id=1")
    assert intake.defaults()['email']=='account@example.org'
    repo.update_profile_fields({'email':'preferred@example.org','phone':'123'})
    assert intake.defaults()['email']=='preferred@example.org'
    assert intake.defaults()['phone']=='123'


def test_first_preview_for_new_account_uses_stable_revision(monkeypatch):
    fake_extraction(monkeypatch)
    with context.run_as(context.ActorContext(user_id=2)):
        result=intake.preview(TEXT)
        assert result['ok']
        assert intake.commit(result['preview']['id'],result['preview']['items'])['ok']
        assert repo.get_profile()['name']=='Test Owner'
    assert not repo.get_profile()['name']


def test_application_learning_requires_saved_human_edit_and_owner(monkeypatch):
    fake_extraction(monkeypatch)
    _,opp=repo.upsert_opportunity_full({'title':'Fixture job','url':'https://fixture.example/job','kind':'job'})
    app_id=repo.create_application(opp,'cover_letter','other','en','Fixture',TEXT,{})
    assert not intake.application_preview(app_id)['ok']
    repo.add_application_version(app_id,'edited',TEXT)
    assert intake.application_preview(app_id)['ok']
    assert not repo.get_profile()['name']
    with context.run_as(context.ActorContext(user_id=2)):
        assert not intake.application_preview(app_id)['ok']


def test_personal_data_is_not_a_career_signal_or_application_fact():
    repo.update_profile_fields({'summary':'Teaching German'})
    token,text,_=intelligence.snapshot()
    repo.set_profile_section('personal',[{'age':'50','nationality':'private citizenship'}])
    assert intelligence.snapshot()==(token,text,None)
    assert 'private citizenship' not in profile_context.fact_values()
    repo.set_profile_section('interests',[{'name':'hiking'}])
    ctx=profile_context.build_context()
    assert not ctx['skills'] and ctx['interests'][0]['name']=='hiking'
    assert any(f['key']=='interest:hiking' and f['kind']=='interest' for f in ctx['facts'])


def test_basic_language_level_cannot_become_a_teaching_direction():
    concepts=[{'category':'role','labels':{'en':'English teacher'}},
              {'category':'role','labels':{'en':'German learning support'}},
              {'category':'skill','labels':{'en':'Basic English'}}]
    kept,rejected=intelligence.filter_language_roles(concepts,[{'name':'Englisch','level':'A2'},{'name':'Deutsch','level':'B1/B2'}])
    assert kept==concepts[1:] and rejected==1


def test_semantic_audit_removes_unsupported_concepts_and_rejects_unknown_ids(monkeypatch):
    import ai_service
    output={'supported_ids':['grounded']}
    class Provider:
        def __init__(self,cfg):pass
        def complete(self,prompt):return json.dumps(output),{}
    monkeypatch.setitem(ai_service._PROVIDERS,'fixture',Provider)
    base={'category':'role','labels':{'en':'Pet care'},'rationale':{'en':'Enjoys pets'},'basis':'interest','evidence':['pets']}
    concepts=[dict(base,id='grounded'),dict(base,id='irrelevant')]
    kept,rejected=intelligence.audit_concepts(concepts,'pets',{'provider':'fixture'})
    assert kept==concepts[:1] and rejected==1
    output['supported_ids']=['invented']
    with pytest.raises(ValueError):intelligence.audit_concepts(concepts,'pets',{'provider':'fixture'})


def test_education_coverage_has_its_own_evidence_and_stays_exploratory(monkeypatch):
    import ai_service
    repo.set_profile_section('education',[{'title':'German studies'}])
    class Provider:
        def __init__(self,cfg):pass
        def complete(self,prompt):
            assert 'EDUCATION:\nGerman studies' in prompt
            return json.dumps({'concepts':[{'category':'role','labels':{k:'Language support' for k in ('sq','en','de')},
                'rationale':{k:'German studies support learning assistance' for k in ('sq','en','de')},
                'checks':{k:'Verify language and requirements' for k in ('sq','en','de')},
                'basis':'experience','adjacent':True,'terms':['language support'],'evidence_ids':['E1']}]}),{}
    monkeypatch.setitem(ai_service._PROVIDERS,'fixture',Provider)
    concepts,rejected=intelligence.adjacent_coverage(profile_context.build_context(),{'provider':'fixture'})
    assert len(concepts)==1 and rejected==0
    assert concepts[0]['evidence']==['German studies'] and concepts[0]['adjacent']
    assert not repo.get_profile_section('experience')[0]


def test_teaching_support_requires_relevant_studies_and_retains_eligibility_checks():
    ctx=profile_context.build_context()
    ctx['education']=[{'title':'Studium Deutsche Sprache'},{'title':'Master im Lehramt'}]
    ctx['languages']=[{'name':'Deutsch','level':'B1/B2'}]
    roles=intelligence.education_connections(ctx)
    assert len(roles)==1 and roles[0]['adjacent']
    assert roles[0]['labels']['de']=='Lernbegleitung Deutsch'
    assert 'recognition' in roles[0]['checks']['en']
    assert roles[0]['evidence']==['Studium Deutsche Sprache','Master im Lehramt']
    ctx['education']=ctx['education'][:1]
    assert not intelligence.education_connections(ctx)


def test_no_licence_to_car_inference_and_salary_reuse():
    assert profile_preferences.local_values([{'transport':'Führerschein B'}])=={}
    out=profile_preferences.local_values([{'transport':'Ich habe kein Auto','salary':'20 €/h','travel':'30 km, 45 min','availability':'weekends'}])
    assert out=={'no_car':True,'min_hourly_rate':20.0,'radius_km':30,'max_travel_min':45,'available_days':'weekends'}
    repo.set_profile_section('preferences',[{'salary':'20 €/h'}])
    repo.set_profile_section('local',[{'min_hourly_rate':25}])
    assert repo.get_local_prefs()['min_hourly_rate']==25


def test_deep_discovery_reserves_space_for_adjacent_roles(monkeypatch):
    import connectors
    roles=[{'id':str(i),'category':'role','status':'active','adjacent':i>=3,'labels':{'de':f'Role {i}'}} for i in range(8)]
    monkeypatch.setattr(intelligence,'state',lambda:{'stale':False,'concepts':roles})
    monkeypatch.setattr(connectors.REGISTRY['adzuna'],'is_configured',lambda:True)
    queries=[]
    repo.set_profile_section('preferences',[{'travel':'30 km'}])
    radii=[]
    monkeypatch.setattr(connectors,'run_source_search',lambda source,query:queries.append(query.what) or radii.append(query.radius_km) or {'ok':True,'new':0})
    assert intelligence.discover(deep=True)['ok']
    assert len(queries)==6 and queries[:2]==['Role 0','Role 1']
    assert queries[2:]==['Role 3','Role 4','Role 5','Role 6']
    assert radii==[30]*6


def test_adzuna_receives_explicit_radius_in_km(monkeypatch):
    import search_apis
    params=[]
    monkeypatch.setattr(search_apis,'_get',lambda url,p:params.append(p) or {'results':[]})
    search_apis.adzuna('fixture-id','fixture-key',where='Essen',distance=30)
    assert params[0]['where']=='Essen' and params[0]['distance']==30


def test_adjacent_offers_are_owned_and_explain_interests_without_experience(monkeypatch):
    import matching
    signal={'category':'role','status':'active','adjacent':True,'basis':'interest','terms':['pet care'],
            'labels':{'en':'Pet sitter'},'rationale':{'en':'You enjoy pets'},'checks':{'en':'Verify availability'},'evidence':['I enjoy pets']}
    monkeypatch.setattr(intelligence,'state',lambda:{'stale':False,'concepts':[signal]})
    _,own=repo.upsert_opportunity_full({'title':'Pet care','url':'https://jobs.example.org/real','kind':'job'})
    repo.upsert_opportunity_full({'title':'Pet care demo','url':'https://fake.example/demo','kind':'job'})
    with context.run_as(context.ActorContext(user_id=2)):
        repo.upsert_opportunity_full({'title':'Pet care foreign','url':'https://jobs.example.org/foreign','kind':'job'})
    results=intelligence.adjacent_opportunities()['items']
    assert [x['id'] for x in results]==[own]
    assert results[0]['reasons'][0]['checks']['en']=='Verify availability'
    scored=matching.score({'title':'Pet care','kind':'job'},{**repo.get_profile(),'search_signals':[signal]}, {})
    factor=next(f for f in scored.factors if f.id=='career_connections')
    assert factor.weight==.3
    assert not profile_context.build_context()['experience']


def test_resume_uses_reviewed_facts_and_sensitive_fields_are_opt_in():
    repo.update_profile_fields({'name':'Test Dëshmi','title':'Developer','email':'test@example.org','summary':'Reviewed summary'})
    repo.set_profile_section('education',[{'title':'German language studies'}])
    repo.set_profile_section('personal',[{'nationality':'PRIVATE NATIONALITY'}])
    repo.set_profile_section('intelligence',[{'concepts':[{'labels':{'en':'Invented credential'}}]}])
    raw=profile_resume.pdf(include_photo=False)
    text='\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(raw)).pages)
    assert 'Test Dëshmi' in text and 'German language studies' in text
    assert 'PRIVATE NATIONALITY' not in text and 'Invented credential' not in text
    private=profile_resume.pdf(include_personal=True,include_photo=False)
    assert 'PRIVATE NATIONALITY' in '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(private)).pages)
    with context.run_as(context.ActorContext(user_id=2)):
        other=profile_resume.pdf(include_photo=False)
        assert 'Test Dëshmi' not in '\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(other)).pages)


def test_setup_and_resume_routes_render_in_all_languages():
    client=TestClient(app.app)
    for lang in ('sq','en','de'):
        assert client.get('/profile',params={'lang':lang}).status_code==200
        assert client.get('/profile/resume',params={'lang':lang}).status_code==200
    response=client.get('/api/profile/resume.pdf')
    # An ungenerated language edition must never silently return untranslated profile text.
    assert response.status_code==409
    assert response.headers['cache-control']=='private, no-store'
