"""Cached reads for fixed public job APIs; credentials and private feeds bypass it."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from urllib.parse import urlparse

import httpx
import config
import usage_runtime

CACHE_DIR = config.DATA_DIR / '.cache' / 'public-sources'
_LOCK = threading.Lock()
_TTLS = {'www.arbeitnow.com': 900, 'remotive.com': 21600,
         'jobicy.com': 3600, 'hn.algolia.com': 900, 'remoteok.com':3600,
         'boards-api.greenhouse.io':3600,'api.lever.co':3600,'api.eu.lever.co':3600,'api.ashbyhq.com':3600}
MAX_CACHE_BYTES = 8 * 1024 * 1024


def get_json(url, params=None, *, headers=None, timeout=25, allow_list=False,
             usage_operation='search-fetch', usage_provider=''):
    parsed = urlparse(url)
    ttl = _TTLS.get(parsed.hostname, 0) if parsed.scheme == 'https' else 0
    # Only our fixed public hosts without embedded credentials or secret parameters.
    if parsed.username or parsed.password or parsed.query or any(
        marker in str(key).lower() for key in (params or {})
        for marker in ('key', 'token', 'secret', 'password')
    ):
        ttl = 0

    def fetch():
        response = usage_runtime.http_response(usage_operation,usage_provider or parsed.hostname,
            lambda:httpx.get(url,params=params,headers=headers,timeout=timeout,follow_redirects=True),
            fingerprint={'host':parsed.hostname,'path_hash':hashlib.sha256(parsed.path.encode()).hexdigest(),
                         'params_hash':hashlib.sha256(json.dumps(params or {},sort_keys=True).encode()).hexdigest()},
            max_redirects=20)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (dict,list) if allow_list else dict):
            raise ValueError('Expected an API response object')
        return payload

    if not ttl:
        return fetch()
    key = hashlib.sha256(json.dumps([url, params or {}], sort_keys=True).encode()).hexdigest()
    path = CACHE_DIR / (key + '.json')
    # One process serializes cache misses so jobs/contract views share one request.
    with _LOCK:
        try:
            if path.stat().st_size <= MAX_CACHE_BYTES:
                stored = json.loads(path.read_text(encoding='utf-8'))
                age = time.time() - stored['fetched_at']
                if 0 <= age < ttl and isinstance(stored['payload'], (dict,list) if allow_list else dict):
                    return stored['payload']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        payload = fetch()  # No stale success when the upstream request fails.
        temp_path = None
        try:
            encoded = json.dumps({'fetched_at': time.time(), 'payload': payload}, ensure_ascii=False)
            if len(encoded.encode()) <= MAX_CACHE_BYTES:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=CACHE_DIR,
                                                 delete=False, suffix='.tmp') as handle:
                    temp_path = Path(handle.name)
                    handle.write(encoded)
                os.replace(temp_path, path)
        except OSError:
            pass  # An unwritable cache must not break a successful source read.
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return payload
