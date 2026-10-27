import io
import json

import pytest
from PIL import Image

import app
import config
import cv_ai
import cv_merge
import profile_photo
import repository as repo
import storage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'audit.db')
    monkeypatch.setattr(storage, 'get_storage', lambda: storage.LocalStorage(tmp_path / 'uploads'))
    repo.init()


def tonal_image(fmt='PNG'):
    img=Image.new('RGB',(120,120))
    img.putdata([(x*2,y*2,(x+y)%256) for y in range(120) for x in range(120)])
    out=io.BytesIO();img.save(out,format=fmt);return out.getvalue()


def cv():
    return repo.save_cv('fixture.pdf','fixture.pdf','application/pdf',10,'fixture')


def test_embedded_jpeg2000_is_supported_without_broadening_upload_types():
    raw=tonal_image('JPEG2000')
    with pytest.raises(ValueError):profile_photo.normalize(raw)
    assert profile_photo.normalize(raw,embedded=True).startswith(b'\xff\xd8')


def test_auto_photo_ignores_qr_and_preserves_manual_portrait(monkeypatch):
    cid=cv()
    qr=Image.new('RGB',(120,120),'white')
    qr.putdata([(0,0,0) if (x//4+y//4)%2 else (255,255,255) for y in range(120) for x in range(120)])
    out=io.BytesIO();qr.save(out,format='PNG')
    monkeypatch.setattr(profile_photo,'cv_candidates',lambda:[out.getvalue(),tonal_image()])
    assert profile_photo.autofill(cid)['status']=='saved'
    before=profile_photo.get()
    assert profile_photo.autofill(cid)['status']=='preserved'
    assert profile_photo.get()==before


def test_ambiguous_or_replaced_cv_never_autosaves(monkeypatch):
    cid=cv()
    monkeypatch.setattr(profile_photo,'cv_candidates',lambda:[tonal_image(),tonal_image()])
    assert profile_photo.autofill(cid)['status']=='choose'
    def replace():
        repo.save_cv('replacement.pdf','replacement.pdf','application/pdf',11,'replacement')
        return [tonal_image()]
    monkeypatch.setattr(profile_photo,'cv_candidates',replace)
    assert profile_photo.autofill(cid)['status']=='cv_changed'
    assert profile_photo.get() is None


def test_bullet_descriptions_www_and_skill_group_are_grounded():
    text='Technician\n• Repair circuits.\n• Support customers.\nPortfolio: www.example.test\nBackend & Daten: SQLite'
    items=[{'section':'experience','fields':{'title':'Technician','description':'Repair circuits. Support customers.'}},
           {'section':'portfolio','fields':{'title':'Portfolio','url':'www.example.test'}},
           {'section':'skills','fields':{'name':'SQLite','level':'Backend & Daten'}}]
    result,rejected=cv_ai.checked_suggestions({'items':items},text)
    assert rejected==0
    values=[json.loads(s['proposed_value']) for s in result]
    assert values[0]['description']=='Repair circuits. Support customers.'
    assert values[1]['url']=='https://www.example.test'
    assert 'level' not in values[2]
    items[0]['fields']['description']='Repair rockets.'
    result,_=cv_ai.checked_suggestions({'items':items},text)
    assert 'description' not in json.loads(result[0]['proposed_value'])


def test_reimport_project_label_and_node_alias_do_not_duplicate():
    cid=cv()
    repo.set_profile_section('projects',[{'title':'Example | SaaS / in development','description':'A real project','source':'manual'}])
    repo.set_profile_section('skills',[{'name':'Node.js','source':'manual'}])
    for section,fields in [('projects',{'title':'Example','description':'A real project'}),('skills',{'name':'node'})]:
        value=json.dumps(fields)
        repo.create_cv_suggestion(cid,section,'entry',value,cv_ai.norm(value),'evidence',.9,f'cv:{cid}')
    assert repo.autofill_cv_suggestions(cid)['duplicates']==2
    assert len(repo.get_profile_section('projects')[0])==1
    assert len(repo.get_profile_section('skills')[0])==1


def test_review_only_does_not_auto_set_photo(monkeypatch):
    cid=cv(); calls=[]
    monkeypatch.setattr(cv_merge,'parse_cv',lambda *a,**kw:{'ok':True,'candidate_ids':[]})
    monkeypatch.setattr(profile_photo,'autofill',lambda cid:calls.append(cid) or {'status':'none'})
    app._process_cv(cid,False,False)
    assert calls==[]
    app._process_cv(cid,True,False)
    assert calls==[cid]


def test_narrative_skill_pass_checks_evidence_and_aliases(monkeypatch):
    captured=[]
    class Provider:
        def __init__(self,cfg):captured.append(cfg)
        def complete(self,prompt):
            return json.dumps({'names':['Arduino','Lötarbeiten','node','Invented skill']}),{}
    monkeypatch.setitem(cv_ai.ai_service._PROVIDERS,'fixture',Provider)
    existing=[{'section':'skills','proposed_value':json.dumps({'name':'Node.js'})}]
    extra,rejected=cv_ai.extract_narrative_skills('Arduino, Lötarbeiten and node',existing,
        {'provider':'fixture','base_url':'https://api.openai.com/v1'})
    assert [json.loads(s['proposed_value'])['name'] for s in extra]==['Arduino','Lötarbeiten']
    assert rejected==1
    assert captured[0]['response_schema']['properties']['names']['maxItems']==45


def test_failed_coverage_pass_retains_first_extraction(monkeypatch):
    class Provider:
        calls=0
        def __init__(self,cfg):pass
        def complete(self,prompt):
            Provider.calls+=1
            if Provider.calls==1:
                return json.dumps({'items':[{'section':'experience','fields':{'title':'Technician'}}]}),{}
            raise TimeoutError()
    monkeypatch.setattr(cv_ai.ai_service,'get_config',lambda:{'provider':'fixture','model':'fixture'})
    monkeypatch.setitem(cv_ai.ai_service._PROVIDERS,'fixture',Provider)
    result=cv_ai.extract('Technician')
    assert result['ok'] and result['suggestions'][0]['section']=='experience'
    assert result['warning']


def test_explicit_tool_fallback_uses_whole_source_tokens():
    assert cv_ai.explicit_tools('Arduino-, CNC- und Elektroinstallationsprojekte. FFmpeg · Stripe/Paddle')==['Arduino','CNC','FFmpeg','Stripe','Paddle']
    assert cv_ai.explicit_tools('digit githubish cncfiction')==[]
