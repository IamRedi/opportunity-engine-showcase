"""Cloud transport, private-file and account-boundary regressions; no real secrets."""
import io
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import libsql
from fastapi.testclient import TestClient
import app
import auth
import auth_service
import cloud_access
import cloud_database
import config
import context
import repository as repo
import storage
import usage_ledger
from scripts.prepare_cloud_schema import schema_sql


@pytest.fixture
def cloud(tmp_path,monkeypatch):
    path=tmp_path/'cloud.sqlite3'
    sql=schema_sql()
    con=sqlite3.connect(path);con.executescript(sql);con.close()
    monkeypatch.setenv('DATABASE_PROVIDER','turso')
    monkeypatch.setattr(cloud_database,'connect',lambda:cloud_database.Connection(libsql.connect(str(path))))
    return path


def test_cloud_transactions_rows_reconnect_and_duplicate_constraints(cloud):
    row=cloud_database.Row(('value',),('KEY',))
    assert row['key']==row['Key']==row[0]=='value'
    with pytest.raises(IndexError):row['missing']
    with repo.db() as c:
        assert dict(c.execute('select 3 as value').fetchone())=={'value':3}
        c.execute("insert into settings values('pilot-check','one')")
    with pytest.raises(RuntimeError):
        with repo.db() as c:
            c.execute("update settings set value='two' where key='pilot-check'")
            raise RuntimeError('abort')
    with repo.db() as c:
        assert c.execute("select value from settings where key='pilot-check'").fetchone()[0]=='one'
    with pytest.raises(sqlite3.IntegrityError):
        with repo.db() as c:c.execute("insert into settings values('pilot-check','duplicate')")
    with repo.db() as c:
        assert c.execute("select value from settings where key='usage_enforcement_state'").fetchone()[0]=='disabled'
    assert repo.init()['migrated'] is False


def test_remote_repository_round_trip_and_two_owners(cloud,monkeypatch):
    monkeypatch.setenv('APP_ENV','production')
    one=auth_service.create_user('one@example.org','long-test-password-one',tenant_id=1,role='owner')
    two=auth_service.create_user('two@example.org','long-test-password-two',tenant_id=2)
    with context.run_as(context.ActorContext(one,1,'owner')):
        repo.get_profile();repo.update_profile_fields({'name':'One','title':'Python Developer'})
        repo.set_profile_section('skills',[{'name':'Python'}])
        _,oid=repo.upsert_opportunity_full({'url':'https://example.org/cloud-only','title':'Python','kind':'job','tags':['python']})
        repo.set_setting('pilot-check','saved')
        assert repo.get_opportunity(oid)['title']=='Python'
    with context.run_as(context.ActorContext(two,2,'member')):
        assert repo.get_profile()['name']==''
        assert repo.get_profile_section('skills')[0]==[]
        assert repo.get_opportunity(oid) is None


def test_remote_local_lens_uses_canonical_returned_opportunity_id(cloud):
    uid = auth_service.create_user(
        'local-lens@example.org', 'long-test-password-local-lens', tenant_id=2)
    actor = context.ActorContext(uid, 2, 'member')
    with context.run_as(actor):
        status, opportunity_id = repo.upsert_opportunity_full({
            'source': 'adzuna', 'url': 'https://example.org/remote-local-lens',
            'title': 'Gassi gehen', 'company': 'Fixture', 'location': 'Dortmund',
            'description': 'Local help fixture', '_local_search': True,
        })
    assert status == 'new'
    with repo.db() as con:
        opportunity = con.execute(
            'SELECT id,user_id,tenant_id FROM opportunities WHERE id=?',
            (opportunity_id,),
        ).fetchone()
        lens = con.execute(
            "SELECT lens,source FROM opportunity_lenses WHERE opportunity_id=?",
            (opportunity_id,),
        ).fetchone()
    assert (opportunity['user_id'], opportunity['tenant_id']) == (uid, 2)
    assert dict(lens) == {'lens': 'local_job', 'source': 'search'}


def test_public_cloud_requires_auth_and_customer_cannot_change_global_services(cloud,monkeypatch):
    monkeypatch.setenv('APP_ENV','production');monkeypatch.setenv('PLATFORM_ADMIN_USER_IDS','1')
    monkeypatch.setattr(config,'SECRET_KEY','test-secret-that-is-long-enough-for-cloud')
    monkeypatch.setenv('SECRET_KEY',config.SECRET_KEY)
    auth_service.create_user('one@example.org','long-test-password-one',tenant_id=1,role='owner')
    auth_service.create_user('two@example.org','long-test-password-two',tenant_id=2)
    with TestClient(app.app,base_url='https://testserver') as client:
        assert client.get('/profile',follow_redirects=False).status_code==303
        assert client.get('/api/profile/cv').status_code==401
        response=client.post('/api/auth/login',data={'username':'two@example.org','password':'long-test-password-two'})
        assert response.status_code==200 and 'Secure' in response.headers['set-cookie']
        headers={'X-CSRF-Token':client.cookies[auth.COOKIE_CSRF]}
        assert client.post('/api/ai/config',headers=headers,json={}).status_code==403
        assert client.post('/api/scheduler/tick',headers=headers).status_code==403
        assert client.get('/api/eval/runs').status_code==403
        assert client.get('/api/email/config').status_code==403
        assert client.post('/api/import/url',headers=headers,json={'url':'http://127.0.0.1/'}).status_code==409
        page=client.get('/profile?lang=en')
        assert page.status_code==200
        assert 'name="adzuna_app_key"' not in page.text
        assert 'name="recipient"' not in page.text
        assert client.post('/api/pilot/switch',headers=headers,json={'user_id':1}).status_code==404
        assert client.get('/api/profile/cv').headers['cache-control']=='private, no-store'


def test_only_platform_admin_can_reconcile_unknown_usage_with_hashed_evidence(cloud, monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('PLATFORM_ADMIN_USER_IDS', '1')
    monkeypatch.setenv('SECRET_KEY', 'fixture-reconciliation-secret-at-least-32')
    monkeypatch.setattr(config, 'SECRET_KEY', os.environ['SECRET_KEY'])
    one_id = auth_service.create_user(
        'usage-admin@example.org', 'long-test-password-one', tenant_id=1, role='owner')
    two_id = auth_service.create_user(
        'usage-member@example.org', 'long-test-password-two', tenant_id=2, role='member')
    one = context.ActorContext(one_id, 1, 'owner')
    two = context.ActorContext(two_id, 2, 'member')
    usage_ledger.register_price_card(
        'reconcile-fetch-v1', 'search', 'fixture', 'USD', 1_000_000, 'fetch',
        {'fixed_amount': 0, 'billing_mode': 'per_fetch'},
        '2026-01-01T00:00:00+00:00', ctx=one,
    )
    usage_ledger.set_period_limit(
        'search', 'USD', 1_000_000, 1_000_000, 'reconcile-v1', ctx=two,
    )
    held = usage_ledger.reserve(
        'fixture-search', 'fixture', 'search', 'reconcile-fetch-v1', 0,
        {'query_hash': 'fixture'}, ctx=two,
    )
    claim = usage_ledger.claim(held['handle'], 'fixture-worker', now_epoch=1, ctx=two)
    unknown = usage_ledger.fail_claim(claim, True, 'transport-error', ctx=two)
    payload = {'attempt_id': unknown['attempt_id'], 'charge_amount': 0,
               'provider_evidence_id': 'fixture-provider-audit-1',
               'evidence': 'Provider account audit confirms the request.'}

    with TestClient(app.app, base_url='https://testserver') as member:
        response = member.post('/api/auth/login', data={
            'username': 'usage-member@example.org', 'password': 'long-test-password-two'})
        assert response.status_code == 200
        headers = {'X-CSRF-Token': member.cookies[auth.COOKIE_CSRF]}
        assert member.post('/api/admin/usage/reconcile', headers=headers,
                           json=payload).status_code == 403

    with TestClient(app.app, base_url='https://testserver') as admin:
        response = admin.post('/api/auth/login', data={
            'username': 'usage-admin@example.org', 'password': 'long-test-password-one'})
        assert response.status_code == 200
        headers = {'X-CSRF-Token': admin.cookies[auth.COOKIE_CSRF]}
        settled = admin.post('/api/admin/usage/reconcile', headers=headers, json=payload)
        assert settled.status_code == 200
        assert settled.json() == {'ok': True, 'attempt_id': unknown['attempt_id'],
            'state': 'settled', 'charge_amount': 0, 'already_final': False}
        repeated = admin.post('/api/admin/usage/reconcile', headers=headers, json=payload)
        assert repeated.status_code == 200 and repeated.json()['already_final'] is True
    with repo.db() as con:
        attempt = con.execute('SELECT * FROM usage_attempts WHERE id=?',
                              (unknown['attempt_id'],)).fetchone()
        events = con.execute('SELECT COUNT(*) n FROM usage_evidence_events WHERE attempt_id=?',
                             (unknown['attempt_id'],)).fetchone()['n']
    assert attempt['state'] == 'settled' and attempt['settled_charge'] == 0
    assert events == 1


def test_login_rate_limit_is_persistent_across_connections(cloud,monkeypatch):
    monkeypatch.setenv('SECRET_KEY','private-fixture-secret')
    for _ in range(10):assert cloud_access.login_allowed('one@example.org','127.0.0.1')
    assert not cloud_access.login_allowed('one@example.org','127.0.0.1')
    with repo.db() as con:
        assert all('example.org' not in r['key'] for r in con.execute('select key from login_attempts'))


def test_private_storage_never_returns_public_urls_and_cv_parser_reads_bytes(monkeypatch):
    from vercel import blob
    import cv_parser
    files={};calls=[]
    def put(key,body,**options):
        assert options['access']=='private';files[key]=body;calls.append(options)
    monkeypatch.setattr(blob,'put',put)
    monkeypatch.setattr(blob,'get',lambda key,**options: files[key] if options['access']=='private' else pytest.fail('public access'))
    monkeypatch.setattr(blob,'head',lambda key:SimpleNamespace(size=len(files[key])))
    monkeypatch.setattr(blob,'delete',lambda key:files.pop(key))
    monkeypatch.setenv('STORAGE_PROVIDER','vercel-blob')
    store=storage.get_storage();meta=store.save('cv/1/cv-test.txt',b'Ada Example\nPython')
    assert meta['path']=='blob:cv/1/cv-test.txt' and 'url' not in meta
    assert cv_parser.extract_text(Path(meta['path']))['text']=='Ada Example\nPython'
    for key in ('../secret','/absolute','https://example.org','a\\b'):
        with pytest.raises(ValueError):store.open(key)
    with pytest.raises(OSError):storage.read_reference('/tmp/private.txt')


def test_vercel_cannot_start_with_local_auth_or_ephemeral_database(monkeypatch,tmp_path):
    monkeypatch.setenv('VERCEL','1');monkeypatch.setenv('APP_ENV','local')
    assert 'Vercel requires APP_ENV=production' in config.validate_production()
    monkeypatch.setenv('APP_ENV','production');monkeypatch.delenv('DATABASE_PROVIDER',raising=False)
    assert 'Vercel requires a durable remote database' in config.validate_production(tmp_path/'missing.db')


def test_release_bundle_excludes_data_credentials_and_keeps_runtime_assets(tmp_path):
    from scripts.prepare_vercel_release import prepare
    for name in ('templates','static','uploads','.private'):(tmp_path/name).mkdir()
    for name in ('app.py','requirements.txt','vercel.json','pyproject.toml',
                 'background_subscriber.py','templates/base.html','static/translations.json'):
        (tmp_path/name).write_text('public source')
    for name in ('.env.local','jobs.db','uploads/private.pdf','.private/passwords.txt'):
        (tmp_path/name).write_text('PRIVATE SENTINEL')
    release,count=prepare(tmp_path)
    assert count==7
    assert (release/'static/translations.json').exists()
    assert not any('PRIVATE SENTINEL' in p.read_text() for p in release.rglob('*') if p.is_file())


def test_pyproject_cloud_extra_keeps_queue_runtime_dependencies():
    import tomllib

    config_data = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )
    project = config_data["project"]
    dependencies = project["dependencies"] + project["optional-dependencies"]["cloud"]
    assert any(item.startswith("vercel==") for item in dependencies)
    assert any(item.startswith("libsql==") for item in dependencies)
    assert "fastapi" in dependencies
    subscriber = config_data["tool"]["vercel"]["subscribers"][0]
    assert subscriber == {
        "entrypoint": "background_subscriber:consume_work",
        "topics": ["oe-work-v1"],
    }


def test_pilot_provisioning_is_repeatable_without_resetting_credentials(cloud,monkeypatch):
    from scripts.provision_cloud_pilot import provision
    monkeypatch.setenv('APP_ENV','production')
    accounts=[{'email':f'pilot{i}@example.org','password':f'fixture-password-long-enough-{i}',
               'tenant_id':i,'display_name':f'Pilot {i}'} for i in (1,2)]
    assert provision(accounts)==[1,2]
    assert provision(accounts)==[1,2]
    accounts[0]['password']='different-password-long-enough'
    with pytest.raises(RuntimeError,match='no reset performed'):provision(accounts)
