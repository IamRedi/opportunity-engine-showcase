"""Private Vercel Queues subscriber for durable Opportunity Engine work.

Vercel imports this module at build time from ``pyproject.toml``.  The queue
payload contains only the opaque job ID and contract version; all ownership,
input and state are loaded from the canonical Turso job store by the worker.
"""
from __future__ import annotations

from vercel.queue import Message, subscribe

import background_queue
import background_worker


@subscribe(
    topic=background_queue.TOPIC,
    consumer_group="oe-worker-v1",
    retry_after=5,
    max_concurrency=2,
    max_attempts=8,
)
async def consume_work(message: Message[dict[str, object]]) -> None:
    """Process one durable step; raising leaves retry control to the queue."""
    await background_worker.consume(message.payload)
