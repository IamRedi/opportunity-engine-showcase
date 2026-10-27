"""Create a fictional profile and calculate real matching scores, without providers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def seed(data: Path):
    marker = data / 'showcase-fixture.json'
    db = data / 'jobs.db'
    if db.exists():
        if not marker.exists():
            raise RuntimeError('Refusing to reuse a database without the synthetic fixture marker')
        if json.loads(marker.read_text(encoding='utf-8')).get('fixture') != 'alex-beispiel-v1':
            raise RuntimeError('Unknown demo workspace')
        return
    import repository as repo
    repo.init()
    import context
    with context.run_as(context.dev_context()):
        repo.save_profile({
            'name': 'Alex Beispiel', 'title': 'Python Backend Developer',
            'email': 'alex@candidate.example', 'phone': '', 'location': 'Berlin',
            'summary': 'Backend developer focused on clear APIs, reliable data workflows and useful automation. This is a fictional demonstration profile.',
            'skills': ['Python', 'FastAPI', 'SQL', 'PostgreSQL', 'Docker', 'Git', 'JavaScript'],
            'languages': ['Deutsch – B2', 'English – C1'],
            'experience': '2022–2026 · Backend Developer · Example Software Studio (fictional). Built Python APIs, SQL reporting and automated tests for internal tools.',
            'products': ['API Integration Toolkit'],
        })
        repo.set_profile_section('education', [{'title': 'B.Sc. Computer Science', 'org': 'Example University (fictional)', 'period': '2018–2022'}])
        repo.set_profile_section('experience', [{'title': 'Backend Developer', 'org': 'Example Software Studio (fictional)', 'period': '2022–2026', 'description': 'Built Python APIs, SQL reporting and automated tests for internal tools.'}])
        repo.set_profile_section('projects', [{'title': 'API Integration Toolkit', 'description': 'Fictional portfolio example: a small FastAPI service with tested data imports.', 'stack': 'Python, FastAPI, PostgreSQL'}])
        repo.save_filters({'keywords': ['Python', 'Backend', 'API'], 'exclude_keywords': ['unpaid'],
                           'locations': ['Berlin'], 'remote_ok': 1, 'onsite_ok': 1, 'hybrid_ok': 1, 'salary_min': 50000})
        for key, value in {'ai_provider': 'disabled', 'email_enabled': '0', 'smtp_host': '',
                           'adzuna_app_key': '', 'adzuna_app_id': '', 'usage_enforcement_state': 'disabled'}.items():
            repo.set_setting(key, value)
        roles = [
            ('Python Backend Developer', 'Nordlicht Demo Studio', 'job', 'Python FastAPI SQL PostgreSQL Docker Git. Build and test reliable APIs. English and German. Berlin or remote.', ['Python', 'FastAPI', 'PostgreSQL', 'Docker'], True, 62000, 'new'),
            ('Backend Engineer · API Platform', 'Example Systems', 'job', 'Maintain Python services and PostgreSQL data models. API integrations, Docker and automated testing.', ['Python', 'SQL', 'Docker', 'Git'], True, 68000, 'saved'),
            ('Full Stack Developer', 'Demo Digital Werkstatt', 'job', 'Build internal tools with Python, FastAPI and JavaScript. Hybrid team in Berlin.', ['Python', 'FastAPI', 'JavaScript'], False, 58000, 'new'),
            ('Data Engineer · Python & SQL', 'Example Data Lab', 'job', 'Develop SQL transformations and Python data pipelines. PostgreSQL experience is useful.', ['Python', 'SQL', 'PostgreSQL'], True, 65000, 'new'),
            ('API Integration · 4-week Project', 'Demo Automation Collective', 'freelance', 'A fictional freelance project: integrate two APIs with Python and FastAPI. Document the workflow and provide tests.', ['Python', 'FastAPI', 'Git'], True, None, 'lead_saved'),
            ('Automation Developer', 'Example Operations', 'job', 'Automate recurring workflows in Python. Work with SQL and HTTP APIs. Docker and Git are useful.', ['Python', 'SQL', 'Docker'], True, 56000, 'new'),
            ('React Frontend Developer', 'Demo Interface Studio', 'job', 'Build React and TypeScript interfaces. Strong frontend design experience is required. Backend work is not the focus.', ['React', 'TypeScript', 'CSS'], True, 60000, 'new'),
            ('Technical Support · Internal Tools', 'Example Service Team', 'job', 'Support internal applications, investigate SQL queries and document solutions. Hybrid work in Berlin.', ['SQL', 'Support', 'Documentation'], False, 48000, 'new'),
        ]
        ids = []
        for index, (title, company, kind, description, tags, remote, salary, status) in enumerate(roles, 1):
            _, oid = repo.upsert_opportunity_full({
                'title': title, 'company': company, 'kind': kind, 'source': 'manual',
                'url': f'https://listings.example/demo-{index}', 'location': 'Berlin',
                'description': 'DEMO / FICTIONAL LISTING\n\n' + description,
                'tags': tags, 'remote': remote, 'salary_min': salary,
                'salary_text': f'{salary:,} EUR / year' if salary else 'Project budget: fictional example',
                'posted_at': (datetime.now(timezone.utc) - timedelta(days=index % 3)).strftime('%Y-%m-%d %H:%M'),
            })
            repo.update_opportunity(oid, status=status, notes='Fictional showcase listing. No real application or employer contact.')
            ids.append(oid)
        draft = (
            'Dear Example Systems team,\n\n'
            'I am interested in the Backend Engineer role. My reviewed demonstration profile '
            'includes Python, SQL, Docker and API development. At the fictional Example Software '
            'Studio, I worked on backend APIs and internal reporting tools.\n\n'
            'I would welcome a conversation about your API platform. I will review and personalize '
            'this text before using it.\n\nKind regards,\nAlex Beispiel\n\n'
            'DEMO: authored example, not a live AI generation; never submitted.'
        )
        app_id = repo.create_application(ids[1], 'job_application', '', 'en',
                                         'Application · Backend Engineer', draft,
                                         {'kind': 'showcase_fixture', 'authorship': 'editorial example'})
        repo.add_application_version(app_id, 'edited', draft)
        repo.set_application_fields(app_id, {'status': 'edited'})
        with repo.db() as connection:
            connection.execute('UPDATE opportunities SET active_application_id=? WHERE id=?', (app_id, ids[1]))
        repo.update_opportunity(ids[1], status='draft_prepared')
    marker.write_text(json.dumps({'fixture': 'alex-beispiel-v1', 'listings': len(ids),
                                  'external_calls': 0, 'submitted_applications': 0}, indent=2), encoding='utf-8')
