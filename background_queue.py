"""Replaceable Vercel Queues wake-up adapter for OE-004B.

The adapter is disabled by default.  Tests may inject a sender without touching
Vercel; production send is possible only on schema 17 with both rollout gates.
"""
from __future__ import annotations

from datetime import timedelta
import inspect
import os
import time

import background_job_schema
import background_jobs
import repository as repo

TOPIC = "oe-work-v1"


class QueueDispatchDisabled(RuntimeError): pass


def enabled() -> bool:
    if os.environ.get("BACKGROUND_JOBS_ENABLED", "false").lower() != "true":
        return False
    if os.environ.get("BACKGROUND_DISPATCH", "disabled") != "vercel_queue":
        return False
    try:
        with repo.db() as con:
            row = con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        return bool(row and str(row[0]) == str(background_job_schema.SCHEMA_VERSION))
    except Exception:
        return False


def payload(job_id: str, contract_version: int) -> dict:
    # worker store validates the ID again before any lookup
    if int(contract_version) != background_jobs.CONTRACT_VERSION:
        raise ValueError("Unsupported background-job contract version.")
    background_jobs._identifier(job_id, "job ID")
    return {"job_id": job_id, "contract_version": int(contract_version)}


async def _default_sender(topic: str, body: dict, *, idempotency_key: str):
    from vercel.queue import send
    return await send(topic, body, idempotency_key=idempotency_key,
                      retention=timedelta(days=7))


async def publish_pending(*, job_id: str | None = None, limit: int = 20,
                          sender=None, now_epoch: int | None = None) -> dict:
    """Publish pending outbox rows with a stable transport idempotency key."""
    if sender is None:
        if not enabled():
            raise QueueDispatchDisabled("Background queue dispatch is disabled.")
        sender = _default_sender
    sent = failed = 0
    epoch = int(time.time() if now_epoch is None else now_epoch)
    entries = background_jobs.pending_outbox(limit, job_id=job_id, now_epoch=epoch)
    for entry in entries:
        body = payload(entry["job_id"], entry["contract_version"])
        key = f"{entry['job_id']}:{entry['step_sequence']}:{entry['contract_version']}"
        try:
            value = sender(TOPIC, body, idempotency_key=key)
            message_id = await value if inspect.isawaitable(value) else value
            message_text = str(message_id or "")
            if message_text and not background_jobs.IDENTIFIER.fullmatch(message_text):
                message_text = ""
            if background_jobs.mark_outbox_sent(entry["id"], message_text):
                sent += 1
        except Exception:
            # Exception text is deliberately not persisted: it may contain URLs,
            # headers or provider diagnostics.
            background_jobs.mark_outbox_failed(entry["id"], now_epoch=epoch)
            failed += 1
    return {"pending": len(entries), "sent": sent, "failed": failed}
