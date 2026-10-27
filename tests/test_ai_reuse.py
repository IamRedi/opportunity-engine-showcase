"""Paid-call counts and cross-request/account isolation, without real provider calls."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import ai_service,ai_requests,config,context,cv_ai,cv_merge,cv_parser
import repository as repo
import profile_intelligence as intelligence
import resume_ai

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'reuse.db')
    monkeypatch.setenv('APP_ENV','test')
    repo.init()
    monkeypatch.setattr(ai_service,'get_config',lambda:{'provider':'fixture','model':'test','base_url':'https://fixture.invalid'})


def test_cv_extraction_reuses_output_and_concurrent_clicks_never_double_call(monkeypatch):
    entered=threading.Event();finish=threading.Event();calls=[]
    class Provider:
        def __init__(self,cfg):pass
        def complete(self,prompt):
            calls.append(prompt)
            if len(calls)==1:
                entered.set();assert finish.wait(5)
            return json.dumps({'items':[{'section':'skills','fields':{'name':'Python'}}]}),{}
    monkeypatch.setitem(ai_service._PROVIDERS,'fixture',Provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(cv_ai.extract,'Python')
        assert entered.wait(5)
        try:
            assert cv_ai.extract('Python')['busy']
            assert len(calls)==1
        finally:finish.set()
        assert first.result()['ok']
    assert cv_ai.extract('Python')['cached'] and len(calls)==1
    with context.run_as(context.ActorContext(2,2,'member')):
        assert cv_ai.extract('Python')['ok'] and len(calls)==2
    assert cv_ai.extract('Python and SQL')['ok'] and len(calls)==3


def test_same_cv_reupload_keeps_review_decisions_and_parse_does_not_call_ai_again(tmp_path,monkeypatch):
    p=tmp_path/'cv.txt';p.write_text('Skills: Python')
    cid=repo.save_cv('cv.txt',str(p),'text/plain',len(p.read_bytes()),'stable-hash')
    calls=[]
    def extract(text):
        calls.append(text)
        return {'ok':True,'suggestions':[],'rejected':0}
    monkeypatch.setattr(cv_ai,'extract',extract)
    first=cv_merge.parse_cv(cid,use_ai=True);assert first['ok']
    suggestion=repo.cv_suggestions(cid)[0];repo.set_cv_suggestion_status(suggestion['id'],'rejected')
    repo.create_cv_suggestion(cid,'contact','name','Obsolete','obsolete','old',.95,f'cv:{cid}')
    again=repo.save_cv('renamed.txt',str(p),'text/plain',len(p.read_bytes()),'stable-hash')
    assert again==cid
    monkeypatch.setattr(cv_parser,'extract_text',lambda *a:pytest.fail('Already parsed CV should not be read again'))
    second=cv_merge.parse_cv(cid,use_ai=True)
    assert second['cached'] and len(calls)==1
    assert second['candidate_ids']==first['candidate_ids']
    assert repo.get_cv_suggestion(suggestion['id'])['status']=='rejected'
    assert repo.save_cv('new.txt',str(p),'text/plain',len(p.read_bytes()),'different-hash')!=cid


def test_existing_profile_analysis_is_free_and_edits_require_fresh_analysis(monkeypatch):
    repo.update_profile_fields({'name':'Example','summary':'I repair computers.'})
    token,_,cid=intelligence.snapshot()
    record={'version':intelligence.VERSION,'source_token':token,'cv_id':cid,'concepts':[{'id':'one','status':'dismissed'}]}
    repo.set_profile_section('intelligence',[record])
    calls=[]
    class Provider:
        def __init__(self,cfg):pass
        def complete(self,prompt):calls.append(prompt);raise ValueError('fixture stops before paid work')
    monkeypatch.setitem(ai_service._PROVIDERS,'fixture',Provider)
    assert intelligence.analyze()['cached'] and calls==[]
    assert intelligence.state()['analysis_current']
    assert repo.get_profile_section('intelligence')[0][0]==record
    repo.update_profile_fields({'summary':'I repair computers and printers.'})
    assert not intelligence.state()['analysis_current']
    assert not intelligence.analyze()['ok'] and len(calls)==1


def test_resume_same_options_and_concurrent_clicks_reuse_validated_edition(monkeypatch):
    repo.update_profile_fields({'name':'Example','title':'Developer'})
    options={'skill_limit':24,'include_projects':True,'include_personal':False,'include_photo':False}
    entered=threading.Event();finish=threading.Event();calls=[]
    def call(cfg,prompt,original):
        calls.append(prompt)
        if len(calls)==1:entered.set();assert finish.wait(5)
        return original
    monkeypatch.setattr(resume_ai,'_call',call)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(resume_ai.generate,'en',options);assert entered.wait(5)
        try:assert resume_ai.generate('en',options)['busy']
        finally:finish.set()
        assert first.result()['ok']
    assert len(calls)==2
    assert resume_ai.generate('en',options)['cached'] and len(calls)==2
    assert resume_ai.generate('de',options)['ok'] and len(calls)==4


def test_finished_failed_request_does_not_leave_a_stuck_lease():
    @ai_requests.serialized('failure-fixture')
    def fail():raise RuntimeError('fixture')
    for _ in range(2):
        with pytest.raises(RuntimeError):fail()
    assert not repo.get_setting('ai-lease:1:failure-fixture')
