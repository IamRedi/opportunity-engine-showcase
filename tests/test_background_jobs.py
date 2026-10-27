"""OE-004B durable job/outbox/worker tests; zero real provider calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import threading

import libsql
import pytest

import auth_service
import background_job_schema
import background_jobs as jobs
import background_queue
import background_subscriber
import background_worker
import cloud_database
import config
import context
import repository as repo
import usage_ledger
import usage_runtime
from migrations.v17_background_jobs import migrate_connection

ONE = context.ActorContext(1, 1, "owner")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobs.sqlite3")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SECRET_KEY", "fixture-background-secret-at-least-32-characters")
    monkeypatch.delenv("BACKGROUND_JOBS_ENABLED", raising=False)
    monkeypatch.delenv("BACKGROUND_DISPATCH", raising=False)
    repo.init()
    background_worker.clear_handlers()


def discovery(sources=None, ctx=ONE):
    return jobs.create("profile_discovery", {
        "sources": sources or ["fixture"], "language": "en", "kind": "all"
    }, ctx=ctx)


def queue_first(job):
    row = jobs.pending_outbox(job_id=job["job_id"])[0]
    assert jobs.mark_outbox_sent(row["id"], "fixture-message")


def test_fresh_schema_and_explicit_migration_are_idempotent_on_sqlite_and_libsql(tmp_path):
    with repo.db() as con:
        assert con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "17"
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"background_work_requests", "background_jobs", "background_job_steps",
            "background_job_step_usage", "background_job_outbox",
            "background_job_events"} <= names

    for factory, name in (
        (lambda path: sqlite3.connect(path), "sqlite"),
        (lambda path: cloud_database.Connection(libsql.connect(str(path))), "libsql"),
    ):
        con = factory(tmp_path / f"migration-{name}.db")
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        con.execute("INSERT INTO settings VALUES('schema_version','16')")
        con.commit()
        migrate_connection(con)
        migrate_connection(con)
        assert con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "17"
        assert con.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0
        con.close()


@pytest.mark.parametrize("kind", ["sqlite", "libsql"])
def test_migration_failure_rolls_back(tmp_path, monkeypatch, kind):
    path = tmp_path / f"failed-{kind}.db"
    con = (sqlite3.connect(path) if kind == "sqlite"
           else cloud_database.Connection(libsql.connect(str(path))))
    con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    con.execute("INSERT INTO settings VALUES('schema_version','16')")
    con.commit()
    monkeypatch.setattr(background_job_schema, "DDL", [background_job_schema.DDL[0], "INVALID SQL"])
    with pytest.raises(sqlite3.OperationalError):
        migrate_connection(con)
    assert con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "16"
    assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='background_jobs'").fetchone()
    con.close()


def test_create_is_idempotent_allowlisted_and_actor_isolated():
    first = discovery(["adzuna", "google_jobs"])
    same = discovery(["adzuna", "google_jobs"])
    assert same["job_id"] == first["job_id"]
    assert first["status"] == "dispatch_pending" and first["progress_total"] == 2
    with pytest.raises(ValueError):
        jobs.create("profile_discovery", {"sources": ["fixture"], "cv_text": "private"}, ctx=ONE)

    uid = auth_service.create_user("other@example.org", "fixture-password-long-enough")
    other = context.ActorContext(uid, 1, "member")
    with pytest.raises(jobs.JobNotFound):
        jobs.get(first["job_id"], ctx=other)
    with pytest.raises(jobs.JobNotFound):
        jobs.request_cancel(first["job_id"], ctx=other)
    with pytest.raises(jobs.JobNotFound):
        jobs.create("profile_discovery", {"sources": ["adzuna", "google_jobs"]},
                    retry_of_job_id=first["job_id"], ctx=other)
    assert discovery(ctx=other)["job_id"] != first["job_id"]


def test_two_workers_claim_one_step_and_publish_one_effect():
    job = discovery()
    queue_first(job)
    barrier = threading.Barrier(2)

    def take(owner):
        barrier.wait()
        return jobs.claim(job["job_id"], owner, ctx=ONE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(take, ("worker-a", "worker-b")))
    winners = [claim for claim in claims if claim]
    assert len(winners) == 1
    result = jobs.complete(winners[0], "opportunity-batch:1",
                           current_revision=jobs.profile_revision_token(ONE), ctx=ONE)
    assert result["status"] == "succeeded"
    with repo.db() as con:
        assert con.execute("SELECT COUNT(*) FROM background_job_events WHERE event_type='completed'").fetchone()[0] == 1


def test_duplicate_queue_deliveries_execute_handler_once():
    job = discovery()
    queue_first(job)
    lock = threading.Lock()
    calls = 0

    def handler(detail, step, claim):
        nonlocal calls
        with lock:
            calls += 1
        return "opportunity-batch:one"

    async def run():
        return await asyncio.gather(
            background_worker.process(job["job_id"], handlers={"profile_discovery": handler}),
            background_worker.process(job["job_id"], handlers={"profile_discovery": handler}),
        )

    results = asyncio.run(run())
    assert calls == 1
    assert jobs.get(job["job_id"], ctx=ONE)["status"] == "succeeded"
    assert {result["status"] for result in results} <= {"running", "succeeded"}


def test_expired_lease_recovers_and_old_fence_cannot_publish():
    job = discovery()
    queue_first(job)
    old = jobs.claim(job["job_id"], "old-worker", lease_seconds=1, now_epoch=10, ctx=ONE)
    assert old and jobs.claim(job["job_id"], "early-worker", now_epoch=10, ctx=ONE) is None
    new = jobs.claim(job["job_id"], "new-worker", now_epoch=12, ctx=ONE)
    assert new and new.fence == old.fence + 1
    with pytest.raises(jobs.JobConflict):
        jobs.complete(old, "result:old", current_revision=jobs.profile_revision_token(ONE), ctx=ONE)
    assert jobs.complete(new, "result:new", current_revision=jobs.profile_revision_token(ONE), ctx=ONE)["status"] == "succeeded"


def test_cancel_during_step_discards_publication_and_stops_later_steps():
    job = discovery(["one", "two"])
    queue_first(job)

    def handler(detail, step, claim):
        jobs.request_cancel(job["job_id"], ctx=ONE)
        return "opportunity-batch:late"

    result = asyncio.run(background_worker.process(
        job["job_id"], handlers={"profile_discovery": handler}
    ))
    assert result["status"] == "cancelled" and result["progress_current"] == 0
    with repo.db() as con:
        statuses = [row[0] for row in con.execute(
            "SELECT status FROM background_job_steps WHERE job_id=? ORDER BY sequence", (job["job_id"],)
        )]
        pending = con.execute(
            "SELECT COUNT(*) FROM background_job_outbox WHERE job_id=? AND state='pending'", (job["job_id"],)
        ).fetchone()[0]
    assert statuses == ["cancelled", "cancelled"] and pending == 0


def test_cancel_during_step_wins_race_with_retryable_failure():
    job = discovery(["one", "two"])
    queue_first(job)
    claim = jobs.claim(job["job_id"], "failing-worker", ctx=ONE)
    assert claim
    assert jobs.request_cancel(job["job_id"], ctx=ONE)["status"] == "cancel_requested"

    result = jobs.fail(
        claim, "provider_unavailable", retryable=True, max_attempts=8, ctx=ONE
    )

    assert result["status"] == "cancelled"
    assert jobs.claim(job["job_id"], "late-worker", ctx=ONE) is None
    assert jobs.pending_outbox(job_id=job["job_id"]) == []
    with repo.db() as con:
        statuses = [row[0] for row in con.execute(
            "SELECT status FROM background_job_steps WHERE job_id=? ORDER BY sequence",
            (job["job_id"],),
        )]
    assert statuses == ["cancelled", "cancelled"]
    assert [event["event_type"] for event in jobs.events(job["job_id"], ctx=ONE)] == [
        "queued", "started", "cancel_requested", "cancelled"
    ]


def test_publish_confirmation_racing_with_cancel_does_not_requeue_job():
    job = discovery(["cancel-race"])
    outbox = jobs.pending_outbox(job_id=job["job_id"])[0]
    assert jobs.request_cancel(job["job_id"], ctx=ONE)["status"] == "cancelled"
    assert jobs.mark_outbox_sent(outbox["id"], "late-confirmation")
    assert jobs.get(job["job_id"], ctx=ONE)["status"] == "cancelled"
    assert [event["event_type"] for event in jobs.events(job["job_id"], ctx=ONE)] == ["cancelled"]


def test_profile_edit_while_handler_runs_makes_result_superseded():
    job = discovery()
    queue_first(job)

    def handler(detail, step, claim):
        repo.set_profile_section("skills", [{"name": "New skill"}], ctx=ONE)
        return "opportunity-batch:stale"

    result = asyncio.run(background_worker.process(
        job["job_id"], handlers={"profile_discovery": handler}
    ))
    assert result["status"] == "superseded"
    assert jobs.events(job["job_id"], ctx=ONE)[-1]["event_type"] == "superseded"


def test_publish_timeout_stays_pending_and_reuses_exact_idempotency_key():
    job = discovery()
    keys = []

    async def sender(topic, body, *, idempotency_key):
        keys.append(idempotency_key)
        if len(keys) == 1:
            raise TimeoutError("fixture timeout")
        assert topic == "oe-work-v1"
        assert set(body) == {"job_id", "contract_version"}
        return "message-two"

    first = asyncio.run(background_queue.publish_pending(
        job_id=job["job_id"], sender=sender, now_epoch=100
    ))
    assert first == {"pending": 1, "sent": 0, "failed": 1}
    assert jobs.get(job["job_id"], ctx=ONE)["status"] == "dispatch_pending"
    second = asyncio.run(background_queue.publish_pending(
        job_id=job["job_id"], sender=sender, now_epoch=103
    ))
    assert second == {"pending": 1, "sent": 1, "failed": 0}
    assert keys[0] == keys[1]
    assert jobs.get(job["job_id"], ctx=ONE)["status"] == "queued"
    with pytest.raises(background_queue.QueueDispatchDisabled):
        asyncio.run(background_queue.publish_pending(job_id=job["job_id"]))


def test_queue_gate_requires_both_flags_and_exact_schema_17(monkeypatch):
    assert not background_queue.enabled()
    monkeypatch.setenv("BACKGROUND_JOBS_ENABLED", "true")
    assert not background_queue.enabled()
    monkeypatch.setenv("BACKGROUND_DISPATCH", "vercel_queue")
    assert background_queue.enabled()
    with repo.db() as con:
        con.execute("UPDATE settings SET value='16' WHERE key='schema_version'")
        con.execute("INSERT INTO settings(key,value) VALUES('usage_enforcement_state','active') "
                    "ON CONFLICT(key) DO UPDATE SET value='active'")
    assert not background_queue.enabled()
    assert usage_ledger.enforcement_state() == "active"


def test_vercel_subscriber_contract_and_delivery(monkeypatch):
    """The deployed subscriber keeps queue configuration and payload exact."""
    from vercel.queue import Message, MessageMetadata, get_subscriptions

    subscription = next(
        item for item in get_subscriptions()
        if item.func is background_subscriber.consume_work
    )
    assert subscription.topic == background_queue.TOPIC
    assert subscription.consumer_group == "oe-worker-v1"
    assert subscription.max_concurrency == 2
    assert subscription.max_attempts == 8

    delivered = []

    async def consume(payload):
        delivered.append(payload)

    monkeypatch.setattr(background_worker, "consume", consume)
    metadata = MessageMetadata(
        message_id="fixture-message", delivery_count=1,
        created_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        topic=background_queue.TOPIC, consumer_group="oe-worker-v1",
    )
    message = Message(
        payload={"job_id": "00000000-0000-4000-8000-000000000001",
                 "contract_version": 1},
        metadata=metadata,
    )
    asyncio.run(background_subscriber.consume_work(message))
    assert delivered == [message.payload]


def test_retry_limit_becomes_poison_and_user_retry_creates_new_job():
    job = discovery()
    queue_first(job)
    first = jobs.claim(job["job_id"], "worker-one", ctx=ONE)
    assert jobs.fail(first, "provider_unavailable", retryable=True, max_attempts=2, ctx=ONE)["status"] == "queued"
    second = jobs.claim(job["job_id"], "worker-two", ctx=ONE)
    failed = jobs.fail(second, "provider_unavailable", retryable=True, max_attempts=2, ctx=ONE)
    assert failed["status"] == "failed" and failed["error_code"] == "poison_message"
    retried = jobs.retry(job["job_id"], ctx=ONE)
    assert retried["job_id"] != job["job_id"] and retried["retry_of_job_id"] == job["job_id"]


def test_handler_runs_without_database_transaction_and_flow_works_via_local_libsql(tmp_path, monkeypatch):
    job = discovery()
    queue_first(job)

    def handler(detail, step, claim):
        with repo.db() as con:
            assert not con.in_transaction
        return "fixture-result:outside-transaction"

    assert asyncio.run(background_worker.process(
        job["job_id"], handlers={"profile_discovery": handler}
    ))["status"] == "succeeded"

    libsql_path = tmp_path / "runtime-libsql.db"
    repo.init(libsql_path)
    real_db = repo.db

    def local_libsql(path=None):
        if path is not None:
            return real_db(path)
        return cloud_database.Connection(libsql.connect(str(libsql_path)))

    monkeypatch.setattr(repo, "db", local_libsql)
    other = discovery(["libsql"])
    queue_first(other)
    claim = jobs.claim(other["job_id"], "libsql-worker", ctx=ONE)
    assert jobs.complete(claim, "libsql-result:one",
                         current_revision=jobs.profile_revision_token(ONE), ctx=ONE)["status"] == "succeeded"


def test_worker_uses_linked_usage_state_to_retry_before_send_and_stop_unknown_outcome():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    usage_ledger.register_price_card(
        "job-fixture-v1", "search", "fixture", "EUR", 100, "fetch",
        {"fixed_amount": 1}, "2026-01-01T00:00:00+00:00", ctx=ONE,
    )
    usage_ledger.set_period_limit("search", "EUR", 100, 10, "pilot-v1", now=now, ctx=ONE)

    before = discovery(["before-send"])
    queue_first(before)
    before_handle = ""

    def before_send(detail, step, claim):
        nonlocal before_handle
        held = usage_ledger.reserve(
            "fixture", "fixture", "search", "job-fixture-v1", 1,
            {"job_id": detail["job_id"]}, idempotency_key=detail["job_id"],
            now=now, ctx=ONE,
        )
        before_handle = held["handle"]
        jobs.attach_usage_request(claim, before_handle, ctx=ONE)
        raise background_worker.RetryableStepError("provider_unavailable")

    with pytest.raises(background_worker.RetryableStepError):
        asyncio.run(background_worker.process(
            before["job_id"], handlers={"profile_discovery": before_send}
        ))
    assert usage_ledger.get(before_handle, ctx=ONE)["state"] == "released"
    assert jobs.get(before["job_id"], ctx=ONE)["status"] == "queued"

    unknown = discovery(["unknown-after-send"])
    queue_first(unknown)
    unknown_handle = ""

    def after_send(detail, step, claim):
        nonlocal unknown_handle
        held = usage_ledger.reserve(
            "fixture", "fixture", "search", "job-fixture-v1", 1,
            {"job_id": detail["job_id"]}, idempotency_key=detail["job_id"],
            now=now, ctx=ONE,
        )
        unknown_handle = held["handle"]
        jobs.attach_usage_request(claim, unknown_handle, ctx=ONE)
        usage_claim = usage_ledger.claim(unknown_handle, "fixture-provider", ctx=ONE)
        usage_ledger.settle(usage_claim, None, {"count_method": "usage-unavailable"}, ctx=ONE)
        raise background_worker.RetryableStepError("provider_unavailable")

    result = asyncio.run(background_worker.process(
        unknown["job_id"], handlers={"profile_discovery": after_send}
    ))
    assert result["status"] == "failed" and result["error_code"] == "provider_outcome_unknown"
    assert usage_ledger.get(unknown_handle, ctx=ONE)["state"] == "sent_unknown"


def test_worker_automatically_links_every_metered_call_in_one_step():
    # Periudha e buxhetit duhet të mbulojë muajin REAL të ores — reserve()
    # përdor orën e vërtetë; data e fiksuar e thyej çdo muaj të ri (OE-007D triage).
    now = datetime.now(timezone.utc)
    price_card = usage_runtime.card_id("ai", "fixture-ai", "fixture-model")
    usage_ledger.register_price_card(
        price_card, "ai", "fixture-ai", "EUR", 100, "tokens",
        {"fixed_amount": 0, "input_rate": 1, "output_rate": 1,
         "input_unit_size": 10, "output_unit_size": 1, "max_output_units": 10,
         "encoder": "fixture-v1", "billing_mode": "per_token"},
        "2025-01-01T00:00:00+00:00", model_alias="fixture-model", ctx=ONE,
    )
    usage_ledger.set_period_limit("ai", "EUR", 100, 100, "fixture-v1", now=now, ctx=ONE)
    with repo.db() as con:
        con.execute("UPDATE settings SET value='active' WHERE key='usage_enforcement_state'")

    class Provider:
        id = "fixture-ai"
        cfg = {"model": "fixture-model", "max_output_tokens": 10,
               "system_instructions": "fixture", "usage_operation": "ai-fixture"}

        def _complete_unmetered(self, prompt):
            return "{}", {"prompt_tokens": 2, "completion_tokens": 1}

    job = discovery(["metered"])
    queue_first(job)

    def handler(detail, step, claim):
        usage_runtime.ai_complete(Provider(), "first")
        usage_runtime.ai_complete(Provider(), "second")
        raise background_worker.TerminalStepError("invalid_output")

    result = asyncio.run(background_worker.process(
        job["job_id"], handlers={"profile_discovery": handler}
    ))
    assert result["status"] == "failed"
    assert result["error_code"] == "provider_outcome_unknown"
    with repo.db() as con:
        links = con.execute(
            "SELECT usage_request_ref FROM background_job_step_usage WHERE job_id=?",
            (job["job_id"],),
        ).fetchall()
    assert len(links) == 2


def test_cancel_after_metered_settlement_keeps_usage_finalized():
    # E njëjta arsye si më sipër: periudha = muaji real (dritare e reserve()).
    now = datetime.now(timezone.utc)
    price_card = usage_runtime.card_id("ai", "cancel-ai", "cancel-model")
    usage_ledger.register_price_card(
        price_card, "ai", "cancel-ai", "EUR", 100, "tokens",
        {"fixed_amount": 0, "input_rate": 1, "output_rate": 1,
         "input_unit_size": 10, "output_unit_size": 1, "max_output_units": 10,
         "encoder": "fixture-v1", "billing_mode": "per_token"},
        "2025-01-01T00:00:00+00:00", model_alias="cancel-model", ctx=ONE,
    )
    usage_ledger.set_period_limit("ai", "EUR", 100, 100, "fixture-v1", now=now, ctx=ONE)
    with repo.db() as con:
        con.execute("UPDATE settings SET value='active' WHERE key='usage_enforcement_state'")

    class Provider:
        id = "cancel-ai"
        cfg = {"model": "cancel-model", "max_output_tokens": 10,
               "system_instructions": "fixture", "usage_operation": "ai-cancel"}

        def _complete_unmetered(self, prompt):
            return "{}", {"prompt_tokens": 2, "completion_tokens": 1}

    job = discovery(["metered-cancel"])
    queue_first(job)

    def handler(detail, step, claim):
        usage_runtime.ai_complete(Provider(), "settle before cancellation")
        assert jobs.request_cancel(job["job_id"], ctx=ONE)["status"] == "cancel_requested"
        return "result:discarded-after-cancel"

    result = asyncio.run(background_worker.process(
        job["job_id"], handlers={"profile_discovery": handler}
    ))
    assert result["status"] == "cancelled"
    with repo.db() as con:
        links = con.execute(
            "SELECT usage_request_ref FROM background_job_step_usage WHERE job_id=?",
            (job["job_id"],),
        ).fetchall()
    assert len(links) == 1
    assert usage_ledger.get(links[0]["usage_request_ref"], ctx=ONE)["state"] == "settled"
