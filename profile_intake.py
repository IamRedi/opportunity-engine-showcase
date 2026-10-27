"""One reviewed profile intake for self-descriptions and user-edited applications."""
import hashlib
import json
import uuid

import cv_ai
from cv_identity import identity
import profile_context
import repository as repo

PREVIEW_SECTION = 'intake_preview'
SINGLETONS = {'personal', 'preferences'}
CORE_FIELDS = {'name','title','email','phone','location','summary'}


def revision(con, uid):
    profile=con.execute('SELECT * FROM profile WHERE user_id=?',(uid,)).fetchone()
    sections=[dict(r) for r in con.execute('SELECT section,data FROM profile_sections WHERE user_id=? ORDER BY section',(uid,))
              if r['section'] not in (PREVIEW_SECTION,'intelligence','avatar','resume_exports')]
    cv=con.execute('SELECT id FROM profile_cv WHERE user_id=?',(uid,)).fetchone()
    return hashlib.sha256(json.dumps([dict(profile) if profile else {},sections,dict(cv) if cv else None],sort_keys=True).encode()).hexdigest()


def defaults():
    p=repo.get_profile()
    user=repo.get_user(repo._uid(repo._actor(None))) or {}
    email=user.get('email','')
    name=user.get('display_name','')
    if email.endswith('@local'): email=''
    if name in ('Owner',''): name=''
    return {k:p.get(k) or ({'name':name,'email':email}.get(k,'')) for k in CORE_FIELDS}


def preview(text, application_id=None):
    if not isinstance(text,str) or not 20 <= len(text.strip()) <= 12000:
        return {'ok':False,'reason':'Përshkrimi duhet të ketë 20–12000 karaktere.'}
    uid=repo._uid(repo._actor(None))
    repo.get_profile()  # Initialize the canonical row before taking the review revision.
    with repo.db() as con: token=revision(con,uid)
    result=cv_ai.extract(text,description=True)
    if not result['ok']: return result
    ctx=profile_context.build_context(); core={**ctx['core'],'summary':ctx['summary']}
    items=[]
    for suggestion in result['suggestions']:
        section=suggestion['section']
        if section in ('contact','summary'):
            fields={('summary' if section=='summary' else suggestion['field']):suggestion['proposed_value']}
            current={k:core.get(k,'') for k in fields}; section='contact'
        else:
            fields=json.loads(suggestion['proposed_value'])
            entries=ctx.get(section,[])
            primary='name' if section in ('skills','languages','interests','certifications') else 'title'
            current=(entries[0] if entries else {}) if section in SINGLETONS else next((e for e in entries if identity(section,e.get(primary,''))==identity(section,fields.get(primary,''))),{})
        if fields and all(str(current.get(k,'')).strip()==str(v).strip() for k,v in fields.items()): continue
        conflict=any(current.get(k) and str(current[k]).strip()!=str(v).strip() for k,v in fields.items())
        items.append({'id':str(len(items)),'section':section,'fields':fields,'current':current,
                      'selected':not conflict,'evidence':suggestion['evidence']})
    account=defaults()
    for key in ('name','email'):
        if not core.get(key) and account.get(key) and not any(key in x['fields'] for x in items if x['section']=='contact'):
            items.append({'id':str(len(items)),'section':'contact','fields':{key:account[key]},'current':{},'selected':True,'evidence':'account'})
    record={'id':uuid.uuid4().hex,'revision':token,'items':items,'created_at':repo.now(),'application_id':application_id}
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE')
        if revision(con,uid)!=token: return {'ok':False,'reason':'Profili ndryshoi. Gjenero parashikimin përsëri.'}
        con.execute('INSERT INTO profile_sections(user_id,profile_id,section,data,updated_at) VALUES (?,1,?,?,?) ON CONFLICT(user_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                    (uid,PREVIEW_SECTION,json.dumps([record],ensure_ascii=False),repo.now()))
    return {'ok':True,'preview':record,'defaults':account,'warning':result.get('warning','')}


def application_preview(app_id):
    app=repo.get_application(app_id,with_versions=True)
    if not app: return {'ok':False,'reason':'Aplikimi nuk u gjet.'}
    if app.get('latest_kind')!='edited':
        return {'ok':False,'reason':'Ruaj ndryshimet e tua në draft përpara kontrollit të profilit.'}
    return preview(app['latest_content'],application_id=app_id)


def commit(preview_id, items, allow_updates=False):
    if not isinstance(items,list) or len(items)>180: return {'ok':False,'reason':'Parashikimi është i pavlefshëm.'}
    uid=repo._uid(repo._actor(None)); accepted=0
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute('SELECT data FROM profile_sections WHERE user_id=? AND section=?',(uid,PREVIEW_SECTION)).fetchone()
        record=json.loads(row['data'])[0] if row else {}
        if not record or record['id']!=preview_id: return {'ok':False,'reason':'Parashikimi nuk u gjet. Gjeneroje përsëri.'}
        if revision(con,uid)!=record['revision']: return {'ok':False,'reason':'Profili ndryshoi. Gjenero parashikimin përsëri.'}
        known={item['id']:item for item in record['items']}
        sections={r['section']:json.loads(r['data']) for r in con.execute('SELECT section,data FROM profile_sections WHERE user_id=?',(uid,))}
        changed=set(); core={};conflicts=[]
        original_core=dict(con.execute('SELECT * FROM profile WHERE user_id=?',(uid,)).fetchone() or {})
        def check_changes(section,before,fields):
            for key,value in fields.items():
                if before.get(key) and str(before[key]).strip()!=value:
                    conflicts.append({'section':section,'field':key,'before':str(before[key]),'after':value})
        for item in items:
            if not isinstance(item,dict): raise ValueError('Invalid preview item')
            section=item.get('section'); fields=item.get('fields')
            allowed=CORE_FIELDS if section=='contact' else cv_ai.ENTRY_FIELDS.get(section)
            if not allowed or not isinstance(fields,dict) or not fields or any(k not in allowed or not isinstance(v,str) or len(v)>4000 for k,v in fields.items()):
                raise ValueError('Invalid profile fields')
            if item.get('id') is not None and (item['id'] not in known or known[item['id']]['section']!=section):
                raise ValueError('Invalid preview reference')
            fields={k:v.strip() for k,v in fields.items() if v.strip()}
            if not fields: continue
            if section=='contact':
                check_changes(section,original_core,fields);core.update(fields);accepted+=len(fields);continue
            ok,_=profile_context.validate_section(section,[fields])
            if not ok:raise ValueError('Missing profile field')
            entries=sections.setdefault(section,[])
            primary='name' if section in ('skills','languages','interests','certifications') else 'title'
            existing=(entries[0] if entries else None) if section in SINGLETONS else next((e for e in entries if identity(section,e.get(primary,''))==identity(section,fields.get(primary,'')) and
                (section not in ('experience','education') or not e.get('org') or not fields.get('org') or e['org']==fields['org']) and
                (section not in ('experience','education') or not e.get('period') or not fields.get('period') or e['period']==fields['period'])),None)
            # Explicitly reviewed values may correct existing values; provenance is review, not AI certainty.
            if existing is None: entries.append({**fields,'source':'review:'+preview_id})
            else:
                check_changes(section,existing,fields)
                existing.update(fields);existing['source']='review:'+preview_id
            changed.add(section);accepted+=1
        if conflicts and allow_updates is not True:
            return {'ok':False,'needs_confirmation':True,'conflicts':conflicts,
                    'reason':'Këto ndryshime zëvendësojnë të dhëna ekzistuese. Konfirmoji përpara ruajtjes.'}
        con.execute('INSERT OR IGNORE INTO profile(user_id) VALUES (?)',(uid,))
        for key,value in core.items():con.execute(f'UPDATE profile SET {key}=? WHERE user_id=?',(value,uid))
        for section in changed:
            con.execute('INSERT INTO profile_sections(user_id,profile_id,section,data,updated_at) VALUES (?,1,?,?,?) ON CONFLICT(user_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                        (uid,section,json.dumps(sections[section],ensure_ascii=False),repo.now()))
        for section in changed & {'skills','languages'}:
            values=[' – '.join([e['name'],e.get('level','')]).strip(' –') if section=='languages' else e['name'] for e in sections[section]]
            con.execute(f'UPDATE profile SET {section}=? WHERE user_id=?',(json.dumps(values,ensure_ascii=False),uid))
        if 'experience' in changed:
            text='\n'.join(' | '.join(str(e.get(k) or '') for k in ('title','org','period','description')).strip(' |') for e in sections['experience'])
            con.execute('UPDATE profile SET experience=? WHERE user_id=?',(text,uid))
        con.execute('DELETE FROM profile_sections WHERE user_id=? AND section=?',(uid,PREVIEW_SECTION))
    repo.rescore_all()
    return {'ok':True,'accepted':accepted}
