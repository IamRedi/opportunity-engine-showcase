"""Behavioral acceptance for opt-in CV autofill and evidence-gated extraction."""
import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app
import config
import context
import cv_ai
import cv_merge
import cv_parser
import repository as repo
import storage


@pytest.fixture(autouse=True)
def isolated_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'DB_PATH', tmp_path / 'cv.sqlite3')
    monkeypatch.setattr(storage, 'get_storage', lambda: storage.LocalStorage(tmp_path / 'uploads'))
    repo.init()


def upload(text, **data):
    with TestClient(app.app) as c:
        r = c.post('/api/profile/cv', data=data, files={'file': ('profile.txt', text.encode(), 'text/plain')})
        assert r.status_code == 200, r.text
        return r.json()


def test_local_autofill_populates_fields_and_keeps_manual_values():
    repo.update_profile_fields({'name': 'Manual Name'})
    r = upload('Name: CV Name\nTitle: Python Developer\nLocation: Essen\nEmail: cv@example.com\nPhone: +49 170 1234567\nSkills: Python, SQL, Git\nLanguages: German – B2\nSummary:\nI build internal software tools with Python and SQL.\nEducation:\nBSc Computer Science\n', profile_mode='autofill')
    assert r['processing']['autofill']['merged'] >= 7
    p = repo.get_profile()
    assert p['name'] == 'Manual Name' and p['title'] == 'Python Developer'
    assert p['email'] == 'cv@example.com' and p['phone'] == '+49 170 1234567'
    assert p['location'] == 'Essen' and set(p['skills']) == {'Python', 'SQL', 'Git'}
    assert p['summary'] == 'I build internal software tools with Python and SQL.'
    assert repo.get_profile_section('education')[0][0]['title'] == 'BSc Computer Science'
    assert r['processing']['autofill']['conflicts'] == 1


def test_upload_and_review_modes_do_not_change_profile_or_call_ai(monkeypatch):
    monkeypatch.setattr(cv_ai, 'extract', lambda text: pytest.fail('AI must be explicitly selected'))
    before = repo.get_profile()
    for mode in ['upload', 'review']:
        upload('Name: New Name\nSkills: Python, SQL\n', profile_mode=mode)
        assert repo.get_profile() == before
        assert repo.get_profile_section('skills')[0] == []


def test_evidence_validation_rejects_invention_and_unknown_fields():
    text = 'Candidate Example\nPython Developer\nPython, SQL\nDeveloper at Studio, 2020–2023'
    data = {'items': [
        {'section': 'contact', 'fields': {'name': 'Candidate Example'}, 'evidence': 'Candidate Example'},
        {'section': 'skills', 'fields': {'name': 'Kubernetes'}, 'evidence': 'Python, SQL'},
        {'section': 'skills', 'fields': {'name': 'SQL'}, 'evidence': 'SQL and AWS'},
        {'section': 'contact', 'fields': {'salary': '2020'}, 'evidence': 'Developer at Studio, 2020–2023'},
    ]}
    suggestions, rejected = cv_ai.checked_suggestions(data, text)
    assert len(suggestions) == 1 and rejected == 3
    assert suggestions[0]['proposed_value'] == 'Candidate Example'


def test_ai_import_preserves_structured_dates_orgs_levels_and_every_supported_section(monkeypatch):
    fields = {
        'contact': {'name': 'Candidate Example', 'title': 'Python Developer', 'email': 'example@test.local', 'phone': '+49 170 1234567', 'location': 'Essen', 'summary': 'I build software tools.'},
        'skills': {'name': 'Python', 'level': 'Advanced'},
        'languages': {'name': 'German', 'level': 'B2'},
        'experience': {'title': 'Developer', 'org': 'Studio', 'period': '2020–2023', 'description': 'Built internal tools.'},
        'education': {'title': 'BSc Informatics', 'org': 'University', 'period': '2017–2020'},
        'certifications': {'name': 'Cloud Certificate', 'org': 'Institute', 'year': '2024'},
        'portfolio': {'title': 'Portfolio', 'url': 'https://portfolio.example', 'description': 'Selected work'},
        'projects': {'title': 'Booking App', 'url': 'https://projects.example', 'description': 'Appointment software', 'stack': 'Python'},
    }
    text = '\n'.join('\n'.join(values.values()) for values in fields.values())
    payload = {'items': [{'section': section, 'fields': values, 'evidence': '\n'.join(values.values())} for section, values in fields.items()]}
    calls = []
    def extract(source):
        calls.append(source)
        suggestions, rejected = cv_ai.checked_suggestions(payload, source)
        return {'ok': True, 'suggestions': suggestions, 'rejected': rejected}
    monkeypatch.setattr(cv_ai, 'extract', extract)
    result = upload(text, profile_mode='autofill', use_ai='on')
    assert calls == [text]
    assert result['processing']['autofill']['merged'] == 13
    assert repo.get_profile()['name'] == 'Candidate Example'
    for section, values in fields.items():
        if section == 'contact': continue
        entry = repo.get_profile_section(section)[0][0]
        assert all(entry[k] == value for k, value in values.items())
        assert entry['source'].startswith('cv:')
    assert 'Built internal tools.' in repo.get_profile()['experience']
    again = repo.autofill_cv_suggestions(result['cv']['id'])
    assert again['merged'] == 0


def test_ai_failure_returns_local_fallback_without_claiming_full_ai_success(monkeypatch):
    monkeypatch.setattr(cv_ai, 'extract', lambda text: {'ok': False, 'reason': 'Leximi me AI dështoi; u përdor leximi lokal.'})
    result = upload('Skills: Python, SQL, Git', profile_mode='autofill', use_ai='on')
    assert result['processing']['warning'] and result['processing']['autofill']['merged'] == 3


def test_rejected_low_confidence_and_legacy_suggestions_are_not_auto_applied():
    result = upload('Skills: Python\nI used Docker in a project.', profile_mode='review')
    cv_id = result['cv']['id']
    python = next(s for s in repo.cv_suggestions(cv_id) if s['normalized_value'] == 'python')
    repo.set_cv_suggestion_status(python['id'], 'rejected')
    repo.create_cv_suggestion(cv_id, 'contact', 'name', 'Old Parser Name', 'old parser name', 'old', .95, 'cv:old')
    processed = app._process_cv(cv_id, True, False)
    assert repo.get_profile()['name'] == ''
    assert 'Python' not in repo.get_profile()['skills']
    assert 'docker' not in repo.get_profile()['skills']
    assert processed['autofill']['pending'] >= 1


def test_autofill_is_idempotent_under_concurrent_requests():
    result = upload('Name: Candidate\nSkills: Python, SQL', profile_mode='review')
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: repo.autofill_cv_suggestions(result['cv']['id']), range(2)))
    assert sum(r['merged'] for r in outputs) == 3
    assert len(repo.get_profile_section('skills')[0]) == 2


def test_other_users_cannot_parse_or_autofill_cv(monkeypatch):
    result = upload('Name: Private Candidate\nSkills: Python', profile_mode='review')
    other = context.ActorContext(user_id=999, tenant_id=999)
    monkeypatch.setattr(cv_ai, 'extract', lambda text: pytest.fail('Foreign CV must never be sent to AI'))
    with context.run_as(other):
        assert not app._process_cv(result['cv']['id'], True, True)['ok']
        assert not repo.autofill_cv_suggestions(result['cv']['id'])['ok']
    assert repo.get_profile()['name'] == ''


def test_scanned_pdf_is_reported_as_no_text(tmp_path):
    from pypdf import PdfWriter
    writer = PdfWriter(); writer.add_blank_page(width=612, height=792)
    path = tmp_path / 'scan.pdf'
    with path.open('wb') as f: writer.write(f)
    result = cv_parser.extract_text(path)
    assert result['status'] == 'no_text' and 'OCR' in result['error']


def test_parser_does_not_import_headers_entire_cv_summary_or_dates_as_phone():
    text = 'Summary:\nI build internal Python systems for local businesses.\nEducation:\nBSc Computer Science\nExperience:\nEngineer @ Studio | 2020 - 2023\nSkills:\nPython, SQL\n'
    rows = cv_parser.parse_suggestions(text)
    assert all(s['proposed_value'] not in ('Skills','Education','Experience') for s in rows)
    assert not any(s['field'] == 'phone' for s in rows)
    summary = next(s['proposed_value'] for s in rows if s['section'] == 'summary')
    assert 'Education' not in summary and '2023' not in summary


def test_no_javascript_processing_form_has_working_redirect():
    upload('Name: Candidate\nSkills: Python', profile_mode='upload')
    with TestClient(app.app) as c:
        c.get('/profile?lang=en')
        response = c.post('/profile/cv/parse', data={'profile_mode': 'autofill'}, follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'].endswith('#cv')
        assert repo.get_profile()['name'] == 'Candidate'


def test_import_keeps_legacy_manual_skills_visible_to_matching():
    import profile_context
    p = repo.get_profile()
    repo.save_profile(p | {'skills': 'Rust', 'languages': 'English – C1', 'products': ''})
    upload('Skills: Python, SQL', profile_mode='autofill')
    assert {'Rust', 'Python', 'SQL'} == {e['name'] for e in profile_context.build_context()['skills']}


def test_replacement_cv_cannot_receive_old_cv_suggestions():
    first = upload('Skills: Python', profile_mode='upload')
    upload('Skills: SQL', profile_mode='upload')
    assert repo.create_cv_suggestion(first['cv']['id'], 'skills', 'name', 'Old Skill', 'old skill', 'old', .9, 'cv:old') is None
    assert not repo.autofill_cv_suggestions(first['cv']['id'])['ok']


def test_partial_ai_language_extraction_keeps_other_locally_evidenced_languages(monkeypatch):
    def extract(text):
        suggestions, rejected = cv_ai.checked_suggestions({'items': [
            {'section': 'languages', 'fields': {'name': 'German', 'level': 'B2'}, 'evidence': 'German – B2'}]}, text)
        return {'ok': True, 'suggestions': suggestions, 'rejected': 1}
    monkeypatch.setattr(cv_ai, 'extract', extract)
    upload('Languages:\nGerman – B2\nEnglish – C1\nAlbanian – native', profile_mode='autofill', use_ai='on')
    assert {'German','English','Albanian'} == {e['name'] for e in repo.get_profile_section('languages')[0]}


def test_autofill_refreshes_existing_opportunity_scores():
    _, oid = repo.upsert_opportunity_full({'url': 'https://cv-scoring.example/python', 'title': 'Python Developer', 'description': 'Python SQL Git', 'kind': 'job'})
    with repo.db() as con:
        con.execute('UPDATE opportunities SET rule_score=-1,final_score=-1 WHERE id=?', (oid,))
    result = upload('Skills: Python, SQL, Git', profile_mode='autofill')
    assert result['processing']['rescored'] == 1
    assert repo.get_opportunity(oid)['rule_score'] >= 0


def test_local_autofill_does_not_treat_institutions_or_years_as_certificates():
    upload('Certifications:\nPython Certificate\nExample Institute\n2023\nEducation:\nBSc Informatics\nExample University\n2018–2021', profile_mode='autofill')
    assert [e['name'] for e in repo.get_profile_section('certifications')[0]] == ['Python Certificate']
    assert [e['title'] for e in repo.get_profile_section('education')[0]] == ['BSc Informatics']


def test_empty_upload_keeps_previous_cv_and_whitespace_does_not_call_ai(monkeypatch):
    old = upload('Skills: Python', profile_mode='upload')['cv']['id']
    with TestClient(app.app) as c:
        response = c.post('/api/profile/cv', files={'file': ('empty.txt', b'', 'text/plain')})
        assert response.status_code == 400 and repo.get_cv()['id'] == old
    monkeypatch.setattr(cv_ai, 'extract', lambda text: pytest.fail('Blank text must not call AI'))
    result = upload('  \n\t', profile_mode='autofill', use_ai='on')
    assert not result['processing']['ok']


@pytest.mark.parametrize('text', [
    'Emri: Mira Example\nTitulli profesional: Python Developer\nVendndodhja: Essen\nAftësitë: Python, SQL\nGjuhët:\nGjermanisht – B2',
    'Name: Mira Example\nBerufsbezeichnung: Python Developer\nWohnort: Essen\nKenntnisse: Python, SQL\nSprachen:\nDeutsch – B2',
])
def test_local_cv_labels_in_albanian_and_german(text):
    upload(text, profile_mode='autofill')
    profile = repo.get_profile()
    assert profile['name'] == 'Mira Example' and profile['title'] == 'Python Developer'
    assert profile['location'] == 'Essen' and set(profile['skills']) == {'Python','SQL'}
    assert repo.get_profile_section('languages')[0][0]['level'] == 'B2'


def test_ai_layout_linebreaks_do_not_create_duplicate_education():
    text = 'Master in\nTeaching\nExample University'
    items = [{'section': 'education', 'fields': {'title': title, 'org': 'Example University'}, 'evidence': text}
             for title in ('Master in Teaching', 'Master in\nTeaching')]
    suggestions, rejected = cv_ai.checked_suggestions({'items': items}, text)
    assert rejected == 0
    assert suggestions[0]['normalized_value'] == suggestions[1]['normalized_value']
    assert json.loads(suggestions[1]['proposed_value'])['title'] == 'Master in Teaching'


def test_cv_prompt_preserves_literal_text_without_double_json_escaping(monkeypatch):
    captured = []
    class Provider:
        def __init__(self, cfg): pass
        def complete(self, prompt):
            captured.append(prompt)
            return '{"items": []}', {}
    monkeypatch.setattr(cv_ai.ai_service, 'get_config', lambda: {'provider': 'fixture', 'model': 'fixture', 'timeout': 45})
    monkeypatch.setitem(cv_ai.ai_service._PROVIDERS, 'fixture', Provider)
    text = 'Master in\nTeaching\nUniversity "Example"'
    assert cv_ai.extract(text)['ok']
    assert captured[0].endswith(text)
