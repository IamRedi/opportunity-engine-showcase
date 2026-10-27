import io
import json

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import app
import config
import context
import cv_ai
import matching
import profile_context
import profile_intelligence as intelligence
import profile_photo
import repository as repo
import storage


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'career.db')
    monkeypatch.setattr(storage,'get_storage',lambda:storage.LocalStorage(tmp_path/'uploads'))
    repo.init()


def concept(label='Electronics repair technician'):
    return {'category':'role','labels':{'sq':'Teknik riparimi elektronik','en':label,'de':'Elektronik Reparaturtechniker'},
            'terms':['TV repair','Fernsehreparatur','electronics repair'],
            'evidence':['15 years repairing televisions'],
            'rationale':{k:'Practical repair experience supports related service roles.' for k in ('sq','en','de')}}


def provider(monkeypatch,change=None):
    class Provider:
        def __init__(self,cfg): pass
        def complete(self,prompt):
            if prompt.startswith('Audit proposed career concepts'):
                return json.dumps({'supported_ids':[c['id'] for c in json.loads(prompt.split('\nPROPOSED:\n')[1])]}),{}
            if change: change()
            return json.dumps({'concepts':[concept()]}), {}
    monkeypatch.setattr(intelligence.ai_service,'get_config',lambda:{'provider':'fixture','model':'fixture','timeout':20})
    monkeypatch.setitem(intelligence.ai_service._PROVIDERS,'fixture',Provider)
    repo.update_profile_fields({'summary':'15 years repairing televisions'})


def test_derived_concepts_match_synonyms_but_never_become_application_facts(monkeypatch):
    provider(monkeypatch)
    assert intelligence.analyze()['ok']
    p=repo.get_profile()
    assert len(p['search_signals'])==1
    ctx=profile_context.build_context()
    assert 'Electronics repair technician' not in [f['value'] for f in ctx['facts']]
    op={'title':'Fernsehreparatur im Außendienst','kind':'job','description':'','tags':[]}
    r=matching.score(op,p,{})
    factor=next(f for f in r.factors if f.id=='career_connections')
    assert factor.status=='match' and '15 years repairing televisions' in factor.evidence
    no_connection=matching.score({'title':'Accountant','kind':'job'},p,{})
    assert not any(f.id=='career_connections' for f in no_connection.factors)


def test_reject_removes_search_signal_and_reanalysis_keeps_decision(monkeypatch):
    provider(monkeypatch);intelligence.analyze()
    cid=intelligence.state()['concepts'][0]['id']
    assert intelligence.decide(cid,'dismissed')
    assert repo.get_profile()['search_signals']==[]
    intelligence.analyze()
    assert intelligence.state()['concepts'][0]['status']=='dismissed'


def test_profile_changes_invalidate_signals_but_photo_does_not(monkeypatch):
    provider(monkeypatch);intelligence.analyze()
    repo.set_profile_section('avatar',[{'key':'none','version':'test'}])
    assert not intelligence.state()['stale']
    repo.set_profile_section('skills',[{'name':'Python'}])
    assert intelligence.state()['stale'] and not repo.get_profile()['search_signals']


def test_changed_profile_during_ai_call_is_not_overwritten(monkeypatch):
    provider(monkeypatch,lambda:repo.update_profile_fields({'summary':'Updated manual summary'}))
    assert not intelligence.analyze()['ok']
    assert repo.get_profile_section('intelligence')[0]==[]


def test_unverified_evidence_and_malformed_concepts_are_rejected():
    item=concept();item['evidence']=['Invented degree']
    concepts,rejected=intelligence.checked_concepts({'concepts':[item,{'category':'role'}]},'15 years repairing televisions')
    assert not concepts and rejected==2


def test_multi_span_cv_preserves_roles_when_optional_date_is_invalid():
    text='15 years repairing televisions\nTV technician\n\nOther column\nRepair Studio'
    data={'items':[{'section':'experience','fields':{'title':'TV technician','org':'Repair Studio','period':'1990–2025','description':'15 years repairing televisions'},'evidence':['TV technician','Repair Studio','15 years repairing televisions']} ]}
    suggestions,rejected=cv_ai.checked_suggestions(data,text)
    fields=json.loads(suggestions[0]['proposed_value'])
    assert rejected==1 and fields=={'title':'TV technician','org':'Repair Studio','description':'15 years repairing televisions'}


def test_licence_classes_never_enter_year():
    suggestions,_=cv_ai.checked_suggestions({'items':[{'section':'certifications','fields':{'name':'Driving licence','year':'B, BE, C'},'evidence':'Driving licence B, BE, C'}]},'Driving licence B, BE, C')
    assert 'year' not in json.loads(suggestions[0]['proposed_value'])


def photo():
    out=io.BytesIO();Image.new('RGB',(100,150),'green').save(out,'PNG');return out.getvalue()


def test_private_photo_upload_normalizes_and_preserves_previous_on_invalid_input():
    c=TestClient(app.app)
    assert c.post('/api/profile/photo',files={'file':('face.png',photo(),'image/png')}).status_code==200
    stored=c.get('/api/profile/photo')
    assert stored.status_code==200 and stored.headers['content-type']=='image/jpeg'
    assert stored.headers['cache-control']=='private, no-store'
    assert c.post('/api/profile/photo',files={'file':('face.svg',b'<svg/>','image/svg+xml')}).status_code==400
    assert c.get('/api/profile/photo').content==stored.content
    assert c.delete('/api/profile/photo').json()['ok']
    assert c.get('/api/profile/photo').status_code==404


def test_photo_and_concepts_are_isolated_by_user(monkeypatch):
    provider(monkeypatch);intelligence.analyze();profile_photo.save(photo())
    cid=intelligence.state()['concepts'][0]['id']
    with context.run_as(context.ActorContext(user_id=922,tenant_id=922)):
        assert profile_photo.get() is None
        assert intelligence.state()['concepts']==[]
        assert not intelligence.decide(cid,'dismissed')
        assert profile_photo.cv_candidates()==[]
    assert profile_photo.get() and repo.get_profile()['search_signals']


def test_discovery_uses_active_roles_and_separate_bounded_queries(monkeypatch):
    provider(monkeypatch);intelligence.analyze()
    import connectors
    calls=[]
    monkeypatch.setattr(connectors.REGISTRY['adzuna'],'is_configured',lambda:True)
    def run(source,query):
        calls.append((source,query.what));return {'ok':True,'new':2}
    monkeypatch.setattr(connectors,'run_source_search',run)
    assert intelligence.discover()['new']==2
    assert calls==[('adzuna','Elektronik Reparaturtechniker')]


def test_profile_overview_has_avatar_timeline_and_ai_controls_in_all_languages():
    c=TestClient(app.app)
    for lang in ('sq','en','de'):
        r=c.get('/profile',params={'lang':lang})
        assert r.status_code==200
        for value in ('profile-orbit','career-timeline','career-concepts','enrich-profile','portrait-form'):
            assert value in r.text


def test_salary_completeness_uses_saved_filters():
    import completeness
    f=repo.get_filters();f['salary_min']=42000;repo.save_filters(f)
    assert next(d for d in completeness.compute()['dimensions'] if d['key']=='salary')['status']=='ok'


def test_server_locates_evidence_without_model_repeating_pdf_columns():
    text='TV technician\n\x00\nRepair Studio\nA completely different column\n2004-09 -\n2021-07'
    data={'items':[{'section':'experience','fields':{'title':'TV technician','org':'Repair Studio','period':'2004-09 - 2021-07','description':'Invented duties'}}]}
    suggestions,rejected=cv_ai.checked_suggestions(data,text)
    fields=json.loads(suggestions[0]['proposed_value'])
    assert fields=={'title':'TV technician','org':'Repair Studio','period':'2004-09 - 2021-07'}
    assert rejected==1


def test_rescore_large_catalog_does_not_lock_other_scorer_reads():
    # Force SQLite dirty pages beyond its cache before later gap reads, which
    # deadlocked when scoring and writes were interleaved on two connections.
    with repo.db() as con:
        for i in range(60):
            con.execute("INSERT INTO opportunities(user_id,tenant_id,source_url,canonical_url,dedup_key,title,description,kind,status,source,discovered_at,updated_at) VALUES (1,1,?,?,?,'Python Developer',?,'job','new','test',?,?)",(f'https://bulk.test/{i}',f'https://bulk.test/{i}',f'https://bulk.test/{i}','python '*8000,repo.now(),repo.now()))
    assert repo.rescore_all()==60


def test_parenthesized_language_level_is_not_a_second_language():
    text='Albanian (Native)'
    suggestions,_=cv_ai.checked_suggestions({'items':[{'section':'languages','fields':{'name':text}}]},text)
    assert json.loads(suggestions[0]['proposed_value'])=={'name':'Albanian','level':'Native'}


def test_automatic_ai_cv_import_also_builds_search_connections(monkeypatch):
    monkeypatch.setattr(cv_ai,'extract',lambda text:{'ok':True,'suggestions':[],'rejected':0})
    called=[]
    monkeypatch.setattr(intelligence,'analyze',lambda:called.append(True) or {'ok':True,'count':1})
    c=TestClient(app.app)
    r=c.post('/api/profile/cv',data={'profile_mode':'autofill','use_ai':'on'},files={'file':('cv.txt',b'Name: Example Candidate\nSkills: Python','text/plain')})
    assert r.json()['processing']['intelligence']['count']==1 and called==[True]


def test_pdf_layout_is_available_for_date_role_alignment(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    import cv_parser
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 700 Td (2010 - 2011) Tj 150 0 Td (Master Teaching) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream)
    path=tmp_path/'columns.pdf'
    with path.open('wb') as f:writer.write(f)
    result=cv_parser.extract_text(path)
    assert any('2010 - 2011' in line and 'Master Teaching' in line for line in result['layout_text'].splitlines())


def test_provider_label_variants_keep_display_name_and_search_aliases():
    item=concept()
    item['labels']={'sq':['Teknik elektronik','Specialist riparimi'],'en':['Electronics Repair Technician','TV Repair Specialist'],'de':['Elektronik Reparaturtechniker','Fernsehtechniker']}
    concepts,rejected=intelligence.checked_concepts({'concepts':[item]},'15 years repairing televisions')
    assert rejected==0 and concepts[0]['labels']['en']=='Electronics Repair Technician'
    assert 'Fernsehtechniker' in concepts[0]['terms']
    item['labels']['sq']=['too many']*20
    assert intelligence.checked_concepts({'concepts':[item]},'15 years repairing televisions')[0]==[]


def test_confirmed_roles_have_priority_within_discovery_budget(monkeypatch):
    import connectors
    roles=[{'id':str(i),'category':'role','status':'confirmed' if i==3 else 'active','labels':{'de':f'Role {i}'}} for i in range(4)]
    monkeypatch.setattr(intelligence,'state',lambda:{'stale':False,'concepts':roles})
    monkeypatch.setattr(connectors.REGISTRY['adzuna'],'is_configured',lambda:True)
    calls=[]
    monkeypatch.setattr(connectors,'run_source_search',lambda source,query:calls.append(query.what) or {'ok':True,'new':0})
    assert intelligence.discover()['ok']
    assert calls==['Role 3','Role 0','Role 1']


def test_evidence_references_are_resolved_from_source_and_unknown_ids_rejected():
    text='15 years repairing televisions\nTechnical customer support'
    blocks=intelligence.evidence_blocks(text)
    item=concept();item.pop('evidence');item['evidence_ids']=['E1']
    data=intelligence.resolve_evidence({'concepts':[item]},blocks)
    assert intelligence.checked_concepts(data,text)[0][0]['evidence']==['15 years repairing televisions']
    item['evidence_ids']=['E1','invented']
    data=intelligence.resolve_evidence({'concepts':[item]},blocks)
    assert intelligence.checked_concepts(data,text)[0]==[]


def test_openai_career_schema_constrains_evidence_ids(monkeypatch):
    import ai_service,httpx
    captured=[]
    schema=intelligence.output_schema({'E1':'Explicit source fact'})
    def post(url,**kw):
        captured.append(kw['json'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"concepts":[]}'}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr(ai_service.httpx,'post',post)
    cfg={'base_url':'https://api.openai.com/v1','model':'fixture','timeout':10,'response_schema':schema}
    ai_service.OpenAICompatProvider(cfg).complete('profile')
    response=captured[0]['response_format']
    assert response['type']=='json_schema' and response['json_schema']['strict'] is True
    assert response['json_schema']['schema']['properties']['concepts']['items']['properties']['evidence_ids']['items']['enum']==['E1']


def test_valid_multi_reference_roles_and_short_technical_skills_are_retained():
    blocks={f'E{i}':f'Explicit professional fact {i}' for i in range(1,8)}
    blocks['E8']='SQL'
    role=concept();role.pop('evidence');role['evidence_ids']=list(blocks)[:6]
    skill=concept('SQL');skill['category']='skill';skill.pop('evidence');skill['evidence_ids']=['E8']
    data=intelligence.resolve_evidence({'concepts':[role,skill]},blocks)
    concepts,rejected=intelligence.checked_concepts(data,'\n'.join(blocks.values()))
    assert len(concepts)==2 and rejected==0
