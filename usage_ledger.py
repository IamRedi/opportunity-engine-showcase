"""Atomic, actor-scoped usage reservations and settlement (OE-003B).

Provider dispatch is intentionally outside this module and remains OE-003C.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from math import ceil

import config
import context
import repository as repo

ACTIVE_HOLDS = ('reserved','dispatching','sent_unknown','reconciling')
UNKNOWN_STATES = ('sent_unknown','reconciling')
FINAL_STATES = ('settled','released','cancelled')
SAFE_META = frozenset({'input_units','output_units','count_method','http_hops',
                       'final_host_class','result_count','error_class'})
SAFE_CARD = frozenset({'fixed_amount','input_rate','output_rate','rounding','encoder',
                       'max_output_units','max_redirects','billing_mode',
                       'input_unit_size','output_unit_size'})
SAFE_EVENT_TYPES = frozenset({'late-response','provider-audit','reconciliation'})
IDENTIFIER = re.compile(r'^[A-Za-z0-9_.:/-]{1,100}$')


class UsageError(RuntimeError): pass
class UsageDenied(UsageError): pass
class UsageConflict(UsageError): pass
class UsageNotFound(UsageError): pass
class UsageBusy(UsageError): pass


@dataclass(frozen=True)
class Claim:
    request_id: str
    attempt_id: int
    attempt_no: int
    fence: int
    token: str
    claim_until: int


def _ts(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec='seconds')


def month_window(now: datetime | None = None) -> tuple[str,str]:
    value=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start=value.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    end=start.replace(year=start.year+1,month=1) if start.month==12 else start.replace(month=start.month+1)
    return _ts(start),_ts(end)


def _actor(explicit=None):
    actor=context.require_context(explicit)
    if actor.is_system:
        raise UsageDenied('Usage accounting requires one authenticated user.')
    return actor


def _secret() -> bytes:
    value=os.environ.get('USAGE_HMAC_SECRET') or os.environ.get('SECRET_KEY') or config.SECRET_KEY
    if len(value)<32:
        raise UsageDenied('Usage HMAC secret is missing or too short.')
    return value.encode()


def _digest(label: str, value: str) -> str:
    return hmac.new(_secret(),f'{label}\0{value}'.encode(),hashlib.sha256).hexdigest()


def _canonical_fingerprint(values: dict) -> str:
    if not isinstance(values,dict):raise ValueError('Fingerprint input must be an object.')
    raw=json.dumps(values,sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return _digest('input-fingerprint-v1',raw)


def _safe_meta(values: dict | None) -> str:
    values=values or {}
    if not isinstance(values,dict) or set(values)-SAFE_META:
        raise ValueError('Usage metadata contains unsupported fields.')
    clean={}
    for key,value in values.items():
        if not isinstance(value,(str,int,float,bool,type(None))):raise ValueError('Usage metadata must be scalar.')
        text=str(value)
        if len(text)>120:raise ValueError('Usage metadata value is too long.')
        clean[key]=value
    return json.dumps(clean,sort_keys=True,separators=(',',':'))


def _identifier(value: str, label: str, *, empty: bool=False) -> str:
    value=str(value or '')
    if not value and empty:return ''
    if not IDENTIFIER.fullmatch(value):raise ValueError(f'Invalid {label}.')
    return value


def _safe_card(values: dict) -> str:
    if not isinstance(values,dict) or set(values)-SAFE_CARD:
        raise ValueError('Price snapshot contains unsupported fields.')
    numeric={'fixed_amount','input_rate','output_rate','max_output_units','max_redirects',
             'input_unit_size','output_unit_size'}
    for key,value in values.items():
        if not isinstance(value,(str,int,float)) or isinstance(value,bool):
            raise ValueError('Price snapshot values must be scalar.')
        if key in numeric and (not isinstance(value,int) or isinstance(value,bool)):
            raise ValueError('Price snapshot amounts, rates and caps must be integers.')
        if isinstance(value,(int,float)) and value<0:
            raise ValueError('Price snapshot amounts and caps cannot be negative.')
        if len(str(value))>100:raise ValueError('Price snapshot value is too long.')
    if 'billing_mode' in values and values['billing_mode'] not in ('per_token','per_fetch','per_hop'):
        raise ValueError('Unsupported price-card billing mode.')
    return json.dumps(values,sort_keys=True,separators=(',',':'))


def enforcement_state() -> str:
    """Return the fail-closed global dispatch gate for compatibility builds."""
    try:
        with repo.db() as con:
            rows=con.execute("SELECT key,value FROM settings WHERE key IN ('schema_version','usage_enforcement_state')").fetchall()
    except Exception:
        return 'disabled'
    values={row['key']:row['value'] for row in rows}
    try:
        schema_ready=int(values.get('schema_version','0')) >= 16
    except (TypeError,ValueError):
        schema_ready=False
    return 'active' if schema_ready and values.get('usage_enforcement_state')=='active' else 'disabled'


def price_amount(price_card_id: str, *, input_units: int=0, output_units: int=0,
                 hops: int=1, reservation: bool=False) -> int:
    """Calculate an integer amount from one immutable price-card snapshot."""
    price_card_id=_identifier(price_card_id,'price card ID')
    with repo.db() as con:
        card=con.execute("SELECT * FROM usage_price_cards WHERE price_card_id=? AND status='active'",
                         (price_card_id,)).fetchone()
    if not card:raise UsageDenied('unknown-price')
    try:snapshot=json.loads(card['card_snapshot'])
    except (TypeError,ValueError):raise UsageDenied('unknown-price') from None
    fixed=int(snapshot.get('fixed_amount',0))
    if card['billing_unit']=='fetch':
        return fixed
    if card['billing_unit']=='hop':
        return fixed*max(1,int(hops))
    in_size=int(snapshot.get('input_unit_size',1));out_size=int(snapshot.get('output_unit_size',1))
    if in_size<=0 or out_size<=0 or 'input_rate' not in snapshot or 'output_rate' not in snapshot:
        raise UsageDenied('unknown-price')
    output=int(output_units)
    if reservation:
        cap=int(snapshot.get('max_output_units',0))
        if output and cap and output>cap:raise UsageDenied('output-cap-exceeds-price-card')
        output=output or cap
    return fixed+ceil(max(0,int(input_units))/in_size)*int(snapshot['input_rate'])+ceil(max(0,output)/out_size)*int(snapshot['output_rate'])


def register_price_card(price_card_id: str, meter: str, provider: str, currency: str,
                        currency_scale: int, billing_unit: str, snapshot: dict,
                        effective_from: str, model_alias: str='', ctx=None) -> None:
    actor=_actor(ctx)
    if actor.role not in ('owner','platform_admin'):raise UsageDenied('Only an owner can register a price card.')
    price_card_id=_identifier(price_card_id,'price card ID')
    meter=_identifier(meter,'meter')
    provider=_identifier(provider,'provider')
    model_alias=_identifier(model_alias,'model alias',empty=True)
    if billing_unit not in ('tokens','fetch','hop'):raise ValueError('Unsupported billing unit.')
    if not currency.isalpha() or len(currency)!=3 or int(currency_scale)<=0:raise ValueError('Invalid currency or scale.')
    payload=_safe_card(snapshot)
    with repo.db() as con:
        old=con.execute('SELECT * FROM usage_price_cards WHERE price_card_id=?',(price_card_id,)).fetchone()
        values=(meter,provider,model_alias,currency.upper(),int(currency_scale),billing_unit,payload,effective_from)
        if old:
            if tuple(old[k] for k in ('meter','provider','model_alias','currency','currency_scale','billing_unit','card_snapshot','effective_from'))!=values:
                raise UsageConflict('Price card IDs are immutable.')
            return
        con.execute("""INSERT INTO usage_price_cards(price_card_id,meter,provider,model_alias,currency,currency_scale,billing_unit,card_snapshot,effective_from,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(price_card_id,*values,_ts()))


def set_period_limit(meter: str, currency: str, currency_scale: int, limit_amount: int,
                     limit_version: str, now: datetime | None=None, ctx=None) -> int:
    actor=_actor(ctx);start,end=month_window(now)
    meter=_identifier(meter,'meter')
    limit_version=_identifier(limit_version,'limit version')
    if not currency.isalpha() or len(currency)!=3:raise ValueError('Invalid currency.')
    if int(limit_amount)<0 or int(currency_scale)<=0:raise ValueError('Invalid usage limit.')
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE')
        existing=con.execute("""SELECT id,limit_amount FROM usage_budget_periods
            WHERE tenant_id=? AND user_id=? AND meter=? AND period_start=?""",
            (actor.tenant_id,actor.user_id,meter,start)).fetchone()
        if existing:
            same=(existing['limit_amount']==int(limit_amount))
            version=con.execute('SELECT limit_version,currency,currency_scale FROM usage_budget_periods WHERE id=?',(existing['id'],)).fetchone()
            if not same or version['limit_version']!=limit_version or version['currency']!=currency.upper() or version['currency_scale']!=int(currency_scale):
                raise UsageConflict('The active budget period is immutable; configure changes for the next period.')
            return existing['id']
        cur=con.execute("""INSERT INTO usage_budget_periods
            (tenant_id,user_id,meter,currency,currency_scale,period_start,period_end,limit_amount,limit_version,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",(actor.tenant_id,actor.user_id,meter,currency.upper(),int(currency_scale),start,end,int(limit_amount),limit_version,_ts()))
        return cur.lastrowid


def _exposure(con, actor, meter: str, period_start: str, period_end: str) -> tuple[int,bool]:
    row=con.execute("""SELECT
      COALESCE(SUM(CASE WHEN a.state='settled' AND f.finalized_at>=? AND f.finalized_at<?
                        THEN a.settled_charge ELSE 0 END),0) settled,
      COALESCE(SUM(CASE WHEN a.state IN ('reserved','dispatching','sent_unknown','reconciling')
                             AND p.period_start=? THEN a.reserved_amount ELSE 0 END),0) holds,
      COALESCE(SUM(CASE WHEN a.state IN ('reserved','dispatching','sent_unknown','reconciling')
                             AND p.period_start<? THEN a.reserved_amount ELSE 0 END),0) carried_holds,
      COALESCE(MAX(CASE WHEN (a.state IN ('sent_unknown','reconciling') AND p.period_start=?)
                             OR (a.state IN ('reserved','dispatching','sent_unknown','reconciling') AND p.period_start<?)
                        THEN 1 ELSE 0 END),0) has_unknown
      FROM usage_attempts a JOIN usage_requests r ON r.request_id=a.request_id
      JOIN usage_budget_periods p ON p.id=r.period_id
      LEFT JOIN usage_finalizations f ON f.attempt_id=a.id
      WHERE r.tenant_id=? AND r.user_id=? AND r.meter=?""",
      (period_start,period_end,period_start,period_start,period_start,period_start,
       actor.tenant_id,actor.user_id,meter)).fetchone()
    return int(row['settled'])+int(row['holds'])+int(row['carried_holds']),bool(row['has_unknown'])


def budget(meter: str, now: datetime | None=None, ctx=None) -> dict:
    actor=_actor(ctx);start,end=month_window(now)
    with repo.db() as con:
        period=con.execute("""SELECT * FROM usage_budget_periods WHERE tenant_id=? AND user_id=? AND meter=? AND period_start=?""",
            (actor.tenant_id,actor.user_id,meter,start)).fetchone()
        if not period:return {'configured':False,'meter':meter,'period_start':start}
        exposure,unknown=_exposure(con,actor,meter,start,end)
    return {'configured':True,'meter':meter,'period_start':start,'period_end':period['period_end'],
            'limit':period['limit_amount'],'committed':exposure,'available':period['limit_amount']-exposure,
            'currency':period['currency'],'currency_scale':period['currency_scale'],'blocked_unknown':unknown}


def reserve(operation: str, provider: str, meter: str, price_card_id: str,
            reserve_amount: int, fingerprint: dict, model_alias: str='',
            idempotency_key: str='', now: datetime | None=None, ctx=None) -> dict:
    actor=_actor(ctx);start,end=month_window(now);request_id=str(uuid.uuid4());created=_ts(now)
    operation=_identifier(operation,'operation')
    provider=_identifier(provider,'provider')
    meter=_identifier(meter,'meter')
    price_card_id=_identifier(price_card_id,'price card ID')
    model_alias=_identifier(model_alias,'model alias',empty=True)
    idem=_digest('idempotency-v1',idempotency_key) if idempotency_key else ''
    fp=_canonical_fingerprint(fingerprint)
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE')
        if idem:
            old=con.execute("""SELECT * FROM usage_requests WHERE tenant_id=? AND user_id=? AND operation=? AND idempotency_key_hash=?""",
                (actor.tenant_id,actor.user_id,operation,idem)).fetchone()
            if old:
                if old['input_fingerprint']!=fp:raise UsageConflict('Idempotency key was reused with different input.')
                attempt=con.execute('SELECT * FROM usage_attempts WHERE request_id=? ORDER BY attempt_no DESC LIMIT 1',(old['request_id'],)).fetchone()
                return _view(old,attempt,actor)
        card=con.execute("""SELECT * FROM usage_price_cards WHERE price_card_id=? AND meter=? AND provider=? AND status='active'
            AND effective_from<=? AND (effective_until IS NULL OR effective_until>?)""",
            (price_card_id,meter,provider,created,created)).fetchone()
        if not card:raise UsageDenied('unknown-price')
        period=con.execute("""SELECT * FROM usage_budget_periods WHERE tenant_id=? AND user_id=? AND meter=? AND period_start=?""",
            (actor.tenant_id,actor.user_id,meter,start)).fetchone()
        if not period:raise UsageDenied('budget-not-configured')
        if period['currency']!=card['currency'] or period['currency_scale']!=card['currency_scale']:
            raise UsageDenied('budget-currency-mismatch')
        exposure,unknown=_exposure(con,actor,meter,start,end)
        amount=int(reserve_amount)
        if amount<0:raise ValueError('Reservation must be non-negative.')
        if unknown:raise UsageDenied('unresolved-usage')
        if period['limit_amount']-exposure<amount:raise UsageDenied('budget-exhausted')
        con.execute("""INSERT INTO usage_requests(request_id,tenant_id,user_id,operation,provider,model_alias,meter,idempotency_key_hash,input_fingerprint,fingerprint_key_version,price_card_id,period_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(request_id,actor.tenant_id,actor.user_id,operation,provider,model_alias,meter,idem,fp,'hmac-v1',price_card_id,period['id'],created))
        cur=con.execute("""INSERT INTO usage_attempts(request_id,attempt_no,state,reserved_amount,currency,currency_scale,price_snapshot,created_at,updated_at)
            VALUES(?,1,'reserved',?,?,?,?,?,?)""",(request_id,amount,card['currency'],card['currency_scale'],card['card_snapshot'],created,created))
        attempt=con.execute('SELECT * FROM usage_attempts WHERE id=?',(cur.lastrowid,)).fetchone()
        request=con.execute('SELECT * FROM usage_requests WHERE request_id=?',(request_id,)).fetchone()
        return _view(request,attempt,actor)


def _handle(request_id: str, actor) -> str:
    sig=_digest('request-handle-v1',f'{actor.tenant_id}:{actor.user_id}:{request_id}')
    return request_id+'.'+sig


def _request_for_handle(con,handle: str,actor):
    try:request_id,sig=handle.rsplit('.',1)
    except ValueError:raise UsageNotFound('Usage request not found.') from None
    expected=_handle(request_id,actor).rsplit('.',1)[1]
    if not hmac.compare_digest(sig,expected):raise UsageNotFound('Usage request not found.')
    row=con.execute('SELECT * FROM usage_requests WHERE request_id=? AND tenant_id=? AND user_id=?',
                    (request_id,actor.tenant_id,actor.user_id)).fetchone()
    if not row:raise UsageNotFound('Usage request not found.')
    return row


def _view(request,attempt,actor):
    return {'handle':_handle(request['request_id'],actor),'request_id':request['request_id'],
            'attempt_id':attempt['id'],'attempt_no':attempt['attempt_no'],'state':attempt['state'],
            'reserved_amount':attempt['reserved_amount'],'settled_charge':attempt['settled_charge'],
            'currency':attempt['currency'],'currency_scale':attempt['currency_scale']}


def get(handle: str, ctx=None) -> dict:
    actor=_actor(ctx)
    with repo.db() as con:
        request=_request_for_handle(con,handle,actor)
        attempt=con.execute('SELECT * FROM usage_attempts WHERE request_id=? ORDER BY attempt_no DESC LIMIT 1',(request['request_id'],)).fetchone()
    return _view(request,attempt,actor)


def claim(handle: str, owner: str, lease_seconds: int=60, now_epoch: int | None=None, ctx=None) -> Claim:
    actor=_actor(ctx);epoch=int(time.time() if now_epoch is None else now_epoch)
    owner=_identifier(owner,'claim owner')
    if not 1<=int(lease_seconds)<=900:raise ValueError('Invalid claim.')
    token=secrets.token_urlsafe(24);token_hash=_digest('claim-v1',token)
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE');request=_request_for_handle(con,handle,actor)
        attempt=con.execute('SELECT * FROM usage_attempts WHERE request_id=? ORDER BY attempt_no DESC LIMIT 1',(request['request_id'],)).fetchone()
        cur=con.execute("""UPDATE usage_attempts SET state='dispatching',claim_owner=?,claim_token_hash=?,claim_until=?,fence=fence+1,updated_at=?
            WHERE id=? AND state='reserved' AND claim_until<?""",(owner,token_hash,epoch+lease_seconds,_ts(),attempt['id'],epoch))
        if cur.rowcount!=1:raise UsageBusy('Usage attempt is already claimed or terminal.')
        fresh=con.execute('SELECT * FROM usage_attempts WHERE id=?',(attempt['id'],)).fetchone()
    return Claim(request['request_id'],fresh['id'],fresh['attempt_no'],fresh['fence'],token,fresh['claim_until'])


def reserve_retry(handle: str, reserve_amount: int, retry_of_attempt_id: int,
                  now: datetime | None=None, ctx=None) -> dict:
    actor=_actor(ctx);start,end=month_window(now);created=_ts(now)
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE');request=_request_for_handle(con,handle,actor)
        previous=con.execute('SELECT * FROM usage_attempts WHERE id=? AND request_id=?',
                             (int(retry_of_attempt_id),request['request_id'])).fetchone()
        if not previous:raise UsageNotFound('Previous usage attempt not found.')
        if previous['state'] not in ('settled','released'):
            raise UsageConflict('Retry requires a finalized previous attempt.')
        amount=int(reserve_amount)
        if amount<0:raise ValueError('Reservation must be non-negative.')
        existing=con.execute('SELECT * FROM usage_attempts WHERE retry_of_attempt_id=?',(previous['id'],)).fetchone()
        if existing:
            if existing['reserved_amount']!=amount:
                raise UsageConflict('Retry was repeated with a different reservation.')
            return _view(request,existing,actor)
        period=con.execute('SELECT * FROM usage_budget_periods WHERE id=?',(request['period_id'],)).fetchone()
        if period['period_start']!=start:raise UsageDenied('Retry requires a request in the current period.')
        exposure,unknown=_exposure(con,actor,request['meter'],start,end)
        if unknown:raise UsageDenied('unresolved-usage')
        if period['limit_amount']-exposure<amount:raise UsageDenied('budget-exhausted')
        attempt_no=con.execute('SELECT COALESCE(MAX(attempt_no),0)+1 n FROM usage_attempts WHERE request_id=?',(request['request_id'],)).fetchone()['n']
        cur=con.execute("""INSERT INTO usage_attempts(request_id,attempt_no,retry_of_attempt_id,state,reserved_amount,currency,currency_scale,price_snapshot,created_at,updated_at)
            VALUES(?,?,?,'reserved',?,?,?,?,?,?)""",(request['request_id'],attempt_no,previous['id'],amount,previous['currency'],previous['currency_scale'],previous['price_snapshot'],created,created))
        attempt=con.execute('SELECT * FROM usage_attempts WHERE id=?',(cur.lastrowid,)).fetchone()
        return _view(request,attempt,actor)


def claim_reconciliation(handle: str, owner: str, lease_seconds: int=60,
                         now_epoch: int | None=None, ctx=None) -> Claim:
    actor=_actor(ctx);epoch=int(time.time() if now_epoch is None else now_epoch)
    owner=_identifier(owner,'claim owner')
    if not 1<=int(lease_seconds)<=900:raise ValueError('Invalid claim.')
    token=secrets.token_urlsafe(24);token_hash=_digest('claim-v1',token)
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE');request=_request_for_handle(con,handle,actor)
        attempt=con.execute('SELECT * FROM usage_attempts WHERE request_id=? ORDER BY attempt_no DESC LIMIT 1',(request['request_id'],)).fetchone()
        cur=con.execute("""UPDATE usage_attempts SET state='reconciling',claim_owner=?,claim_token_hash=?,claim_until=?,fence=fence+1,updated_at=?
            WHERE id=? AND state='sent_unknown' AND claim_until<?""",(owner,token_hash,epoch+lease_seconds,_ts(),attempt['id'],epoch))
        if cur.rowcount!=1:raise UsageBusy('Usage attempt is not ready for reconciliation.')
        fresh=con.execute('SELECT * FROM usage_attempts WHERE id=?',(attempt['id'],)).fetchone()
    return Claim(request['request_id'],fresh['id'],fresh['attempt_no'],fresh['fence'],token,fresh['claim_until'])


def expire_reconciliation(claim: Claim, now_epoch: int | None=None, ctx=None) -> dict:
    actor=_actor(ctx);epoch=int(time.time() if now_epoch is None else now_epoch)
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE');row=_claimed(con,claim,actor,('reconciling',))
        if row['claim_until']>=epoch:raise UsageBusy('Reconciliation claim has not expired.')
        con.execute("""UPDATE usage_attempts SET state='sent_unknown',claim_owner='',claim_token_hash='',
                    claim_until=0,fence=fence+1,updated_at=? WHERE id=?""",(_ts(),row['id']))
        request=con.execute('SELECT * FROM usage_requests WHERE request_id=?',(claim.request_id,)).fetchone()
        attempt=con.execute('SELECT * FROM usage_attempts WHERE id=?',(row['id'],)).fetchone()
        return _view(request,attempt,actor)


def _claimed(con,claim: Claim, actor, states):
    row=con.execute("""SELECT a.*,r.tenant_id,r.user_id FROM usage_attempts a JOIN usage_requests r ON r.request_id=a.request_id
        WHERE a.id=? AND a.request_id=? AND r.tenant_id=? AND r.user_id=?""",
        (claim.attempt_id,claim.request_id,actor.tenant_id,actor.user_id)).fetchone()
    if not row:raise UsageNotFound('Usage attempt not found.')
    if row['state'] not in states or row['fence']!=claim.fence or not hmac.compare_digest(row['claim_token_hash'],_digest('claim-v1',claim.token)):
        raise UsageConflict('Stale or invalid usage claim.')
    return row


def settle(claim: Claim, charge: int | None, usage_meta: dict | None=None,
           provider_evidence_id: str='', evidence: str='', now: datetime | None=None, ctx=None) -> dict:
    actor=_actor(ctx);meta=_safe_meta(usage_meta);stamp=_ts(now)
    provider_evidence_hash=_digest('provider-evidence-v1',provider_evidence_id) if provider_evidence_id else ''
    evidence_hash=_digest('evidence-v1',evidence) if evidence else ''
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE')
        final=con.execute('SELECT * FROM usage_finalizations WHERE attempt_id=?',(claim.attempt_id,)).fetchone()
        if final:
            attempt=_claimed(con,claim,actor,(final['final_state'],))
            request=con.execute('SELECT * FROM usage_requests WHERE request_id=?',(claim.request_id,)).fetchone()
            return _view(request,attempt,actor)
        row=_claimed(con,claim,actor,('dispatching','sent_unknown','reconciling'))
        if charge is None:
            con.execute("""UPDATE usage_attempts SET state='sent_unknown',usage_meta=?,error_class='usage-unknown',updated_at=? WHERE id=?""",
                        (meta,stamp,row['id']))
        else:
            amount=int(charge)
            if amount<0:raise ValueError('Charge must be non-negative.')
            con.execute("""UPDATE usage_attempts SET state='settled',settled_charge=?,provider_evidence_hash=?,usage_meta=?,updated_at=? WHERE id=?""",
                        (amount,provider_evidence_hash,meta,stamp,row['id']))
            con.execute("""INSERT INTO usage_finalizations(attempt_id,request_id,final_state,settled_charge,usage_meta,evidence_hash,finalized_at)
                VALUES(?,?,'settled',?,?,?,?)""",(row['id'],claim.request_id,amount,meta,evidence_hash,stamp))
        request=con.execute('SELECT * FROM usage_requests WHERE request_id=?',(claim.request_id,)).fetchone()
        attempt=con.execute('SELECT * FROM usage_attempts WHERE id=?',(claim.attempt_id,)).fetchone()
        return _view(request,attempt,actor)


def finish_without_send(handle: str, state: str='released', ctx=None) -> dict:
    actor=_actor(ctx)
    if state not in ('released','cancelled'):raise ValueError('Invalid no-send final state.')
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE');request=_request_for_handle(con,handle,actor)
        attempt=con.execute('SELECT * FROM usage_attempts WHERE request_id=? ORDER BY attempt_no DESC LIMIT 1',(request['request_id'],)).fetchone()
        final=con.execute('SELECT 1 FROM usage_finalizations WHERE attempt_id=?',(attempt['id'],)).fetchone()
        if final:return _view(request,attempt,actor)
        cur=con.execute("UPDATE usage_attempts SET state=?,updated_at=? WHERE id=? AND state='reserved'",(state,_ts(),attempt['id']))
        if cur.rowcount!=1:raise UsageConflict('Only an unclaimed reservation can be released.')
        con.execute("""INSERT INTO usage_finalizations(attempt_id,request_id,final_state,settled_charge,finalized_at)
            VALUES(?,?,?,?,?)""",(attempt['id'],request['request_id'],state,0,_ts()))
        attempt=con.execute('SELECT * FROM usage_attempts WHERE id=?',(attempt['id'],)).fetchone()
        return _view(request,attempt,actor)


def expire_claim(claim: Claim, sent_may_have_started: bool, now_epoch: int | None=None, ctx=None) -> dict:
    actor=_actor(ctx);epoch=int(time.time() if now_epoch is None else now_epoch)
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE');row=_claimed(con,claim,actor,('dispatching',))
        if row['claim_until']>=epoch:raise UsageBusy('Claim has not expired.')
        state='sent_unknown' if sent_may_have_started else 'released'
        con.execute('UPDATE usage_attempts SET state=?,updated_at=? WHERE id=?',(state,_ts(),row['id']))
        if state=='released':con.execute("""INSERT INTO usage_finalizations(attempt_id,request_id,final_state,settled_charge,finalized_at)
            VALUES(?,?,'released',0,?)""",(row['id'],claim.request_id,_ts()))
        request=con.execute('SELECT * FROM usage_requests WHERE request_id=?',(claim.request_id,)).fetchone()
        attempt=con.execute('SELECT * FROM usage_attempts WHERE id=?',(row['id'],)).fetchone()
        return _view(request,attempt,actor)


def fail_claim(claim: Claim, sent_may_have_started: bool, error_class: str='', ctx=None) -> dict:
    """Finalize a live claim after a synchronous transport failure."""
    actor=_actor(ctx)
    error_class=_identifier(error_class,'error class',empty=True)
    with repo.db() as con:
        con.execute('BEGIN IMMEDIATE');row=_claimed(con,claim,actor,('dispatching',))
        state='sent_unknown' if sent_may_have_started else 'released'
        con.execute('UPDATE usage_attempts SET state=?,error_class=?,updated_at=? WHERE id=?',
                    (state,error_class,_ts(),row['id']))
        if state=='released':
            con.execute("""INSERT INTO usage_finalizations(attempt_id,request_id,final_state,settled_charge,finalized_at)
                VALUES(?,?,'released',0,?)""",(row['id'],claim.request_id,_ts()))
        request=con.execute('SELECT * FROM usage_requests WHERE request_id=?',(claim.request_id,)).fetchone()
        attempt=con.execute('SELECT * FROM usage_attempts WHERE id=?',(row['id'],)).fetchone()
        return _view(request,attempt,actor)


def add_evidence(claim: Claim, event_id: str, event_type: str, evidence: str, ctx=None) -> bool:
    actor=_actor(ctx)
    if event_type not in SAFE_EVENT_TYPES:raise ValueError('Unsupported evidence event type.')
    event_id_hash=_digest('evidence-event-v1',event_id)
    with repo.db() as con:
        _claimed(con,claim,actor,('dispatching','sent_unknown','reconciling','settled'))
        try:
            con.execute("INSERT INTO usage_evidence_events(attempt_id,event_id_hash,event_type,evidence_hash,created_at) VALUES(?,?,?,?,?)",
                        (claim.attempt_id,event_id_hash,event_type,_digest('evidence-v1',evidence),_ts()))
            return True
        except sqlite3.IntegrityError:return False
