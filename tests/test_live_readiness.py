"""Regressions found by the owner-authorized live source/AI pilot."""
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import ai_service
import connectors
import generator
import repository as repo
import search_apis
import source_http


def test_source_cache_shares_payload_and_expires_without_stale_success(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={'jobs': []}, request=httpx.Request('GET', url))
    monkeypatch.setattr(source_http.httpx, 'get', fetch)
    url = 'https://remotive.com/api/remote-jobs'
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(x == {'jobs': []} for x in pool.map(lambda _: source_http.get_json(url, {'limit': 50}), range(4)))
    assert len(calls) == 1
    original_time = time.time()
    monkeypatch.setattr(source_http.time, 'time', lambda: original_time + 21601)
    def fail(url, **kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(source_http.httpx, 'get', fail)
    with pytest.raises(httpx.ConnectError):
        source_http.get_json(url, {'limit': 50})


def test_credentials_and_private_hosts_never_enter_public_cache(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={'jobs': []}, request=httpx.Request('GET', url))
    monkeypatch.setattr(source_http.httpx, 'get', fetch)
    for url, params in [('https://example.com/feed', {}), ('https://remotive.com/api/remote-jobs', {'api_key': 'synthetic-only'}), ('https://remotive.com/api/remote-jobs?token=synthetic', {})]:
        for _ in range(2): source_http.get_json(url, params)
    assert len(calls) == 6
    assert not source_http.CACHE_DIR.exists()


def test_corrupt_or_unwritable_cache_does_not_break_source(monkeypatch, tmp_path):
    def fetch(url, **kwargs):
        return httpx.Response(200, json={'jobs': []}, request=httpx.Request('GET', url))
    monkeypatch.setattr(source_http.httpx, 'get', fetch)
    url = 'https://jobicy.com/api/v2/remote-jobs'
    source_http.get_json(url)
    next(source_http.CACHE_DIR.glob('*.json')).write_text('{broken')
    assert source_http.get_json(url) == {'jobs': []}
    blocked = tmp_path / 'file'; blocked.write_text('not a directory')
    monkeypatch.setattr(source_http, 'CACHE_DIR', blocked)
    assert source_http.get_json(url) == {'jobs': []}


@pytest.mark.parametrize('text,expected', [
    ('SEEKING WORK | React developer, looking for a freelance project', False),
    ('**SEEKING WORK** Looking for a Python developer role', False),
    ('FOR HIRE — Python developer available', False),
    ('Freelance developer available for work', False),
    ('SEEKING FREELANCER | Build a booking app', True),
    ('Looking for a Django developer for a three month project', True),
    ('We need an automation for our invoicing workflow', True),
])
def test_hn_distinguishes_hiring_from_job_seekers(text, expected):
    assert search_apis._hn_is_hiring(text) is expected


def test_hn_scans_past_job_seekers_to_requested_hiring_results(monkeypatch):
    comments = [{'id': i, 'text': 'SEEKING WORK: Python developer seeking a project. Remote.'} for i in range(60)]
    comments.append({'id': 100, 'text': 'SEEKING FREELANCER: Build a booking website for our studio. Remote.'})
    monkeypatch.setattr(search_apis, '_get', lambda url, params=None: {'hits': [{'objectID': 1}]} if 'search_by_date' in url else {'children': comments})
    for fn in [search_apis.hackernews_freelance, search_apis.hackernews_leads]:
        items = fn(limit=1)
        assert len(items) == 1 and items[0]['external_id'] == 'hn-100'


def test_collection_limit_bounds_persistence_and_run_counts(monkeypatch):
    items = [{'url': f'https://bounded-pilot.example/{n}', 'title': f'Pilot {n}', 'kind': 'job', 'description': 'Python'} for n in range(6)]
    monkeypatch.setattr(connectors.REGISTRY['arbeitnow'], 'search', lambda query: connectors.ConnectorResult('arbeitnow', items))
    result = connectors.run_source_search('arbeitnow', connectors.SearchQuery(limit=2))
    try:
        assert result['ok'] and result['discovered'] == result['new'] == 2
        run = next(r for r in repo.recent_search_runs(limit=20) if r['id'] == result['run_id'])
        assert run['discovered'] == 2
    finally:
        for oid in result['new_ids']: repo.delete_opportunity(oid)


@pytest.mark.parametrize('language,language_name', [('en', 'English'), ('de', 'German'), ('sq', 'Albanian')])
def test_long_listing_retains_language_and_profile_in_valid_bounded_prompt(language, language_name):
    profile = {'name': 'Synthetic Candidate', 'title': 'Python Developer', 'skills': ['Python', 'SQL', 'Git'], 'location': 'Essen'}
    prompt = ai_service.build_input('draft_application', {'title': 'Developer', 'description': 'Long listing "\\\n' * 2000}, profile, {'max_input_chars': 2500}, {'language': language, 'tone': 'concise'})
    assert len(prompt) <= 2500 and f'Write the subject and body in {language_name}.' in prompt
    profile_json = prompt.split('PROFILE:\n')[1].split('\nOPPORTUNITY:')[0]
    assert json.loads(profile_json)['skills'] == ['Python', 'SQL', 'Git']
    assert json.loads(profile_json)['name'] == 'Synthetic Candidate'
    assert 'Never sign with the model or assistant name' in prompt
    assert json.loads(prompt.split('OPPORTUNITY:\n')[1])['desc']


def test_tiny_prompt_budget_fails_before_sending_partial_facts():
    with pytest.raises(ValueError, match='budget'):
        ai_service.build_input('draft_application', {}, {'skills': ['Python']}, {'max_input_chars': 20}, {'language': 'de'})


def test_supplied_profile_location_is_supported_but_invented_location_is_not():
    profile = {'location': 'Essen', 'skills': []}
    assert generator._claims_supported({'used_profile_facts': ['location: Essen']}, profile, {})[0]
    assert not generator._claims_supported({'used_profile_facts': ['location: Oslo']}, profile, {})[0]


def test_connection_view_includes_unconfigured_local_feed_and_ai_state(monkeypatch):
    import app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(connectors.REGISTRY['local_feed'], 'is_configured', lambda: False)
    monkeypatch.setattr(connectors.REGISTRY['adzuna'], 'is_configured', lambda: False)
    cfg = ai_service.get_config() | {'provider': 'ollama', 'model': 'synthetic-test-model', 'api_key': ''}
    monkeypatch.setattr(ai_service, 'get_config', lambda: cfg)
    cards = {card['id']: card for card in app._source_cards()}
    assert set(cards) == set(connectors.REGISTRY)
    assert not cards['local_feed']['ok'] and not cards['adzuna']['ok']
    with TestClient(app.app) as client:
        html = client.get('/profile?lang=en').text
        assert 'AI enabled' in html and 'synthetic-test-model' in html
        assert 'Local feeds' in html and 'Setup required' in html
        workspace = client.get('/?lang=en').text
        assert 'href="/profile#lokale">Configure' in workspace
        assert 'data-src="local_feed"' not in workspace


def test_cloud_transport_sends_configured_credentials_and_parses_usage(monkeypatch):
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'choices': [{'message': {'content': '{"subject":"Hello"}'}}],
            'usage': {'total_tokens': 12},
        })
    monkeypatch.setattr(ai_service.httpx, 'post', post)
    provider = ai_service.OpenAICompatProvider({'base_url': 'https://synthetic.example/v1/',
        'api_key': 'synthetic-key', 'model': 'configured-model', 'timeout': 30})
    output, usage = provider.complete('Synthetic JSON request')
    assert json.loads(output) == {'subject': 'Hello'} and usage == {'total_tokens': 12}
    url, request = calls[0]
    assert url == 'https://synthetic.example/v1/chat/completions'
    assert request['headers']['Authorization'] == 'Bearer synthetic-key'
    assert request['json']['model'] == 'configured-model'
    assert request['json']['messages'] == [
        {'role': 'system', 'content': ai_service.SYSTEM_INSTRUCTIONS},
        {'role': 'user', 'content': 'Synthetic JSON request'}]
    assert request['json']['response_format'] == {'type': 'json_object'}
    assert request['timeout'] == 30


def test_cloud_without_credentials_is_inactive_and_makes_no_request(monkeypatch):
    import app
    cfg = ai_service.get_config() | {'provider': 'openai-compatible', 'model': 'configured-model', 'api_key': '  '}
    monkeypatch.setattr(ai_service, 'get_config', lambda: cfg)
    def forbidden(*args, **kwargs):
        pytest.fail('Unconfigured cloud provider must not make a request or read cached success')
    monkeypatch.setattr(ai_service, '_provider_call', forbidden)
    monkeypatch.setattr(repo, 'ai_cache_get', forbidden)
    result = ai_service.run_task('draft_application', {}, profile={}, cfg=cfg)
    assert not result.ok and result.error == 'AI_API_KEY mungon'
    assert app.api_ai_status()['enabled'] is False


def test_ollama_draft_schema_uses_literal_profile_facts(monkeypatch):
    profile = {'name': 'Synthetic Candidate', 'skills': ['Python', 'SQL'], 'location': 'Essen'}
    schema = ai_service._draft_output_schema(profile)
    evidence = schema['properties']['used_profile_facts']['items']['enum']
    assert set(evidence) == {'Synthetic Candidate', 'Python', 'SQL', 'Essen'}
    assert ai_service._draft_output_schema({})['properties']['used_profile_facts']['maxItems'] == 0
    def post(url, **kwargs):
        assert kwargs['json']['format'] == schema
        assert kwargs['json']['options']['temperature'] == 0
        return httpx.Response(200, request=httpx.Request('POST', url), json={'response': '{}'})
    monkeypatch.setattr(ai_service.httpx, 'post', post)
    provider = ai_service.OllamaProvider({'base_url': 'http://localhost:11434', 'model': 'synthetic', 'timeout': 10, 'response_schema': schema})
    assert provider.complete('Synthetic prompt') == ('{}', None)


def test_duplicate_listing_can_gain_a_lens_without_competing_write_transaction(monkeypatch):
    import classifier
    item = {'url': 'https://lens-refresh.example/job', 'title': 'Engineer', 'description': 'Python', 'source': 'manual', 'kind': 'freelance'}
    _, oid = repo.upsert_opportunity_full(item)
    try:
        # A low busy timeout makes the former nested-connection failure quick;
        # the assertion checks persisted behavior, not execution duration.
        connect = repo.sqlite3.connect
        def short_timeout(*args, **kwargs):
            kwargs['timeout'] = .05
            return connect(*args, **kwargs)
        monkeypatch.setattr(repo.sqlite3, 'connect', short_timeout)
        status, duplicate_id = repo.upsert_opportunity_full(item | {'description': 'Python booking website'})
        assert status == 'dup' and duplicate_id == oid
        assert 'product_lead' in classifier.lenses_for_ids([oid]).get(oid, [])
        repo.upsert_opportunity_full(item | {'description': 'Python booking website'})
        assert classifier.lenses_for_ids([oid])[oid].count('product_lead') == 1
    finally:
        repo.delete_opportunity(oid)


@pytest.mark.parametrize('fact', ['Python and Kubernetes', 'Kubernetes'])
def test_job_requirements_are_not_candidate_evidence(fact):
    profile = {'skills': ['Python', 'SQL', 'Git']}
    assert not generator._claims_supported({'used_profile_facts': [fact]}, profile, {'tags': ['Kubernetes']})[0]


@pytest.mark.parametrize('bad', [None, [123], [{'skill': 'Python'}]])
def test_ai_rejects_null_or_nontext_required_evidence(bad):
    assert not ai_service.validate({'subject': 'Hello', 'body': 'Draft', 'used_profile_facts': bad, 'missing_context': []}, 'draft_application')[0]


def test_ai_cache_changes_when_endpoint_changes():
    cfg = ai_service.get_config() | {'provider': 'openai-compatible', 'model': 'test-model', 'base_url': 'https://one.example/v1'}
    assert ai_service.cache_key('explain', {}, {}, cfg) != ai_service.cache_key('explain', {}, {}, cfg | {'base_url': 'https://two.example/v1'})


def test_source_error_does_not_expose_credential_url():
    url = 'https://api.adzuna.com/jobs?app_key=synthetic-secret'
    request = httpx.Request('GET', url)
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError('Forbidden '+url, request=request, response=response)
    assert connectors._fetch_error(error).endswith('HTTP 403')
    assert 'synthetic-secret' not in connectors._fetch_error(error)


def test_saved_adzuna_secret_is_not_rendered_and_blank_preserves_it():
    import app
    from fastapi.testclient import TestClient
    old = repo.get_setting('adzuna_app_key', '')
    repo.set_setting('adzuna_app_key', 'synthetic-private-key')
    try:
        with TestClient(app.app) as client:
            html = client.get('/profile?lang=en').text
            assert 'synthetic-private-key' not in html
            assert 'type="password" name="adzuna_app_key"' in html
            response = client.post('/settings', data={'adzuna_app_id': 'synthetic-id', 'adzuna_app_key': ''}, follow_redirects=False)
            assert response.headers['location'].endswith('#burimet')
            assert repo.get_setting('adzuna_app_key') == 'synthetic-private-key'
            client.post('/settings', data={'adzuna_app_id': 'synthetic-id', 'adzuna_app_key': 'replacement-key'})
            assert repo.get_setting('adzuna_app_key') == 'replacement-key'
    finally:
        repo.set_setting('adzuna_app_key', old)


def test_supplied_professional_title_is_evidence_but_expanded_claim_is_not():
    profile = {'title': 'Python Developer', 'skills': ['Python', 'SQL', 'Git']}
    assert generator._claims_supported({'used_profile_facts': ['Python Developer']}, profile, {})[0]
    assert not generator._claims_supported({'used_profile_facts': ['Senior Python Developer with ten years experience']}, profile, {})[0]
