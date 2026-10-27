"""Provision the prepared two-account pilot after an explicit empty-schema migration.

Run with remote database environment variables loaded in a separate process.
Never opens the local jobs database or exports local profile data.
"""
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def provision(accounts):
    import auth_service
    import cloud_database
    import repository as repo
    import tenancy
    if not cloud_database.enabled() or os.environ.get('APP_ENV') != 'production':
        raise RuntimeError('Explicit production remote database configuration required')
    repo.init()  # Verify version only; never migrate a live database here.
    if len(accounts)!=2 or [a['tenant_id'] for a in accounts]!=[1,2]:
        raise RuntimeError('Exactly two isolated pilot accounts are required')
    allowed={a['email'] for a in accounts}
    with repo.db() as con:
        if any(r['email'] not in allowed for r in con.execute('SELECT email FROM users')):
            raise RuntimeError('Unexpected accounts exist; refusing pilot provisioning')
    for expected_id,account in enumerate(accounts,1):
        if len(account['password'])<24:
            raise RuntimeError('A strong generated pilot password is required')
        found=auth_service.find_user_by_login(account['email'])
        if found:
            uid=auth_service.authenticate(account['email'],account['password'])
            if not uid:raise RuntimeError('Existing pilot credentials do not match; no reset performed')
        else:
            uid=auth_service.create_user(account['email'],account['password'],account['display_name'],
                tenant_id=account['tenant_id'],role='owner' if expected_id==1 else 'member')
        actor=tenancy.resolve_actor(uid)
        if uid!=expected_id or actor.tenant_id!=account['tenant_id']:
            raise RuntimeError('Pilot account mapping differs from the prepared admin configuration')
    return [1,2]


if __name__=='__main__':
    access_path=ROOT/'.private/pilot-access.json'
    data=json.loads(access_path.read_text())
    provision(data['users'])
    data['status']='provisioned_not_live_verified'
    access_path.write_text(json.dumps(data,indent=2)+'\n');access_path.chmod(0o600)
    print('Two database accounts provisioned and authenticated; no local profiles copied.')
