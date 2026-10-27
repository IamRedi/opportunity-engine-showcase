"""Run a fresh, offline, fictional showcase without loading personal configuration."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--container', action='store_true', help='Bind inside a loopback-published container')
    parser.add_argument('--check', action='store_true', help='Seed and HTTP-smoke-check without starting a server')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Choose a port from 1024 to 65535')
    data = Path('/data') if args.container else ROOT / '.demo'
    data = data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    if data.is_symlink() or (not args.container and data != ROOT / '.demo'):
        raise SystemExit('The demo workspace must be the dedicated .demo directory')
    if not args.child:
        from demo_safety import demo_environment
        env = demo_environment(ROOT, data)
        env['BASE_URL'] = f'http://127.0.0.1:{args.port}'
        command = [sys.executable, str(Path(__file__).resolve()), '--child', '--port', str(args.port)]
        if args.container:
            command.append('--container')
        if args.check:
            command.append('--check')
        return subprocess.call(command, cwd=ROOT, env=env)
    if os.environ.get('OPPORTUNITY_DEMO') != '1' or Path(os.environ['OPPORTUNITY_DB']) != data / 'jobs.db':
        raise SystemExit('Use the public launcher, not --child directly')
    from demo_safety import install_network_guard
    install_network_guard()
    from demo.seed import seed
    seed(data)
    import app
    if args.check:
        from fastapi.testclient import TestClient
        with TestClient(app.app) as client:
            for path in ('/health', '/?lang=en', '/?lang=de', '/profile?lang=en', '/profile/resume?lang=en'):
                response = client.get(path)
                if response.status_code != 200:
                    raise RuntimeError(f'Smoke check failed: {path}: {response.status_code}')
            response = client.post('/api/search/arbeitnow')
            if response.status_code != 403:
                raise RuntimeError('Demo did not reject a live search')
        print(json.dumps({'http_smoke': 'passed', 'fixture': 'fictional', 'external_providers': 'disabled'}))
        return 0
    import uvicorn
    print(f'Opportunity Engine showcase: http://127.0.0.1:{args.port}/?lang=en', flush=True)
    uvicorn.run(app.app, host='0.0.0.0' if args.container else '127.0.0.1', port=args.port,
                log_level='warning', access_log=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
