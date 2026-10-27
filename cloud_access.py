"""Platform operations remain separate from a customer's tenant role."""
import os

GLOBAL_PATHS = {'/settings','/settings/discovery','/api/ai/config','/api/email/config',
                '/api/email/test','/api/email/digest/send','/profile/email','/api/scheduler/tick'}


def is_admin(actor):
    allowed = {int(v) for v in os.environ.get('PLATFORM_ADMIN_USER_IDS','').split(',') if v.strip().isdigit()}
    return bool(actor and actor.user_id in allowed and actor.is_authenticated)


def protected(path, method):
    return path.startswith('/api/eval/') or path.startswith('/api/admin/usage') or path in ('/api/scheduler/tick','/api/email/config') or (path in GLOBAL_PATHS and method != 'GET')


def login_allowed(username, client_ip):
    """Atomic, shared rate limits survive serverless instance replacement."""
    import hashlib
    import hmac
    import time
    import repository as repo
    secret = os.environ['SECRET_KEY'].encode()
    window = int(time.time()) // 60
    keys = [('ip:'+client_ip,20),('login:'+username.casefold(),10)]
    with repo.db() as con:
        for value,limit in keys:
            key = hmac.new(secret,value.encode(),hashlib.sha256).hexdigest()
            row = con.execute('''INSERT INTO login_attempts(key,window,count) VALUES(?,?,1)
              ON CONFLICT(key) DO UPDATE SET count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,
              window=excluded.window RETURNING count''',(key,window)).fetchone()
            if row[0]>limit: return False
        con.execute('DELETE FROM login_attempts WHERE window < ?', (window-5,))
    return True
