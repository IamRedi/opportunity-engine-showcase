"""OE-003C dispatch-gate integration tests; no network or paid calls."""
from types import SimpleNamespace

import httpx
import pytest

import config
import cloud_database
import context
import connectors
import ai_service
import libsql
import repository as repo
import source_http
import usage_ledger as ledger
import usage_runtime

ONE=context.ActorContext(1,1,'owner')


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'runtime.sqlite3')
    monkeypatch.setenv('APP_ENV','test')
    monkeypatch.setenv('SECRET_KEY','fixture-runtime-secret-at-least-32-characters')
    monkeypatch.setattr(source_http,'CACHE_DIR',tmp_path/'cache')
    repo.init()


def activate():
    with repo.db() as con:con.execute("UPDATE settings SET value='active' WHERE key='usage_enforcement_state'")


def card(kind,provider,model='',snapshot=None,billing='fetch'):
    cid=usage_runtime.card_id(kind,provider,model)
    ledger.register_price_card(cid,'ai' if kind=='ai' else 'search',provider,'EUR',100,billing,
        snapshot or {'fixed_amount':1,'billing_mode':'per_fetch'},'2025-01-01T00:00:00+00:00',model_alias=model,ctx=ONE)
    return cid


class FakeAI:
    id='fixture-ai'
    def __init__(self,usage=None,fail_once=False):
        self.cfg={'model':'fixture-model','max_output_tokens':10,'system_instructions':'system','usage_operation':'ai-fixture'}
        self.usage={'prompt_tokens':2,'completion_tokens':3} if usage is None else usage
        self.fail_once=fail_once;self.calls=0
    def _complete_unmetered(self,prompt):
        self.calls+=1
        if self.fail_once and self.calls==1:raise httpx.ConnectError('offline')
        return '{"ok":true}',self.usage


def setup_ai(limit=100):
    card('ai','fixture-ai','fixture-model',{'fixed_amount':0,'input_rate':1,'output_rate':1,
        'input_unit_size':10,'output_unit_size':1,'max_output_units':10,'encoder':'fixture-v1','billing_mode':'per_token'},'tokens')
    ledger.set_period_limit('ai','EUR',100,limit,'fixture-v1',ctx=ONE);activate()


def test_ai_provider_call_is_reserved_claimed_and_settled():
    setup_ai();provider=FakeAI()
    with context.run_as(ONE):assert usage_runtime.ai_complete(provider,'hello')[0]=='{"ok":true}'
    assert provider.calls==1
    view=ledger.budget('ai',ctx=ONE)
    assert view['committed']==4 and view['available']==96
    with repo.db() as con:
        assert con.execute("SELECT state FROM usage_attempts").fetchone()[0]=='settled'


def test_exhausted_budget_and_unknown_price_make_zero_provider_calls():
    setup_ai(limit=1);provider=FakeAI()
    with context.run_as(ONE),pytest.raises(ledger.UsageDenied,match='budget-exhausted'):
        usage_runtime.ai_complete(provider,'hello')
    assert provider.calls==0
    other=FakeAI();other.id='unpriced'
    with context.run_as(ONE),pytest.raises(ledger.UsageDenied,match='unknown-price'):
        usage_runtime.ai_complete(other,'hello')
    assert other.calls==0


def test_missing_provider_usage_becomes_unknown_and_blocks_next_call():
    setup_ai();provider=FakeAI(usage={})
    with context.run_as(ONE):usage_runtime.ai_complete(provider,'hello')
    assert ledger.budget('ai',ctx=ONE)['blocked_unknown']
    again=FakeAI()
    with context.run_as(ONE),pytest.raises(ledger.UsageDenied,match='unresolved-usage'):
        usage_runtime.ai_complete(again,'again')
    assert again.calls==0


def test_proven_connect_failure_releases_then_retry_creates_one_new_attempt():
    setup_ai();provider=FakeAI(fail_once=True)
    with context.run_as(ONE),pytest.raises(httpx.ConnectError):usage_runtime.ai_complete(provider,'hello')
    with context.run_as(ONE):usage_runtime.ai_complete(provider,'hello')
    assert provider.calls==2
    with repo.db() as con:
        rows=con.execute('SELECT attempt_no,state FROM usage_attempts ORDER BY attempt_no').fetchall()
    assert [tuple(row) for row in rows]==[(1,'released'),(2,'settled')]


def test_public_cache_hit_creates_no_second_reservation(monkeypatch):
    provider='www.arbeitnow.com';card('fetch',provider)
    ledger.set_period_limit('search','EUR',100,10,'fixture-v1',ctx=ONE);activate();calls=[]
    def get(url,**kwargs):
        calls.append(url)
        return httpx.Response(200,json={'data':[]},request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx,'get',get)
    with context.run_as(ONE):
        assert source_http.get_json('https://www.arbeitnow.com/api/job-board-api',{'page':1})=={'data':[]}
        assert source_http.get_json('https://www.arbeitnow.com/api/job-board-api',{'page':1})=={'data':[]}
    assert len(calls)==1 and ledger.budget('search',ctx=ONE)['committed']==1
    with repo.db() as con:assert con.execute('SELECT COUNT(*) FROM usage_requests').fetchone()[0]==1


def test_production_disabled_or_unknown_gate_fails_before_transport(monkeypatch):
    monkeypatch.setenv('APP_ENV','production');calls=[]
    with repo.db() as con:con.execute("UPDATE settings SET value='unexpected' WHERE key='usage_enforcement_state'")
    with context.run_as(ONE),pytest.raises(usage_runtime.ProviderDispatchDisabled):
        usage_runtime.http_response('search-fixture','fixture',lambda:calls.append(1),fingerprint={'fixture':True})
    assert calls==[]
    with repo.db() as con:
        con.execute("UPDATE settings SET value='15' WHERE key='schema_version'")
        con.execute("UPDATE settings SET value='active' WHERE key='usage_enforcement_state'")
    assert not usage_runtime.dispatch_available()


def test_ai_run_task_cache_hit_is_free_and_limit_stops_new_transport(monkeypatch):
    model='fixture-openai';card('ai','openai-compatible',model,{'fixed_amount':0,'input_rate':0,
        'output_rate':1,'input_unit_size':1,'output_unit_size':1,'max_output_units':5,
        'encoder':'chars-upper-v1','billing_mode':'per_token'},'tokens')
    ledger.set_period_limit('ai','EUR',100,5,'fixture-v1',ctx=ONE);activate();calls=[]
    def post(url,**kwargs):
        calls.append(url)
        return httpx.Response(200,request=httpx.Request('POST',url),json={
            'choices':[{'message':{'content':'{"summary":"ok","pros":[],"cons":[]}'}}],
            'usage':{'prompt_tokens':2,'completion_tokens':5}})
    monkeypatch.setattr(httpx,'post',post)
    cfg={'provider':'openai-compatible','model':model,'base_url':'https://api.example.test/v1',
         'timeout':5,'retries':0,'max_input_chars':6000,'max_output_tokens':5,'api_key':'fixture'}
    opportunity={'id':1,'title':'Fixture','description':'Fixture role'}
    with context.run_as(ONE):
        first=ai_service.run_task('explain',opportunity,profile={},cfg=cfg)
        second=ai_service.run_task('explain',opportunity,profile={},cfg=cfg)
        forced=ai_service.run_task('explain',{**opportunity,'id':2},profile={},cfg=cfg,force=True)
    assert first.ok and second.ok and second.cached and len(calls)==1
    assert not forced.ok and 'Limiti i këtij muaji' in forced.error and len(calls)==1


def test_connector_budget_exhaustion_produces_zero_extra_serpapi_calls(monkeypatch):
    card('fetch','serpapi');ledger.set_period_limit('search','EUR',100,1,'fixture-v1',ctx=ONE);activate()
    repo.set_setting('serpapi_key','fixture-key');calls=[]
    def get(url,**kwargs):
        calls.append(url)
        return httpx.Response(200,request=httpx.Request('GET',url),json={'jobs_results':[
            {'job_id':'one','title':'Fixture job','company_name':'Example','location':'Essen',
             'description':'Role','apply_options':[{'link':'https://example.org/job/one'}]}]})
    monkeypatch.setattr(httpx,'get',get)
    query=connectors.SearchQuery(what='developer',where='Essen',country='de',kinds=('job',),limit=5)
    with context.run_as(ONE):
        first=connectors.run_source_search('google_jobs',query)
        second=connectors.run_source_search('google_jobs',query)
    assert first['ok'] and first['discovered']==1
    assert not second['ok'] and 'Limiti i këtij muaji' in second['error'] and len(calls)==1


def test_c1_accepts_cloud_schema_15_but_dispatch_remains_closed(tmp_path,monkeypatch):
    path=tmp_path/'s15.sqlite3';repo.init(path)
    con=__import__('sqlite3').connect(path)
    for table in ('usage_evidence_events','usage_finalizations','usage_attempts','usage_requests','usage_budget_periods','usage_price_cards'):
        con.execute(f'DROP TABLE {table}')
    con.execute("UPDATE settings SET value='15' WHERE key='schema_version'")
    con.execute("DELETE FROM settings WHERE key='usage_enforcement_state'");con.commit();con.close()
    monkeypatch.setenv('DATABASE_PROVIDER','turso');monkeypatch.setenv('APP_ENV','production')
    monkeypatch.setattr(cloud_database,'connect',lambda:cloud_database.Connection(libsql.connect(str(path))))
    info=repo.init();assert info['compatibility'] and info['schema_version']=='15'
    calls=[]
    with context.run_as(ONE),pytest.raises(usage_runtime.ProviderDispatchDisabled):
        usage_runtime.http_response('search-fixture','fixture',lambda:calls.append(1),fingerprint={'fixture':True})
    assert calls==[]
