"""Locale regressions across server rendering, fragments and browser contracts."""
import asyncio
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

import httpx
from jinja2 import nodes
import pytest
from fastapi.testclient import TestClient

import app
import i18n
import repository as repo
from test_ui_views import Elements

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('locale,label', [('sq', 'Profili im'), ('en', 'My profile'), ('de', 'Mein Profil')])
@pytest.mark.parametrize('path', ['/', '/profile', '/jobs/add'])
def test_pages_and_cookie_navigation(locale, label, path):
    with TestClient(app.app) as client:
        response = client.get(path, params={'lang': locale})
        assert response.status_code == 200
        assert f'<html lang="{locale}"' in response.text
        assert label in response.text
        assert response.headers['Content-Language'] == locale
        assert 'Cookie' in response.headers['Vary']
        assert client.cookies['oe_lang'] == locale
        assert f'<html lang="{locale}"' in client.get('/profile').text
        assert i18n.get_locale() == 'sq'


def test_invalid_locale_is_safe_and_valid_cookie_wins():
    with TestClient(app.app) as client:
        client.cookies.set('oe_lang', '<script>')
        assert '<html lang="sq"' in client.get('/?lang=fr').text
        client.cookies.clear()
        client.get('/?lang=de')
        assert '<html lang="de"' in client.get('/?lang=invalid').text
        assert '<html lang="en"' in client.get('/?lang=en').text


def test_language_form_retains_filters_and_escapes_input():
    with TestClient(app.app) as client:
        response = client.get('/', params={'lang': 'de', 'q': '<img src=x>', 'mode': 'local', 'mins': 63})
    dom = Elements(response.text)
    assert dom.find('input', type='hidden', name='q')[0]['value'] == '<img src=x>'
    assert dom.find('input', type='hidden', name='mode')[0]['value'] == 'local'
    assert not dom.find('img', src='x')
    assert not dom.find('input', type='hidden', name='lang')
    assert 'Übereinstimmung ≥ 63%' in response.text


def test_languages_do_not_change_form_submission_values():
    def values(html):
        return [(tag, attrs.get('name'), attrs.get('value')) for tag, attrs in Elements(html).nodes
                if tag in ('option', 'input') and attrs.get('name') != 'lang']
    with TestClient(app.app) as client:
        for path in ['/jobs/add?kind=product_lead', '/profile']:
            assert values(client.get(path + '&lang=en' if '?' in path else path + '?lang=en').text) == values(client.get(path + '&lang=de' if '?' in path else path + '?lang=de').text)
        html = client.get('/jobs/add?lang=de').text
        assert '<option value="Automatizim">Automatisierung</option>' in html
        profile = client.get('/profile?lang=de').text
        assert 'value="public transport"' in profile and 'Öffentliche Verkehrsmittel' in profile


def test_drawer_translates_controls_but_preserves_user_content():
    _, oid = repo.upsert_opportunity_full({
        'url': 'https://locale-fixture.example/opportunity', 'title': 'Mundësitë',
        'company': 'Ruaj', 'kind': 'job', 'description': 'Profili im <script>alert(1)</script>', 'tags': [],
    })
    try:
        with TestClient(app.app) as client:
            client.get('/?lang=de')
            response = client.get(f'/api/jobs/{oid}/panel')
            assert response.status_code == 200
            assert response.headers['Content-Language'] == 'de'
            assert 'Mundësitë' in response.text and 'Ruaj' in response.text
            assert 'Profili im &lt;script&gt;' in response.text
            assert 'Notizen' in response.text
            assert not Elements(response.text).find('script')
            assert repo.get_opportunity(oid)['status'] == 'new'
    finally:
        repo.delete_opportunity(oid)


def test_concurrent_requests_keep_locale_isolated(monkeypatch):
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url='http://test') as client:
            locales = ['sq', 'en', 'de'] * 3
            responses = await asyncio.gather(*(client.get('/login', params={'lang': locale}) for locale in locales))
            for locale, response in zip(locales, responses):
                assert f'<html lang="{locale}"' in response.text
            assert i18n.get_locale() == 'sq'
    # Local login redirects; production shows the unauthenticated page.
    monkeypatch.setenv('APP_ENV', 'production')
    asyncio.run(check())


def test_language_query_cannot_bypass_auth_and_https_cookie_is_secure(monkeypatch):
    monkeypatch.setenv('APP_ENV', 'production')
    with TestClient(app.app, base_url='https://test') as client:
        response = client.get('/profile?lang=de', follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == '/login'
        assert 'Secure' in response.headers['set-cookie']
        login = client.get('/login')
        assert '<html lang="de"' in login.text
        assert client.get('/api/profiles?lang=en').status_code == 401


def test_catalog_covers_explicit_ui_copy_and_preserves_placeholders():
    keys = set(i18n.SYSTEM_PATTERNS)
    for path in (ROOT / 'templates').glob('*.html'):
        tree = app.templates.env.parse(path.read_text())
        for call in tree.find_all(nodes.Call):
            if isinstance(call.node, nodes.Name) and call.node.name in ('_', 'ui') and call.args and isinstance(call.args[0], nodes.Const):
                keys.add(call.args[0].value)
    for path in (ROOT / 'static').glob('*.js'):
        keys.update(ast.literal_eval(m) for m in re.findall(r'''\b(?:tr|t|ui)\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''', path.read_text()))
    assert not keys - i18n.CATALOG.keys(), sorted(keys - i18n.CATALOG.keys())
    for source, translations in i18n.CATALOG.items():
        assert set(translations) == {'en', 'de'}
        for translated in translations.values():
            assert translated.strip()
            assert set(re.findall(r'\{\w+\}', source)) == set(re.findall(r'\{\w+\}', translated)), source


def test_inline_confirmations_use_the_translation_catalog():
    for path in (ROOT / 'templates').glob('*.html'):
        text = path.read_text()
        assert not re.search(r'''\b(?:confirm|alert|prompt)\(\s*['\"]''', text), path
    profile = (ROOT / 'templates' / 'profile.html').read_text()
    assert 'data-confirm="{{ _(\'Të fshihet produkti?\') }}"' in profile


def test_literal_api_errors_and_reasons_are_localizable():
    keys = set()
    for path in ROOT.glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value in ('error', 'reason')
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and value.value):
                    keys.add(value.value)
    assert not keys - i18n.CATALOG.keys(), sorted(keys - i18n.CATALOG.keys())


def test_browser_translator_fallback_and_interpolation():
    if not shutil.which('node'):
        pytest.skip('Node is required for frontend contract check')
    script = """
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
const context = {window: {__I18N__: {'Hi {name}': 'Hallo {name}'}}, document: {getElementById: () => null}};
vm.runInNewContext(fs.readFileSync('static/i18n.js', 'utf8'), context);
const t = context.window.OEI18n.t;
assert.equal(t('Hi {name}', {name: '<user>'}), 'Hallo <user>');
assert.equal(t('User text'), 'User text');
assert.equal(t('constructor'), 'constructor');
assert.equal(t('Hi {name}'), 'Hallo {name}');
"""
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True)


def test_language_navigation_retains_query_anchor_and_restores_current_selection():
    if not shutil.which('node'):
        pytest.skip('Node is required for frontend contract check')
    script = """
const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
let submit, destination;
const form = {elements: {lang: {value: 'de'}}, addEventListener: (_, handler) => {submit = handler;}};
const context = {URL, window: {}, document: {documentElement: {lang: 'en'}, getElementById: () => form},
  location: {href: 'https://local.example/profile?q=Python&mode=jobs&lang=en#strukture', assign: url => {destination = url;}}};
vm.runInNewContext(fs.readFileSync('static/i18n.js', 'utf8'), context);
submit({preventDefault() {}});
const url = new URL(destination);
assert.equal(url.searchParams.get('lang'), 'de');
assert.equal(url.searchParams.get('q'), 'Python');
assert.equal(url.searchParams.get('mode'), 'jobs');
assert.equal(url.hash, '#strukture');
assert.equal(form.elements.lang.value, 'en');
destination = undefined;
let feedback;
context.window.OE = {hasPendingEdits: () => true, toast: text => {feedback = text;}};
form.elements.lang.value = 'sq';
submit({preventDefault() {}});
assert.equal(destination, undefined);
assert.ok(feedback);
assert.equal(form.elements.lang.value, 'en');
"""
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True)


def test_system_evidence_localizes_patterns_without_rewriting_values():
    token = i18n._locale.set('de')
    try:
        assert i18n.ui_text('pagë 12.5€/h nën minimumin 15€/h') == 'Vergütung 12.5 €/h unter dem Minimum von 15 €/h'
        assert i18n.ui_text('2/3 terma · po mësohet: Ruaj') == '2/3 Begriffe · Wird erlernt: Ruaj'
        assert i18n.ui_text('distanca e panjohur — nuk gjykohet') == 'Entfernung unbekannt — nicht bewertet'
        assert i18n.ui_text('An unknown provider message') == 'An unknown provider message'
    finally:
        i18n._locale.reset(token)


@pytest.mark.parametrize('locale', ['sq', 'en', 'de'])
def test_dynamic_source_and_country_copy_is_covered(locale):
    import config
    import connectors
    keys = set(config.SEARCH_COUNTRIES.values()) | set(config.LOCAL_CATEGORY_LABELS.values())
    keys |= set(config.STATUS_LABELS.values()) | set(config.KIND_LABELS.values())
    # Provider brand names remain unchanged; every description must have translations.
    keys |= {c.note for c in connectors.REGISTRY.values() if c.note}
    keys |= {c.name for c in connectors.REGISTRY.values() if any(word in c.name for word in ('Punë','Tendera','Kontrata','Feed-e'))}
    assert not keys - i18n.CATALOG.keys(), sorted(keys - i18n.CATALOG.keys())
    with TestClient(app.app) as client:
        html = client.get('/discover', params={'lang':locale}).text
        if locale != 'sq':
            for source in ('Bundesnetzagentur · Punë','Bundesnetzagentur · Tendera','Remotive Kontrata','Feed-e lokale'):
                assert f'<strong>{source}</strong>' not in html


def test_api_fragments_follow_the_requesting_tab_without_changing_ui_cookie():
    _, oid = repo.upsert_opportunity_full({'url':'https://locale-fixture.example/tabs', 'title':'User title', 'kind':'job', 'tags':[]})
    try:
        with TestClient(app.app) as client:
            client.get('/?lang=de')
            response = client.get(f'/api/jobs/{oid}/panel', headers={'X-UI-Language':'en'})
            assert response.headers['Content-Language'] == 'en'
            assert 'My notes' in response.text
            assert client.cookies['oe_lang'] == 'de'
            assert client.get(f'/api/jobs/{oid}/panel', headers={'X-UI-Language':'invalid'}).headers['Content-Language'] == 'de'
            client.get('/api/profile/resume.pdf?lang=sq')
            assert client.cookies['oe_lang'] == 'de'
            assert '<html lang="de"' in client.get('/profile').text
    finally:
        repo.delete_opportunity(oid)


def test_client_system_errors_and_cv_navigation_are_localized_without_rewriting_values():
    if not shutil.which('node'):
        pytest.skip('Node is required for frontend contract check')
    script = r"""
const vm=require('node:vm'), fs=require('node:fs'), assert=require('node:assert/strict');
const catalog=JSON.parse(fs.readFileSync('static/translations.json','utf8'));
let submit, destination, cvForm;
const form={elements:{lang:{value:'en'},cv_language:{value:'de'}},addEventListener:(_,fn)=>{submit=fn;}};
const window={__I18N__:Object.fromEntries(Object.entries(catalog).map(([k,v])=>[k,v.de])),
  __UI_PATTERNS__:['burimi i papërdorshëm: {value}','{n}/{total} faqe punëdhënësi dështuan: {value}','tipare për rolin {value}'],
  __SOURCE_LABELS__:{bund_jobs:'Bundesnetzagentur · Stellenangebote'}};
vm.runInNewContext(fs.readFileSync('static/i18n.js','utf8'),{window, URL,
 FormData:class {constructor(form){this.entries=form.entries;} [Symbol.iterator](){return this.entries[Symbol.iterator]();}},
 document:{documentElement:{lang:'sq'},getElementById:id=>id==='language-form'?form:cvForm},
 location:{href:'https://local.example/profile/resume?lang=sq&msg=Ruaj#preview',assign:url=>{destination=url;}}});
assert.equal(window.OEI18n.ui('1/2 faqe punëdhënësi dështuan: burimi i papërdorshëm: HTTP 503'),'1/2 Arbeitgeberseiten fehlgeschlagen: Quelle nicht verfügbar: HTTP 503');
assert.equal(window.OEI18n.ui('tipare për rolin Ruaj'),'Übliche Fähigkeiten für Ruaj');
assert.equal(window.OEI18n.sourceLabel('bund_jobs'),'Bundesnetzagentur · Stellenangebote');
assert.equal(window.OEI18n.sourceLabel('constructor'),'constructor');
submit({preventDefault(){}});
const url=new URL(destination);
assert.equal(url.searchParams.get('cv_language'),'de');
assert.equal(url.searchParams.get('lang'),'en');
assert.equal(url.searchParams.has('msg'),false);
assert.equal(url.hash,'#preview');
cvForm={id:'resume-options',entries:[['cv_language','sq'],['skill_limit','40'],['configured','yes'],['projects','yes']]};
form.elements.lang.value='de';
submit({preventDefault(){}});
const updated=new URL(destination);
assert.equal(updated.searchParams.get('cv_language'),'sq');
assert.equal(updated.searchParams.get('skill_limit'),'40');
assert.equal(updated.searchParams.get('projects'),'yes');
assert.equal(updated.searchParams.has('photo'),false);
assert.equal(updated.searchParams.get('lang'),'de');
"""
    subprocess.run(['node','-e',script],cwd=ROOT,check=True)
