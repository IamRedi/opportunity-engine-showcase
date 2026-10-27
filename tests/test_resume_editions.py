import copy
import json
import io
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
import ai_service
import app
import config
import context
import profile_context
import profile_intake
import profile_intelligence
import profile_resume
import repository as repo
import resume_ai

OPTIONS={'skill_limit':24,'include_projects':True,'include_personal':False,'include_photo':False}

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'db.sqlite');repo.init()
    repo.update_profile_fields({'name':'Alex Example','title':'Entwickler','email':'alex@example.org','summary':'Arbeitet seit 2015 mit Python.'})
    repo.set_profile_section('languages',[{'name':'Deutsch','level':'B2'}])


def provider(monkeypatch,change=None):
    calls=[]
    class Provider:
        def __init__(self,cfg):pass
        def complete(self,prompt):
            calls.append(prompt)
            fields=json.loads(prompt.split('\nSOURCE FIELDS:\n')[1].split('\nDRAFT FIELDS:\n')[0])
            translations={'Entwickler':'Developer','Arbeitet seit 2015 mit Python.':'Works with Python since 2015.','Deutsch':'German'}
            if change:change()
            return json.dumps({'fields':[{'id':k,'text':translations.get(v,v)} for k,v in fields.items()]}),{}
    cfg={**ai_service.get_config(),'provider':'fixture','model':'test'}
    monkeypatch.setattr(ai_service,'get_config',lambda:cfg)
    monkeypatch.setitem(ai_service._PROVIDERS,'fixture',Provider)
    return calls


def facts_snapshot():
    with repo.db() as c:return profile_intake.revision(c,1)


def test_export_is_two_pass_private_cached_and_does_not_change_profile(monkeypatch):
    calls=provider(monkeypatch);before=facts_snapshot();token=profile_intelligence.snapshot()[0]
    assert resume_ai.generate('en',OPTIONS)['ok']
    assert len(calls)==2 and 'final bilingual' in calls[1]
    edition=resume_ai.get('en',OPTIONS)
    assert edition['document']['core']['title']=='Developer'
    assert edition['document']['core']['email']=='alex@example.org'
    assert repo.get_profile()['title']=='Entwickler' and facts_snapshot()==before
    assert profile_intelligence.snapshot()[0]==token
    assert resume_ai.generate('en',OPTIONS)['cached'] and len(calls)==2
    with context.run_as(context.ActorContext(user_id=2)):
        assert resume_ai.get('en',OPTIONS) is None


def test_larger_current_edition_serves_smaller_layout_without_an_ai_call(monkeypatch):
    calls = provider(monkeypatch)
    repo.set_profile_section('skills', [{'name': f'Skill {index}'} for index in range(50)])
    assert resume_ai.generate('en', resume_ai.PROFILE_OPTIONS)['ok']
    assert len(calls) == 2
    smaller = resume_ai.get('en', OPTIONS)
    assert smaller['derived_from_larger_edition']
    assert len(smaller['document']['skills']) == 24
    assert resume_ai.generate('en', OPTIONS)['cached']
    assert len(calls) == 2


def test_different_exact_layouts_are_retained_together(monkeypatch):
    calls = provider(monkeypatch)
    small = {**OPTIONS, 'include_projects': False}
    larger = {**OPTIONS, 'skill_limit': 40, 'include_projects': True}
    assert resume_ai.generate('en', small)['ok']
    assert resume_ai.generate('en', larger)['ok']
    assert len(calls) == 4
    entries, _ = repo.get_profile_section(resume_ai.SECTION)
    assert {tuple(sorted(e['options'].items())) for e in entries} == {
        tuple(sorted(small.items())), tuple(sorted(larger.items()))}
    assert resume_ai.get('en', small) and resume_ai.get('en', larger)
    r=TestClient(app.app).get('/api/profile/resume.pdf?lang=en&include_photo=false')
    assert r.status_code==200 and r.headers['cache-control']=='private, no-store'
    text='\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
    assert 'Developer' in text and 'Entwickler' not in text


def test_document_language_does_not_change_interface_language(monkeypatch):
    from test_ui_views import Elements
    provider(monkeypatch)
    assert resume_ai.generate('en',OPTIONS)['ok']
    with TestClient(app.app) as client:
        initial=client.get('/profile/resume?lang=de')
        assert Elements(initial.text).find('input',type='hidden',name='cv_language')[0]['value']=='de'
        page=client.get('/profile/resume?lang=de&cv_language=en&configured=yes&projects=yes')
        assert '<html lang="de"' in page.text and '<div class="resume-paper" lang="en">' in page.text
        assert 'cv_language=en' in page.text and 'Developer' in page.text
        pdf=client.get('/api/profile/resume.pdf?cv_language=en&include_photo=false')
        assert pdf.status_code==200 and pdf.headers['content-language']=='en'
        text='\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
        assert 'LANGUAGES' in text and 'SPRACHEN' not in text
        assert client.cookies['oe_lang']=='de'
        assert '<html lang="de"' in client.get('/profile').text
        assert repo.get_profile()['title']=='Entwickler'


def test_profile_overview_uses_complete_current_language_edition(monkeypatch):
    provider(monkeypatch)
    assert resume_ai.generate('en', resume_ai.PROFILE_OPTIONS)['ok']
    with TestClient(app.app) as client:
        page = client.get('/profile?lang=en')
    assert page.status_code == 200
    assert '<p class="identity-title">Developer</p>' in page.text
    assert 'Works with Python since 2015.' in page.text
    assert 'The profile is presented in the selected language' in page.text
    # Editors retain canonical facts; changing presentation never rewrites them.
    assert 'value="Entwickler"' in page.text
    assert '>Arbeitet seit 2015 mit Python.</textarea>' in page.text


def test_profile_get_without_edition_is_honest_and_never_calls_ai(monkeypatch):
    monkeypatch.setattr(resume_ai, 'generate', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('GET called AI')))
    page = TestClient(app.app).get('/profile?lang=en')
    assert page.status_code == 200
    assert 'The profile content is still in its original language.' in page.text
    assert 'id="prepare-profile-language" data-language="en"' in page.text
    assert '<p class="identity-title">Entwickler</p>' in page.text


def test_profile_change_invalidates_export_and_old_cv_cannot_restore_removed_facts(monkeypatch):
    provider(monkeypatch);assert resume_ai.generate('en',OPTIONS)['ok']
    repo.set_profile_section('languages',[{'name':'Deutsch','level':'C1'}])
    assert resume_ai.get('en',OPTIONS) is None
    assert repo.get_profile()['languages']==['Deutsch – C1']
    assert 'B2' not in profile_intelligence.snapshot()[1]
    repo.set_profile_section('languages',[])
    assert profile_context.build_context()['languages']==[]
    assert repo.get_profile()['languages']==[]
    assert TestClient(app.app).get('/api/profile/resume.pdf?lang=en&include_photo=false').status_code==409


def test_change_during_ai_never_publishes_stale_document(monkeypatch):
    provider(monkeypatch,lambda:repo.update_profile_fields({'phone':'changed'}))
    result=resume_ai.generate('en',OPTIONS)
    assert not result['ok'] and 'ndryshoi' in result['reason']
    assert not repo.get_profile_section(resume_ai.SECTION)[0]


def test_numeric_and_language_level_changes_missing_or_duplicate_fields_fail():
    source={'one':'Deutsch B2 seit 2015'}
    for fields in ([{'id':'one','text':'German C1 since 2015'}],[{'id':'one','text':'German B2 since 2014'}],[],[{'id':'other','text':'German B2 since 2015'}]):
        with pytest.raises(ValueError):resume_ai.validate_fields({'fields':fields},source)


def test_organization_names_are_preserved_even_if_model_translates_them():
    repo.set_profile_section('experience',[{'title':'Technician','org':'Baumdienst Holz & Grün, Deutschland'}])
    original,_=resume_ai.source(OPTIONS)
    assert 'experience.0.org' not in resume_ai.slots(original)
    translated=copy.deepcopy(original);translated['experience'][0]['org']='Tree Service Wood & Green, Germany'
    result=resume_ai.protect_names(translated,original,'en')
    assert result['experience'][0]['org']=='Baumdienst Holz & Grün, Germany'
    assert original['experience'][0]['org']=='Baumdienst Holz & Grün, Deutschland'


@pytest.mark.parametrize('language,title,prefix',[
    ('en','Full-Stack & Applied AI Developer','Industry software / online — SaaS platform'),
    ('de','Full-Stack- & Applied-AI-Entwickler','Branchensoftware / online — aus'),
    ('sq','Zhvillues Full-Stack dhe i zgjidhjeve AI','Softuer për sektorin / online — platformë SaaS'),
])
def test_professional_glossary_repairs_known_literal_profile_phrasing(language,title,prefix):
    original=profile_resume.document_data(24,True,False)
    original['core']['title']='FULL-STACK & APPLIED AI DEVELOPER'
    original['projects']=[{'title':'BaumdienstFlow','description':'source'}]
    translated=copy.deepcopy(original)
    translated['projects'][0]['description']={
        'en':'Industry software / online Developed from own industry experience, a SaaS platform for field companies.',
        'de':'Branchensoftware / online Aus eigener Branchenerfahrung entwickelte SaaS-Plattform.',
        'sq':'Softuer sektori / online Platformë SaaS e zhvilluar nga përvoja ime.',
    }[language]
    result=resume_ai.protect_names(translated,original,language)
    assert result['core']['title']==title
    assert result['projects'][0]['description'].startswith(prefix)


def test_explicit_profile_actor_never_inherits_another_users_preferences():
    repo.set_profile_section('local',[{'radius_km':17}])
    other=context.ActorContext(user_id=2)
    repo.set_profile_section('local',[{'radius_km':83}],ctx=other)
    preferences=' '.join(repo.get_profile(ctx=other)['stated_preferences'])
    assert '83' in preferences and '17' not in preferences


def test_old_cv_text_never_reintroduces_a_corrected_language():
    cv_id=repo.save_cv('old.txt','fixture.txt','text/plain',20,'fixture')
    repo.update_cv_extract(cv_id,{'extracted_text':'English C2 and Old Employer','parse_status':'parsed'})
    repo.set_profile_section('languages',[{'name':'English','level':'A2'}])
    source=profile_intelligence.snapshot()[1]
    assert 'A2' in source and 'C2' not in source and 'Old Employer' not in source


def test_description_existing_fields_need_separate_confirmation(monkeypatch):
    monkeypatch.setattr(profile_intake.cv_ai,'extract',lambda *a,**kw:{'ok':True,'suggestions':[
        {'section':'contact','field':'title','proposed_value':'Updated role','evidence':'Updated role'}]})
    preview=profile_intake.preview('I now want to use this Updated role.')['preview']
    assert not preview['items'][0]['selected']
    result=profile_intake.commit(preview['id'],preview['items'])
    assert result['needs_confirmation'] and result['conflicts'][0]['before']=='Entwickler'
    assert repo.get_profile()['title']=='Entwickler'
    assert profile_intake.commit(preview['id'],preview['items'],allow_updates=True)['ok']
    assert repo.get_profile()['title']=='Updated role'
    assert repo.get_profile()['email']=='alex@example.org'


def test_location_and_preferences_refresh_career_context_and_draft_cache():
    before=profile_intelligence.snapshot()[0]
    repo.update_profile_fields({'location':'Dortmund'})
    assert profile_intelligence.snapshot()[0]!=before
    cfg={'provider':'fixture','model':'test'}
    old=ai_service.cache_key('draft_application',{},repo.get_profile(),cfg)
    repo.set_profile_section('education',[{'title':'New education'}])
    assert ai_service.cache_key('draft_application',{},repo.get_profile(),cfg)!=old
    assert 'New education' in ai_service._profile_content(repo.get_profile())['education'][0]
    previous=ai_service.cache_key('draft_application',{},repo.get_profile(),cfg)
    repo.set_profile_section('local',[{'radius_km':15,'no_car':True}])
    assert ai_service.cache_key('draft_application',{},repo.get_profile(),cfg)!=previous
    assert any('15' in p and 'no_car' in p for p in repo.get_profile()['stated_preferences'])
