"""Explicit same-computer pilot switching; never an internet authentication mode."""
import hashlib
import hmac
import os
import secrets
import time

import config
import repository as repo
import tenancy

COOKIE = 'oe_pilot'


def enabled():
    return os.environ.get('APP_ENV', 'local') == 'local' and os.environ.get('LOCAL_PILOT_USERS') == '1'


def local_request(request):
    return request.url.hostname in ('127.0.0.1', 'localhost', '::1', 'testserver') and request.client and request.client.host in ('127.0.0.1', '::1', 'testclient')


def secret():
    path=config.DATA_DIR / '.pilot-session.key'
    try:return path.read_text().strip()
    except FileNotFoundError:
        value=secrets.token_hex(32)
        try:
            with path.open('x') as f:
                os.chmod(path,0o600);f.write(value)
        except FileExistsError:return path.read_text().strip()
        return value


def users():
    with repo.db() as c:
        rows=c.execute("SELECT id,display_name,username FROM users WHERE status='active' AND (id=1 OR username='pilot-friend') ORDER BY id").fetchall()
    return [{'id':r['id'],'label':r['display_name'] if r['id']==1 and r['display_name'] not in ('','Owner') else ('Profili im' if r['id']==1 else 'Piloti 2')} for r in rows]


def token(uid):
    payload=f'{uid}:{int(time.time())+86400}'
    return payload+':'+hmac.new(secret().encode(),payload.encode(),hashlib.sha256).hexdigest()


def actor(value):
    if not value:return tenancy.resolve_actor(1)
    try:
        uid,exp,sig=value.split(':');payload=uid+':'+exp
        if int(exp)<time.time() or not hmac.compare_digest(sig,hmac.new(secret().encode(),payload.encode(),hashlib.sha256).hexdigest()):raise ValueError()
        if int(uid) not in [u['id'] for u in users()]:raise ValueError()
        return tenancy.resolve_actor(int(uid))
    except (ValueError,tenancy.TenantMembershipError):
        raise ValueError('Piloti ka ndryshuar. Rifresko faqen.') from None


def provision():
    if not enabled():raise ValueError('Local pilot mode required')
    secret()
    tid=tenancy.ensure_tenant('Pilot 2','pilot-friend')
    with repo.db() as c:
        row=c.execute("SELECT id FROM users WHERE username='pilot-friend'").fetchone()
        if row:return row['id']
        import auth_service
        cur=c.execute("INSERT INTO users(email,username,password_hash,display_name,status,created_at,updated_at) VALUES (?,?,?,'','active',?,?)",
            ('pilot-friend@local','pilot-friend',auth_service._hash_pw(secrets.token_urlsafe(32)),repo.now(),repo.now()))
        uid=cur.lastrowid
        c.execute('INSERT INTO tenant_memberships(tenant_id,user_id,role,created_at) VALUES (?,?,?,?)',(tid,uid,'owner',repo.now()))
    return uid
