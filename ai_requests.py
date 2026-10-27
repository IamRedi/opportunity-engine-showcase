"""Shared per-account AI request leases; never hold DB transactions during AI I/O."""
from functools import wraps
import hashlib
import json
import secrets
import time

import repository as repo

BUSY = 'AI po përpunon këtë kërkesë. Prit përfundimin; mos e përsërit.'
LEASE_SECONDS = 900  # Longer than the Vercel function deadline and provider timeouts.


def serialized(operation):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            uid=repo._uid(repo._actor(None))
            key=f'ai-lease:{uid}:{operation}'
            owner=secrets.token_hex(16)
            now=time.time()
            with repo.db() as con:
                con.execute('BEGIN IMMEDIATE')
                row=con.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
                previous=json.loads(row[0]) if row else {}
                if previous.get('until',0)>now:
                    return {'ok':False,'busy':True,'reason':BUSY}
                value=json.dumps({'owner':owner,'until':now+LEASE_SECONDS})
                con.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
            try:
                return fn(*args,**kwargs)
            finally:
                with repo.db() as con:
                    con.execute('DELETE FROM settings WHERE key=? AND value=?',(key,value))
        return run
    return decorate


def cache_key(operation, version, inputs, cfg):
    uid=repo._uid(repo._actor(None))
    payload=[uid,operation,version,inputs,{k:cfg.get(k) for k in ('provider','model','base_url')}]
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
