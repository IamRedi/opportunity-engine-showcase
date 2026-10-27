"""Single fail-closed provider dispatch gate for OE-003C."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
import uuid

import httpx

import usage_ledger as ledger


class ProviderDispatchDisabled(RuntimeError):
    pass


PUBLIC_MESSAGES = {
    'budget-exhausted': 'Limiti i këtij muaji është përdorur. Provo përsëri pasi limiti të rinovohet.',
    'unresolved-usage': 'Një kërkesë e mëparshme po konfirmohet ende. Provo përsëri pas pak.',
}
DEFAULT_PUBLIC_MESSAGE = 'AI dhe kërkimi me burime të jashtme janë përkohësisht të padisponueshme. Provo përsëri më vonë.'
_USAGE_LINK = ContextVar('oe_background_usage_link', default=None)


@contextmanager
def link_usage_requests(callback):
    """Attach each metered request created in this execution context."""
    token = _USAGE_LINK.set(callback)
    try:
        yield
    finally:
        _USAGE_LINK.reset(token)


def _link_or_release(handle: str) -> None:
    callback = _USAGE_LINK.get()
    if callback is None:
        return
    try:
        callback(handle)
    except Exception:
        # Dispatch has not been claimed yet, so this reservation is safe to
        # release if the durable link itself cannot be recorded.
        ledger.finish_without_send(handle, 'released')
        raise


def message_key(exc: Exception) -> str:
    """Map internal accounting failures to stable, non-technical UI copy."""
    if isinstance(exc, ProviderDispatchDisabled):
        return DEFAULT_PUBLIC_MESSAGE
    return PUBLIC_MESSAGES.get(str(exc), DEFAULT_PUBLIC_MESSAGE)


def card_id(kind: str, provider: str, model: str='') -> str:
    identity=f'{provider}\0{model}'.encode()
    return f'{kind}-{hashlib.sha256(identity).hexdigest()[:16]}-v1'


def _accounting_enabled() -> bool:
    return ledger.enforcement_state()=='active'


def _require_gate() -> bool:
    return os.environ.get('APP_ENV','local')!='test'


def dispatch_available() -> bool:
    return not _require_gate() or _accounting_enabled()


def _run(operation,provider,meter,price_card,reserve_amount,fingerprint,call,settle_amount,
         idempotency_key=''):
    if not _accounting_enabled():
        if _require_gate():
            raise ProviderDispatchDisabled('Provider dispatch is disabled until usage accounting is active.')
        return call()
    request_key=idempotency_key or str(uuid.uuid4())
    held=ledger.reserve(operation,provider,meter,price_card,reserve_amount,fingerprint,
                        idempotency_key=request_key)
    if held['state']=='released':
        held=ledger.reserve_retry(held['handle'],reserve_amount,held['attempt_id'])
    elif held['state']!='reserved':
        raise ledger.UsageDenied('usage-attempt-not-dispatchable')
    _link_or_release(held['handle'])
    claim=ledger.claim(held['handle'],f'pid-{os.getpid()}')
    try:
        result=call()
    except httpx.ConnectError:
        ledger.fail_claim(claim,False,'connect-error')
        raise
    except Exception:
        ledger.fail_claim(claim,True,'transport-error')
        raise
    try:amount,meta=settle_amount(result)
    except (ValueError,TypeError,ledger.UsageError):amount,meta=None,{'count_method':'usage-unavailable'}
    ledger.settle(claim,amount,meta)
    return result


def ai_complete(provider, prompt: str):
    cfg=provider.cfg;provider_name=provider.id;model=str(cfg.get('model',''))
    if not _accounting_enabled():
        if _require_gate():raise ProviderDispatchDisabled('Provider dispatch is disabled until usage accounting is active.')
        return provider._complete_unmetered(prompt)
    price_card=card_id('ai',provider_name,model)
    input_units=len(prompt)+len(str(cfg.get('system_instructions','')))
    max_output=int(cfg.get('max_output_tokens',1200))
    reserve_amount=ledger.price_amount(price_card,input_units=input_units,output_units=max_output,reservation=True)

    def settle(result):
        _,usage=result
        if not isinstance(usage,dict):return None,{'count_method':'provider-usage-missing'}
        inp=usage.get('prompt_tokens',usage.get('input_tokens',usage.get('prompt_eval_count')))
        out=usage.get('completion_tokens',usage.get('output_tokens',usage.get('eval_count')))
        if inp is None or out is None:return None,{'count_method':'provider-usage-missing'}
        amount=ledger.price_amount(price_card,input_units=int(inp),output_units=int(out))
        return amount,{'input_units':int(inp),'output_units':int(out),'count_method':'provider'}

    request_key=getattr(provider,'_usage_request_key',None) or str(uuid.uuid4())
    provider._usage_request_key=request_key
    return _run(str(cfg.get('usage_operation','ai-complete')),provider_name,'ai',price_card,
                reserve_amount,{'provider':provider_name,'model':model,
                'prompt_hash':hashlib.sha256(prompt.encode()).hexdigest(),'max_output':max_output},
                lambda:provider._complete_unmetered(prompt),settle,request_key)


def http_response(operation: str, provider: str, call, *, fingerprint: dict,
                  max_redirects: int=0):
    if not _accounting_enabled():
        if _require_gate():raise ProviderDispatchDisabled('Provider dispatch is disabled until usage accounting is active.')
        return call()
    price_card=card_id('fetch',provider)
    reserve_amount=ledger.price_amount(price_card,hops=1+max(0,int(max_redirects)),reservation=True)

    def settle(response):
        hops=len(getattr(response,'history',()) or ())+1
        amount=ledger.price_amount(price_card,hops=hops)
        return amount,{'http_hops':hops,'count_method':'http-history',
                       'error_class':f'http-{response.status_code}' if response.status_code>=400 else ''}

    return _run(operation,provider,'search',price_card,reserve_amount,fingerprint,call,settle)


async def async_http_response(operation: str, provider: str, call, *, fingerprint: dict,
                              max_redirects: int=0):
    if not _accounting_enabled():
        if _require_gate():raise ProviderDispatchDisabled('Provider dispatch is disabled until usage accounting is active.')
        return await call()
    price_card=card_id('fetch',provider)
    reserve_amount=ledger.price_amount(price_card,hops=1+max(0,int(max_redirects)),reservation=True)
    held=ledger.reserve(operation,provider,'search',price_card,reserve_amount,fingerprint,
                        idempotency_key=str(uuid.uuid4()))
    _link_or_release(held['handle'])
    claim=ledger.claim(held['handle'],f'pid-{os.getpid()}')
    try:response=await call()
    except httpx.ConnectError:
        ledger.fail_claim(claim,False,'connect-error');raise
    except Exception:
        ledger.fail_claim(claim,True,'transport-error');raise
    hops=len(getattr(response,'history',()) or ())+1
    amount=ledger.price_amount(price_card,hops=hops)
    ledger.settle(claim,amount,{'http_hops':hops,'count_method':'http-history',
                  'error_class':f'http-{response.status_code}' if response.status_code>=400 else ''})
    return response
