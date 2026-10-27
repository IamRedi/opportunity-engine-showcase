"""Safety regressions for the opt-in, isolated public evaluation workspace."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import demo_safety


def test_showcase_environment_drops_private_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv('TURSO_AUTH_TOKEN', 'private-fixture-never-forward')
    monkeypatch.setenv('AI_API_KEY', 'private-fixture-never-forward')
    monkeypatch.setenv('AUTH_PASSWORD', 'private-fixture-never-forward')
    monkeypatch.setenv('OPPORTUNITY_DB', '/private/original.db')
    env = demo_safety.demo_environment(tmp_path, tmp_path / 'demo')
    assert not any(key in env for key in ('TURSO_AUTH_TOKEN', 'AI_API_KEY', 'AUTH_PASSWORD'))
    assert env['OPPORTUNITY_DB'] == str(tmp_path / 'demo' / 'jobs.db')
    assert env['DATABASE_PROVIDER'] == 'sqlite'
    assert env['STORAGE_PROVIDER'] == 'local'
    assert env['AI_PROVIDER'] == 'disabled'
    assert env['LOCAL_PILOT_USERS'] == '0'


@pytest.mark.parametrize('host,allowed', [
    ('127.0.0.1', True), ('localhost', True), ('::1', True), ('::ffff:127.0.0.1', True),
    ('192.168.1.1', False), ('169.254.169.254', False), ('8.8.8.8', False),
    ('api.example.com', False), ('localhost.example.com', False), ('0.0.0.0', False),
])
def test_showcase_loopback_boundary(host, allowed):
    assert demo_safety.is_loopback(host) is allowed


def test_showcase_rejects_external_dns_and_connections_without_network_calls():
    root = Path(__file__).resolve().parents[1]
    script = '''
import json, socket
from demo_safety import install_network_guard
install_network_guard()
denied = 0
for action in (lambda: socket.getaddrinfo('api.example.com', 443),
               lambda: socket.socket().connect(('8.8.8.8', 443)),
               lambda: socket.socket(socket.AF_INET6).connect(('2001:db8::1', 443))):
    try: action()
    except PermissionError: denied += 1
print(json.dumps({'denied': denied, 'loopback_dns': bool(socket.getaddrinfo('127.0.0.1', 8765))}))
'''
    completed = subprocess.run([sys.executable, '-c', script], cwd=root, text=True,
                               capture_output=True, timeout=20, check=True)
    assert json.loads(completed.stdout) == {'denied': 3, 'loopback_dns': True}


def test_showcase_blocks_live_provider_writes_but_keeps_local_review():
    for path in ('/api/search/arbeitnow', '/api/profiles/1/run', '/api/discovery/run',
                 '/api/work', '/api/ai/config', '/api/admin/usage/reconcile'):
        assert demo_safety.blocked_demo_write(path)
    for path in ('/profile', '/api/jobs/1/update', '/api/applications/1/edit'):
        assert not demo_safety.blocked_demo_write(path)


@pytest.mark.parametrize('locale,expected', [
    ('en', '8 relevant terms (2 in title)'),
    ('de', '8 passende Begriffe (2 im Titel)'),
])
def test_matching_term_explanation_follows_the_interface_language(locale, expected):
    import i18n
    token = i18n._locale.set(locale)
    try:
        assert i18n.ui_text('8 terma përkatës (2 në titull)') == expected
    finally:
        i18n._locale.reset(token)
