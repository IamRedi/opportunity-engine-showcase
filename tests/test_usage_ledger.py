"""OE-003B storage/state-machine tests. No provider or network calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import multiprocessing
import sqlite3

import libsql
import pytest

import auth_service
import cloud_database
import config
import context
import repository as repo
import usage_ledger as ledger
import usage_schema
from migrations.v16_usage_ledger import migrate_connection

UTC=timezone.utc
JAN=datetime(2026,1,15,tzinfo=UTC)
FEB=datetime(2026,2,15,tzinfo=UTC)
ONE=context.ActorContext(1,1,'owner')


@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'DB_PATH',tmp_path/'usage.sqlite3')
    monkeypatch.setenv('APP_ENV','test')
    monkeypatch.setenv('SECRET_KEY','fixture-usage-secret-at-least-32-characters')
    repo.init()


def add_card(ctx=ONE,card='search-v1',meter='search',provider='fixture'):
    ledger.register_price_card(card,meter,provider,'EUR',100,'fetch',
        {'fixed_amount':1,'max_redirects':0,'billing_mode':'per_fetch'},
        '2025-01-01T00:00:00+00:00',ctx=ctx)


def setup(limit=10,now=JAN,ctx=ONE):
    add_card(ctx=ONE)
    ledger.set_period_limit('search','EUR',100,limit,'pilot-v1',now=now,ctx=ctx)


def reserve(amount=6,key='one',now=JAN,ctx=ONE,fingerprint=None):
    return ledger.reserve('search','fixture','search','search-v1',amount,
        fingerprint or {'query_hash':'fixture','country':'de'},idempotency_key=key,now=now,ctx=ctx)


def _reserve_process(db_path,key,out):
    config.DB_PATH=db_path
    try:out.put(reserve(key=key,ctx=ONE)['state'])
    except ledger.UsageDenied as exc:out.put(str(exc))


def test_fresh_schema_and_v15_upgrade_preserve_data_and_disable_enforcement(tmp_path):
    path=tmp_path/'upgrade.sqlite3';repo.init(path)
    con=sqlite3.connect(path)
    for name in ('usage_evidence_events','usage_finalizations','usage_attempts','usage_requests','usage_budget_periods','usage_price_cards'):
        con.execute(f'DROP TABLE {name}')
    con.execute("UPDATE settings SET value='15' WHERE key='schema_version'")
    con.execute("DELETE FROM settings WHERE key='usage_enforcement_state'")
    con.execute("UPDATE profile SET name='Preserved' WHERE user_id=1")
    con.commit();con.close()
    info=repo.init(path)
    assert info['upgraded'] and info['backup']
    con=sqlite3.connect(path)
    names={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'usage_requests','usage_attempts','usage_finalizations','usage_budget_periods'}<=names
    assert con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]==str(repo.SCHEMA_VERSION)
    assert con.execute("SELECT value FROM settings WHERE key='usage_enforcement_state'").fetchone()[0]=='disabled'
    assert con.execute('SELECT name FROM profile WHERE user_id=1').fetchone()[0]=='Preserved'


def test_explicit_migration_is_idempotent_for_sqlite_and_local_libsql(tmp_path):
    for factory,name in ((lambda p:sqlite3.connect(p),'sqlite'),(lambda p:cloud_database.Connection(libsql.connect(str(p))),'libsql')):
        path=tmp_path/(name+'.db');con=factory(path)
        con.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        con.execute("INSERT INTO settings VALUES('schema_version','15')");con.commit()
        migrate_connection(con);migrate_connection(con)
        assert con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]=='16'
        assert con.execute("SELECT value FROM settings WHERE key='usage_enforcement_state'").fetchone()[0]=='disabled'
        con.close()


@pytest.mark.parametrize('kind',['sqlite','libsql'])
def test_migration_failure_rolls_back(tmp_path,monkeypatch,kind):
    path=tmp_path/f'failed-{kind}.db'
    con=sqlite3.connect(path) if kind=='sqlite' else cloud_database.Connection(libsql.connect(str(path)))
    con.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    con.execute("INSERT INTO settings VALUES('schema_version','15')");con.commit()
    monkeypatch.setattr(usage_schema,'DDL',[usage_schema.DDL[0],'INVALID SQL'])
    with pytest.raises(sqlite3.OperationalError):migrate_connection(con)
    assert con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0]=='15'
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='usage_price_cards'").fetchone()
    con.close()


def test_limit_10_two_concurrent_reservations_of_6_allow_only_one():
    setup()
    process_context=multiprocessing.get_context('spawn');out_queue=process_context.Queue()
    workers=[process_context.Process(target=_reserve_process,args=(config.DB_PATH,key,out_queue)) for key in ('a','b')]
    for worker in workers:worker.start()
    for worker in workers:worker.join(10)
    assert all(worker.exitcode==0 for worker in workers)
    out=[out_queue.get(timeout=2) for _ in workers]
    assert sorted(out)==['budget-exhausted','reserved']
    assert ledger.budget('search',now=JAN,ctx=ONE)['available']==4


def test_idempotency_fingerprint_and_actor_isolation():
    setup();first=reserve(key='same')
    same=reserve(key='same')
    assert same['request_id']==first['request_id'] and same['attempt_id']==first['attempt_id']
    with pytest.raises(ledger.UsageConflict):reserve(key='same',fingerprint={'query_hash':'changed'})
    two_id=auth_service.create_user('two@example.org','fixture-password-long-enough',tenant_id=1)
    two=context.ActorContext(two_id,1,'member')
    ledger.set_period_limit('search','EUR',100,10,'pilot-v1',now=JAN,ctx=two)
    other=reserve(key='same',ctx=two)
    assert other['request_id']!=first['request_id']
    with pytest.raises(ledger.UsageNotFound):ledger.get(first['handle'],ctx=two)
    assert ledger.get(first['handle'],ctx=ONE)['state']=='reserved'


def test_unknown_price_and_missing_budget_create_no_request():
    with pytest.raises(ledger.UsageDenied,match='unknown-price'):reserve()
    add_card()
    with pytest.raises(ledger.UsageDenied,match='budget-not-configured'):reserve()
    with repo.db() as con:assert con.execute('SELECT COUNT(*) FROM usage_requests').fetchone()[0]==0


def test_one_dispatch_claim_and_idempotent_settlement():
    setup();held=reserve()
    def take(owner):
        try:return ledger.claim(held['handle'],owner,ctx=ONE)
        except ledger.UsageBusy:return None
    with ThreadPoolExecutor(max_workers=2) as pool:claims=list(pool.map(take,['w1','w2']))
    winners=[c for c in claims if c]
    assert len(winners)==1
    done=ledger.settle(winners[0],4,{'input_units':3,'output_units':1},'provider-fixture','opaque-proof',now=JAN,ctx=ONE)
    again=ledger.settle(winners[0],99,now=JAN,ctx=ONE)
    assert done['state']==again['state']=='settled' and again['settled_charge']==4
    with repo.db() as con:assert con.execute('SELECT COUNT(*) FROM usage_finalizations').fetchone()[0]==1
    assert ledger.budget('search',now=JAN,ctx=ONE)['available']==6
    forged=ledger.Claim(winners[0].request_id,winners[0].attempt_id,winners[0].attempt_no,winners[0].fence,'wrong-token',winners[0].claim_until)
    with pytest.raises(ledger.UsageConflict):ledger.settle(forged,4,now=JAN,ctx=ONE)


def test_unknown_carries_into_next_month_and_blocks_new_dispatch():
    setup(now=JAN);held=reserve(now=JAN);claim=ledger.claim(held['handle'],'worker',now_epoch=10,ctx=ONE)
    assert ledger.settle(claim,None,ctx=ONE)['state']=='sent_unknown'
    ledger.set_period_limit('search','EUR',100,10,'pilot-v2',now=FEB,ctx=ONE)
    view=ledger.budget('search',now=FEB,ctx=ONE)
    assert view['blocked_unknown'] and view['available']==4
    with pytest.raises(ledger.UsageDenied,match='unresolved-usage'):reserve(now=FEB,key='feb')


def test_actual_above_reservation_can_make_budget_negative():
    setup();held=reserve();claim=ledger.claim(held['handle'],'worker',ctx=ONE)
    ledger.settle(claim,12,now=JAN,ctx=ONE)
    assert ledger.budget('search',now=JAN,ctx=ONE)['available']==-2
    with pytest.raises(ledger.UsageDenied,match='budget-exhausted'):reserve(amount=1,key='later')


def test_expired_claim_release_unknown_reconciliation_and_retry():
    setup(limit=30);a=reserve(key='a');claim=ledger.claim(a['handle'],'worker',lease_seconds=1,now_epoch=10,ctx=ONE)
    assert ledger.expire_claim(claim,False,now_epoch=12,ctx=ONE)['state']=='released'
    retry=ledger.reserve_retry(a['handle'],5,a['attempt_id'],now=JAN,ctx=ONE)
    assert retry['attempt_no']==2
    same_retry=ledger.reserve_retry(a['handle'],5,a['attempt_id'],now=JAN,ctx=ONE)
    assert same_retry['attempt_id']==retry['attempt_id']
    with pytest.raises(ledger.UsageConflict):ledger.reserve_retry(a['handle'],6,a['attempt_id'],now=JAN,ctx=ONE)
    retry_claim=ledger.claim(a['handle'],'worker',lease_seconds=1,now_epoch=20,ctx=ONE)
    assert ledger.expire_claim(retry_claim,True,now_epoch=22,ctx=ONE)['state']=='sent_unknown'
    def reconcile(owner):
        try:return ledger.claim_reconciliation(a['handle'],owner,lease_seconds=1,now_epoch=23,ctx=ONE)
        except ledger.UsageBusy:return None
    with ThreadPoolExecutor(max_workers=2) as pool:winners=[x for x in pool.map(reconcile,['r1','r2']) if x]
    assert len(winners)==1
    assert ledger.expire_reconciliation(winners[0],now_epoch=25,ctx=ONE)['state']=='sent_unknown'
    with pytest.raises(ledger.UsageConflict):ledger.settle(winners[0],1,now=JAN,ctx=ONE)


def test_active_period_immutable_and_limit_cannot_be_changed_retroactively():
    setup()
    assert ledger.set_period_limit('search','EUR',100,10,'pilot-v1',now=JAN,ctx=ONE)
    with pytest.raises(ledger.UsageConflict):ledger.set_period_limit('search','EUR',100,11,'pilot-v2',now=JAN,ctx=ONE)


def test_storage_has_hashes_only_and_metadata_is_allowlisted():
    setup();raw_key='private-idempotency-value';raw_fact='private-profile-description'
    held=reserve(key=raw_key,fingerprint={'private':raw_fact})
    claim=ledger.claim(held['handle'],'worker',ctx=ONE)
    assert ledger.add_evidence(claim,'event-1','late-response','private-provider-response',ctx=ONE)
    assert not ledger.add_evidence(claim,'event-1','late-response','private-provider-response',ctx=ONE)
    with pytest.raises(ValueError):ledger.settle(claim,1,{'prompt':raw_fact},ctx=ONE)
    ledger.settle(claim,1,{'result_count':1},provider_evidence_id='private-provider-id',evidence='private-provider-response',now=JAN,ctx=ONE)
    with repo.db() as con:
        dump=' '.join(str(v) for table in ('usage_requests','usage_attempts','usage_finalizations','usage_evidence_events')
            for row in con.execute(f'SELECT * FROM {table}') for v in row)
    assert raw_key not in dump and raw_fact not in dump and 'private-provider-response' not in dump
    assert 'private-provider-id' not in dump
    assert 'result_count' in dump


def test_price_card_id_is_immutable_and_no_raw_secret_snapshot():
    add_card();add_card()
    with pytest.raises(ledger.UsageConflict):
        ledger.register_price_card('search-v1','search','fixture','EUR',100,'fetch',{'fixed_amount':2},'2025-01-01T00:00:00+00:00',ctx=ONE)
    with pytest.raises(ValueError):
        ledger.register_price_card('bad','search','fixture','EUR',100,'fetch',{'api_key':'secret'},'2025-01-01T00:00:00+00:00',ctx=ONE)


def test_old_open_hold_is_carried_and_late_settlement_counts_in_resolution_month():
    setup(now=JAN);held=reserve(now=JAN);claim=ledger.claim(held['handle'],'worker',ctx=ONE)
    ledger.set_period_limit('search','EUR',100,10,'pilot-v2',now=FEB,ctx=ONE)
    carried=ledger.budget('search',now=FEB,ctx=ONE)
    assert carried['committed']==6 and carried['available']==4 and carried['blocked_unknown']
    ledger.settle(claim,4,now=FEB,ctx=ONE)
    resolved=ledger.budget('search',now=FEB,ctx=ONE)
    assert resolved['committed']==4 and resolved['available']==6 and not resolved['blocked_unknown']


def test_cache_hit_read_and_fail_closed_gate_create_no_usage_attempt():
    setup()
    assert ledger.enforcement_state()=='disabled'
    ledger.budget('search',now=JAN,ctx=ONE)  # cached work reads budget but reserves nothing
    with repo.db() as con:
        assert con.execute('SELECT COUNT(*) FROM usage_requests').fetchone()[0]==0
        con.execute("UPDATE settings SET value='unexpected' WHERE key='usage_enforcement_state'")
    assert ledger.enforcement_state()=='disabled'
