"""One-step durable background worker (OE-004B).

Handlers run entirely outside database transactions.  The job store owns every
claim, retry, cancellation, revision check and terminal publication.
"""
from __future__ import annotations

import asyncio
import inspect
import socket
import uuid

import background_jobs
import context

STEP_TIMEOUT_SECONDS = 90
_handlers: dict[str, object] = {}


class RetryableStepError(RuntimeError):
    def __init__(self, error_code: str = "provider_unavailable"):
        self.error_code = error_code
        super().__init__(error_code)


class TerminalStepError(RuntimeError):
    def __init__(self, error_code: str = "invalid_output"):
        self.error_code = error_code
        super().__init__(error_code)


def register(key: str, handler) -> None:
    background_jobs._identifier(key, "handler key")
    if not callable(handler):
        raise TypeError("Background handler must be callable.")
    _handlers[key] = handler


def clear_handlers() -> None:
    _handlers.clear()


async def _invoke(handler, detail: dict, step: dict, claim: background_jobs.Claim):
    if inspect.iscoroutinefunction(handler):
        return await handler(detail, step, claim)
    return await asyncio.to_thread(handler, detail, step, claim)


def _owner() -> str:
    host = "".join(ch for ch in socket.gethostname() if ch.isalnum() or ch in "_.-")[:40] or "worker"
    return f"{host}:{uuid.uuid4().hex[:16]}"


def _usage_outcome(claim: background_jobs.Claim, actor) -> str:
    """Return the strongest linked usage state; release every untouched hold."""
    handles = background_jobs.linked_usage_requests(claim, ctx=actor)
    if not handles:
        return "none"
    import usage_ledger
    states = []
    for handle in handles:
        state = usage_ledger.get(handle, ctx=actor)["state"]
        if state == "reserved":
            state = usage_ledger.finish_without_send(
                handle, "released", ctx=actor
            )["state"]
        states.append(state)
    if any(state in {"dispatching", "sent_unknown", "reconciling", "settled"}
           for state in states):
        return "provider_outcome_unknown"
    return "released"


def _record_handler_failure(claim, actor, requested_code: str, *, retryable: bool):
    outcome = _usage_outcome(claim, actor)
    if outcome == "provider_outcome_unknown":
        return background_jobs.fail(claim, "provider_outcome_unknown", ctx=actor), False
    return background_jobs.fail(claim, requested_code, retryable=retryable, ctx=actor), retryable


async def process(job_id: str, *, handlers: dict | None = None,
                  owner: str | None = None, timeout_seconds: int = STEP_TIMEOUT_SECONDS,
                  publish_next: bool = True) -> dict | None:
    """Claim and execute at most one step; duplicate deliveries become no-ops."""
    actor = background_jobs.actor_for_job(job_id)
    with context.run_as(actor):
        claim = background_jobs.claim(job_id, owner or _owner(), ctx=actor)
        if claim is None:
            return background_jobs.get(job_id, ctx=actor)
        detail = background_jobs.worker_detail(job_id, ctx=actor)
        step = next(item for item in detail["steps"] if item["id"] == claim.step_id)
        if handlers is None:
            # Import lazily to avoid a module cycle at startup and to make a
            # fresh queue consumer self-contained even after test registries
            # or process-local overrides have been cleared.
            import background_domain
            registry = {**background_domain.HANDLERS, **_handlers}
        else:
            registry = handlers
        handler = registry.get(f"{detail['job_type']}:{step['step_type']}") or registry.get(detail["job_type"])
        if handler is None:
            return background_jobs.fail(claim, "handler_missing", ctx=actor)
        try:
            import usage_runtime
            with usage_runtime.link_usage_requests(
                lambda handle: background_jobs.attach_usage_request(
                    claim, handle, ctx=actor
                )
            ):
                result_ref = await asyncio.wait_for(
                    _invoke(handler, detail, step, claim),
                    timeout=max(1, min(int(timeout_seconds), 90)),
                )
        except asyncio.TimeoutError:
            result, should_retry = _record_handler_failure(claim, actor, "timeout", retryable=True)
            if should_retry:
                raise RetryableStepError("timeout") from None
            return result
        except RetryableStepError as exc:
            result, should_retry = _record_handler_failure(
                claim, actor, exc.error_code, retryable=True
            )
            if should_retry:
                raise
            return result
        except TerminalStepError as exc:
            result, _ = _record_handler_failure(
                claim, actor, exc.error_code, retryable=False
            )
            return result
        except Exception as exc:
            import usage_ledger
            import usage_runtime
            if isinstance(exc, usage_ledger.UsageDenied):
                code = "budget_exhausted" if str(exc) in {"budget-exhausted", "unresolved-usage"} else "provider_unavailable"
                return background_jobs.fail(claim, code, ctx=actor)
            if isinstance(exc, usage_runtime.ProviderDispatchDisabled):
                return background_jobs.fail(claim, "provider_unavailable", ctx=actor)
            result, should_retry = _record_handler_failure(
                claim, actor, "worker_error", retryable=True
            )
            if should_retry:
                raise RetryableStepError("worker_error") from None
            return result
        try:
            current_revision = background_jobs.profile_revision_token(actor)
            result = background_jobs.complete(
                claim, str(result_ref), current_revision=current_revision, ctx=actor
            )
        except (TypeError, ValueError):
            return background_jobs.fail(claim, "invalid_output", ctx=actor)
        if publish_next and result["status"] == "dispatch_pending":
            import background_queue
            if background_queue.enabled():
                await background_queue.publish_pending(job_id=job_id)
        return result


async def consume(message: dict, *, handlers: dict | None = None) -> dict | None:
    """Queue entry point. Payload keys are exact and contain no actor or PII."""
    if not isinstance(message, dict) or set(message) != {"job_id", "contract_version"}:
        raise ValueError("Invalid background queue payload.")
    if message["contract_version"] != background_jobs.CONTRACT_VERSION:
        raise ValueError("Unsupported background-job contract version.")
    try:
        uuid.UUID(str(message["job_id"]))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Invalid background job ID.") from None
    return await process(str(message["job_id"]), handlers=handlers)
