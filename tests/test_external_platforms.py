"""Country/type routing and safe external links; no live provider calls."""
import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
import app
import config
import external_platforms
import i18n


def test_directory_is_explicit_and_translated():
    entries = external_platforms.PLATFORMS
    assert len({p['name'] for p in entries}) == len(entries)
    for p in entries:
        assert urlparse(p['url']).scheme == 'https'
        assert set(p['countries']) <= config.SEARCH_COUNTRIES.keys()
        assert set(p['kinds']) <= set(config.KINDS)
        assert p['note'] in i18n.CATALOG


@pytest.mark.parametrize('locale', ['sq', 'en', 'de'])
def test_directory_page_renders_without_provider_requests(locale, monkeypatch):
    import ai_service
    monkeypatch.setattr(ai_service, '_provider_call', lambda *_: pytest.fail('No AI on page load'))
    with TestClient(app.app) as client:
        response = client.get('/discover', params={'lang':locale,'kind':'local_job'})
    assert response.status_code == 200
    assert 'Kleinanzeigen' in response.text
    assert 'external-platforms.js' in response.text
    assert 'rel="noopener noreferrer"' in response.text


def test_search_links_follow_country_categories_and_current_form():
    source = Path('static/external-platforms.js').read_text()
    script = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const sandbox={URL,window:{},document:{querySelector:()=>null}};
vm.runInNewContext(SOURCE,sandbox);
const {supported,searchURL}=sandbox.window.OEPlatforms;
assert.equal(supported({kinds:['job','local_job'],countries:['de']},'local_job','de'),true);
assert.equal(supported({kinds:['job','local_job'],countries:['de']},'local_job','ch'),false);
assert.equal(supported({kinds:['freelance'],countries:[]},'job','de'),false);
assert.equal(supported({kinds:['freelance'],countries:[]},'freelance','us'),true);
const fields={kind:'local_job',country:'de',what:'C++ & care',where:'Essen',categories:['dog','cats']};
const terms={dog:['Gassi gehen','dog walking'],cats:['Katzenbetreuung','cat sitting']};
const url=new URL(searchURL('kleinanzeigen.de/s-anzeige/',fields,terms,'sq'));
assert.equal(url.origin,'https://www.google.com');
const q=url.searchParams.get('q');
for(const value of ['site:kleinanzeigen.de/s-anzeige/','C++ & care','Essen','"Gassi gehen" OR "Katzenbetreuung"']) assert.ok(q.includes(value));
assert.equal(url.searchParams.get('hl'),'sq');
assert.equal(url.searchParams.get('gl'),'de');
assert.ok(q.includes('Deutschland'));
assert.ok(q.includes('Minijob OR Teilzeit OR Nebenjob OR gesucht'));
const other=new URL(searchURL('linkedin.com/jobs/view/',{...fields,kind:'job',country:'gb',where:''},terms,'en'));
assert.ok(!other.searchParams.get('q').includes('Gassi'));
assert.ok(!other.searchParams.get('q').includes('Essen'));
assert.ok(other.searchParams.get('q').includes('job vacancy'));
const escaped=new URL(searchURL('kleinanzeigen.de', {...fields,what:'<script>&x=bad#hash'},terms,'de'));
assert.equal(escaped.hash,'');assert.equal(escaped.searchParams.get('x'),null);
'''.replace('SOURCE',json.dumps(source))
    subprocess.run(['node','-e',script],check=True)
