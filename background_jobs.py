"""Actor-scoped durable background-job store (OE-004B).

The module owns short atomic state transitions only.  It never performs AI,
HTTP, queue or Blob I/O while a database transaction is open.
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
import time
import uuid

import config
import context
import profile_context
import repository as repo

CONTRACT_VERSION = 1
JOB_TYPES = frozenset({
    "profile_discovery", "profile_intelligence",
    "resume_generation", "application_draft",
})
ACTIVE_STATES = ("dispatch_pending", "queued", "running", "cancel_requested")
TERMINAL_STATES = ("succeeded", "failed", "cancelled", "superseded")
ERROR_CODES = frozenset({
    "provider_unavailable", "provider_outcome_unknown", "budget_exhausted",
    "invalid_output", "timeout", "worker_error", "poison_message",
    "dispatch_failed", "handler_missing",
})
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")


class JobError(RuntimeError): pass
class JobDenied(JobError): pass
class JobNotFound(JobError): pass
class JobConflict(JobError): pass
class JobBusy(JobError): pass


@dataclass(frozen=True)
class Claim:
    job_id: str
    step_id: int
    step_sequence: int
    fence: int
    token: str
    lease_until: int
    tenant_id: int
    user_id: int


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _actor(explicit=None) -> context.ActorContext:
    actor = context.require_context(explicit)
    if actor.is_system:
        raise JobDenied("Background jobs require one authenticated user.")
    return actor


def _secret() -> bytes:
    value = (os.environ.get("BACKGROUND_JOB_HMAC_SECRET")
             or os.environ.get("USAGE_HMAC_SECRET")
             or os.environ.get("SECRET_KEY") or config.SECRET_KEY)
    if len(value) < 32:
        raise JobDenied("Background-job HMAC secret is missing or too short.")
    return value.encode()


def _digest(label: str, value: str) -> str:
    return hmac.new(_secret(), f"{label}\0{value}".encode(), hashlib.sha256).hexdigest()


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _identifier(value, label: str, *, empty: bool = False) -> str:
    value = str(value or "")
    if empty and not value:
        return ""
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {label}.")
    return value


def _positive(value, label: str, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}.")
    value = int(value)
    if value < 1 or value > maximum:
        raise ValueError(f"Invalid {label}.")
    return value


def _safe_input(job_type: str, values: dict | None) -> dict:
    """Validate the complete allowlist; arbitrary text never enters the store."""
    if job_type not in JOB_TYPES:
        raise ValueError("Unsupported background-job type.")
    if not isinstance(values, dict):
        raise ValueError("Background-job input must be an object.")
    allowed = {
        "profile_discovery": {"request_id", "profile_id", "sources", "max_items", "language", "kind"},
        "profile_intelligence": {"request_id", "profile_id", "cv_id", "language"},
        "resume_generation": {"request_id", "profile_id", "language", "template", "include_photo"},
        "application_draft": {"request_id", "profile_id", "opportunity_id", "application_id", "language"},
    }[job_type]
    if set(values) - allowed:
        raise ValueError("Background-job input contains unsupported fields.")
    clean = {}
    for key, value in values.items():
        if key in {"profile_id", "cv_id", "opportunity_id", "application_id", "max_items"}:
            clean[key] = _positive(value, key, 500 if key == "max_items" else 2_147_483_647)
        elif key == "request_id":
            clean[key] = _identifier(value, "work request ID")
        elif key == "sources":
            if not isinstance(value, list) or not value or len(value) > 32:
                raise ValueError("Discovery sources must be a non-empty list.")
            clean[key] = [_identifier(v, "source") for v in value]
        elif key == "language":
            if value not in ("sq", "en", "de"):
                raise ValueError("Unsupported language.")
            clean[key] = value
        elif key == "kind":
            if value not in ("job", "local_job", "freelance", "product_lead", "all"):
                raise ValueError("Unsupported discovery kind.")
            clean[key] = value
        elif key == "template":
            clean[key] = _identifier(value, "resume template")
        elif key == "include_photo":
            if not isinstance(value, bool):
                raise ValueError("include_photo must be boolean.")
            clean[key] = value
    clean.setdefault("profile_id", 1)
    clean.setdefault("language", "sq")
    if job_type == "profile_discovery":
        clean.setdefault("max_items", 50)
        clean.setdefault("kind", "all")
        if "sources" not in clean:
            raise ValueError("Discovery sources are required.")
    if job_type == "resume_generation":
        clean.setdefault("template", "professional")
        clean.setdefault("include_photo", False)
    if job_type == "application_draft" and not ({"opportunity_id", "application_id"} & clean.keys()):
        raise ValueError("An opportunity reference is required.")
    return clean


def _planned_steps(job_type: str, values: dict) -> list[str]:
    if job_type == "profile_discovery":
        steps = [f"source:{source}" for source in values["sources"]]
        return [*steps, "summarize"] if values.get("request_id") else steps
    if job_type == "profile_intelligence":
        return ["analyze", "validate"]
    if job_type == "resume_generation":
        return ["draft", "validate"]
    return ["draft", "validate"]


def profile_revision_token(ctx=None) -> str:
    """Opaque HMAC over every canonical profile fact and search preference."""
    actor = _actor(ctx)
    with context.run_as(actor):
        profile = profile_context.build_context(repo.get_profile(ctx=actor))
        # Derived/output sections (intelligence, resume exports) may appear in
        # sections_present while a job runs; they are not profile inputs.
        profile.pop("meta", None)
        snapshot = {
            "profile": profile,
            "filters": repo.get_filters(ctx=actor),
        }
    return _digest("profile-revision-v1", _canonical(snapshot))


def _view(row) -> dict:
    return {key: row[key] for key in (
        "job_id", "job_type", "status", "progress_current", "progress_total",
        "result_ref", "error_code", "retry_of_job_id", "created_at", "updated_at",
        "started_at", "finished_at",
    )}


def _owned(con, job_id: str, actor: context.ActorContext):
    _identifier(job_id, "job ID")
    row = con.execute(
        "SELECT * FROM background_jobs WHERE job_id=? AND tenant_id=? AND user_id=?",
        (job_id, actor.tenant_id, actor.user_id),
    ).fetchone()
    if not row:
        raise JobNotFound("Background job not found.")
    return row


def create(job_type: str, values: dict, *, retry_of_job_id: str | None = None,
           ctx=None) -> dict:
    actor = _actor(ctx)
    clean = _safe_input(job_type, values)
    revision = profile_revision_token(actor)
    fingerprint = _digest("job-input-v1", _canonical({"type": job_type, "input": clean}))
    steps = _planned_steps(job_type, clean)
    created = _ts()
    job_id = str(uuid.uuid4())
    retry_ref = _identifier(retry_of_job_id, "retry job ID", empty=True) or None
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        if retry_ref:
            previous = _owned(con, retry_ref, actor)
            if previous["status"] not in TERMINAL_STATES:
                raise JobConflict("Vetëm një punë e përfunduar mund të provohet përsëri.")
            if previous["job_type"] != job_type or previous["input_json"] != _canonical(clean):
                raise JobConflict("Riprova duhet të ruajë të njëjtën kërkesë.")
        existing = con.execute(
            """SELECT * FROM background_jobs
               WHERE tenant_id=? AND user_id=? AND job_type=?
                 AND profile_revision_token=? AND input_fingerprint=?
                 AND status IN ('dispatch_pending','queued','running','cancel_requested')
               ORDER BY created_at LIMIT 1""",
            (actor.tenant_id, actor.user_id, job_type, revision, fingerprint),
        ).fetchone()
        if existing:
            return _view(existing)
        con.execute(
            """INSERT INTO background_jobs(
               job_id,tenant_id,user_id,job_type,status,profile_revision_token,
               input_fingerprint,input_json,progress_total,retry_of_job_id,request_id,created_at,updated_at)
               VALUES(?,?,?,?,'dispatch_pending',?,?,?,?,?,?,?,?)""",
            (job_id, actor.tenant_id, actor.user_id, job_type, revision, fingerprint,
             _canonical(clean), len(steps), retry_ref, clean.get("request_id"), created, created),
        )
        for sequence, step_type in enumerate(steps, 1):
            con.execute(
                """INSERT INTO background_job_steps(
                   job_id,sequence,step_type,status,created_at,updated_at)
                   VALUES(?,?,?,'pending',?,?)""",
                (job_id, sequence, _identifier(step_type, "step type"), created, created),
            )
        con.execute(
            """INSERT INTO background_job_outbox(
               job_id,step_sequence,contract_version,state,created_at,updated_at)
               VALUES(?,?,?,'pending',?,?)""",
            (job_id, 1, CONTRACT_VERSION, created, created),
        )
        row = con.execute("SELECT * FROM background_jobs WHERE job_id=?", (job_id,)).fetchone()
    return _view(row)


def get(job_id: str, ctx=None) -> dict:
    actor = _actor(ctx)
    with repo.db() as con:
        return _view(_owned(con, job_id, actor))


def public_detail(job_id: str, ctx=None) -> dict:
    """Actor-scoped progress view; excludes input JSON, fingerprints and leases."""
    actor = _actor(ctx)
    with repo.db() as con:
        job = _owned(con, job_id, actor)
        steps = con.execute(
            """SELECT sequence,step_type,status,result_ref,error_code,attempt_count,
                      started_at,finished_at
               FROM background_job_steps WHERE job_id=? ORDER BY sequence""", (job_id,)
        ).fetchall()
    result = _view(job)
    result["steps"] = [dict(step) for step in steps]
    return result


def list_jobs(limit: int = 30, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    limit = _positive(limit, "limit", 100)
    with repo.db() as con:
        rows = con.execute(
            """SELECT * FROM background_jobs WHERE tenant_id=? AND user_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (actor.tenant_id, actor.user_id, limit),
        ).fetchall()
    return [_view(row) for row in rows]


def actor_for_job(job_id: str) -> context.ActorContext:
    """Resolve ownership from trusted storage, then revalidate active membership."""
    _identifier(job_id, "job ID")
    with repo.db() as con:
        row = con.execute(
            """SELECT j.user_id,j.tenant_id,u.status AS user_status
               FROM background_jobs j JOIN users u ON u.id=j.user_id
               WHERE j.job_id=?""", (job_id,),
        ).fetchone()
    if not row or row["user_status"] != "active":
        raise JobNotFound("Background job not found.")
    import tenancy
    return tenancy.resolve_actor(row["user_id"], row["tenant_id"])


def worker_detail(job_id: str, ctx=None) -> dict:
    actor = _actor(ctx)
    with repo.db() as con:
        row = _owned(con, job_id, actor)
        steps = con.execute(
            "SELECT * FROM background_job_steps WHERE job_id=? ORDER BY sequence", (job_id,)
        ).fetchall()
    out = _view(row)
    out["input"] = json.loads(row["input_json"])
    out["profile_revision_token"] = row["profile_revision_token"]
    out["steps"] = [dict(step) for step in steps]
    return out


def pending_outbox(limit: int = 20, *, job_id: str | None = None,
                   now_epoch: int | None = None) -> list[dict]:
    """Internal dispatcher read; callers receive operational references only."""
    limit = _positive(limit, "limit", 100)
    epoch = int(time.time() if now_epoch is None else now_epoch)
    args: list = [epoch]
    where = "o.state='pending' AND o.next_attempt_at<=?"
    if job_id:
        where += " AND o.job_id=?"
        args.append(_identifier(job_id, "job ID"))
    args.append(limit)
    with repo.db() as con:
        rows = con.execute(
            f"""SELECT o.* FROM background_job_outbox o
                 JOIN background_jobs j ON j.job_id=o.job_id
                 WHERE {where} AND j.status IN ('dispatch_pending','queued')
                 ORDER BY o.id LIMIT ?""", args,
        ).fetchall()
    return [dict(row) for row in rows]


def mark_outbox_sent(outbox_id: int, message_id: str = "") -> bool:
    message_id = _identifier(message_id, "transport message ID", empty=True)
    updated = _ts()
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM background_job_outbox WHERE id=?", (int(outbox_id),)).fetchone()
        if not row:
            raise JobNotFound("Outbox entry not found.")
        if row["state"] == "sent":
            return False
        cur = con.execute(
            """UPDATE background_job_outbox SET state='sent',publish_attempts=publish_attempts+1,
               transport_message_id=?,last_error_code='',updated_at=?,sent_at=?
               WHERE id=? AND state='pending'""",
            (message_id, updated, updated, int(outbox_id)),
        )
        if cur.rowcount != 1:
            return False
        job = con.execute("SELECT status FROM background_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        if job and job["status"] in ("dispatch_pending", "queued"):
            con.execute(
                """UPDATE background_job_steps SET status='queued',updated_at=?
                   WHERE job_id=? AND sequence=? AND status='pending'""",
                (updated, row["job_id"], row["step_sequence"]),
            )
            con.execute(
                """UPDATE background_jobs SET status='queued',updated_at=?
                   WHERE job_id=? AND status='dispatch_pending'""", (updated, row["job_id"]),
            )
            con.execute(
                "INSERT INTO background_job_events(job_id,event_type,step_sequence,created_at) VALUES(?,'queued',?,?)",
                (row["job_id"], row["step_sequence"], updated),
            )
    return True


def mark_outbox_failed(outbox_id: int, error_code: str = "dispatch_failed",
                       *, now_epoch: int | None = None) -> None:
    if error_code not in ERROR_CODES:
        raise ValueError("Unsupported job error code.")
    epoch = int(time.time() if now_epoch is None else now_epoch)
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT publish_attempts FROM background_job_outbox WHERE id=? AND state='pending'",
                          (int(outbox_id),)).fetchone()
        if not row:
            return
        attempts = int(row["publish_attempts"]) + 1
        delay = min(300, 2 ** min(attempts, 8))
        con.execute(
            """UPDATE background_job_outbox SET publish_attempts=?,next_attempt_at=?,
               last_error_code=?,updated_at=? WHERE id=? AND state='pending'""",
            (attempts, epoch + delay, error_code, _ts(), int(outbox_id)),
        )


def claim(job_id: str, owner: str, *, lease_seconds: int = 120,
          now_epoch: int | None = None, ctx=None) -> Claim | None:
    actor = _actor(ctx)
    owner = _identifier(owner, "lease owner")
    lease_seconds = _positive(lease_seconds, "lease", 600)
    epoch = int(time.time() if now_epoch is None else now_epoch)
    token = secrets.token_urlsafe(32)
    token_hash = _digest("job-claim-v1", token)
    updated = _ts()
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        job = _owned(con, job_id, actor)
        if job["status"] in TERMINAL_STATES:
            return None
        if job["status"] == "cancel_requested":
            _cancel_in_transaction(con, job_id, updated)
            return None
        if job["status"] not in ("queued", "running"):
            return None
        step = con.execute(
            """SELECT * FROM background_job_steps WHERE job_id=?
               AND status IN ('pending','queued','running') ORDER BY sequence LIMIT 1""",
            (job_id,),
        ).fetchone()
        if not step:
            return None
        if step["status"] == "running" and int(step["lease_until"]) >= epoch:
            return None
        cur = con.execute(
            """UPDATE background_job_steps SET status='running',attempt_count=attempt_count+1,
               lease_owner=?,lease_token_hash=?,lease_until=?,fence=fence+1,
               started_at=COALESCE(started_at,?),updated_at=?
               WHERE id=? AND (status IN ('pending','queued') OR
                   (status='running' AND lease_until<?))""",
            (owner, token_hash, epoch + lease_seconds, updated, updated, step["id"], epoch),
        )
        if cur.rowcount != 1:
            return None
        claimed = con.execute("SELECT * FROM background_job_steps WHERE id=?", (step["id"],)).fetchone()
        con.execute(
            """UPDATE background_jobs SET status='running',started_at=COALESCE(started_at,?),updated_at=?
               WHERE job_id=?""", (updated, updated, job_id),
        )
        event = "started" if int(claimed["attempt_count"]) == 1 else "retrying"
        con.execute(
            "INSERT INTO background_job_events(job_id,event_type,step_sequence,created_at) VALUES(?,?,?,?)",
            (job_id, event, claimed["sequence"], updated),
        )
    return Claim(job_id, claimed["id"], claimed["sequence"], claimed["fence"],
                 token, epoch + lease_seconds, actor.tenant_id, actor.user_id)


def _claimed(con, claim_value: Claim, actor: context.ActorContext):
    if (claim_value.tenant_id, claim_value.user_id) != (actor.tenant_id, actor.user_id):
        raise JobNotFound("Background job not found.")
    row = con.execute(
        """SELECT s.*,j.status AS job_status,j.profile_revision_token
           FROM background_job_steps s JOIN background_jobs j ON j.job_id=s.job_id
           WHERE s.id=? AND s.job_id=? AND j.tenant_id=? AND j.user_id=?""",
        (claim_value.step_id, claim_value.job_id, actor.tenant_id, actor.user_id),
    ).fetchone()
    expected = _digest("job-claim-v1", claim_value.token)
    if (not row or row["status"] != "running" or row["fence"] != claim_value.fence
            or not hmac.compare_digest(row["lease_token_hash"], expected)):
        raise JobConflict("Background-job claim is stale.")
    return row


def renew(claim_value: Claim, *, lease_seconds: int = 120,
          now_epoch: int | None = None, ctx=None) -> Claim:
    actor = _actor(ctx)
    lease_seconds = _positive(lease_seconds, "lease", 600)
    epoch = int(time.time() if now_epoch is None else now_epoch)
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        _claimed(con, claim_value, actor)
        cur = con.execute(
            """UPDATE background_job_steps SET lease_until=?,updated_at=?
               WHERE id=? AND fence=? AND status='running'""",
            (epoch + lease_seconds, _ts(), claim_value.step_id, claim_value.fence),
        )
        if cur.rowcount != 1:
            raise JobConflict("Background-job claim is stale.")
    return Claim(claim_value.job_id, claim_value.step_id, claim_value.step_sequence,
                 claim_value.fence, claim_value.token, epoch + lease_seconds,
                 actor.tenant_id, actor.user_id)


def attach_usage_request(claim_value: Claim, usage_handle: str, ctx=None) -> str:
    """Tie an actor-owned OE-003 usage request to the claimed step.

    A step may perform several metered calls (for example draft plus review),
    so every handle is recorded. The legacy scalar keeps older diagnostics
    compatible and points to the first handle only.
    """
    actor = _actor(ctx)
    import usage_ledger
    usage = usage_ledger.get(str(usage_handle), ctx=actor)
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        step = _claimed(con, claim_value, actor)
        con.execute(
            """INSERT OR IGNORE INTO background_job_step_usage(
               step_id,job_id,usage_request_ref,created_at) VALUES(?,?,?,?)""",
            (claim_value.step_id, claim_value.job_id, str(usage_handle), _ts()),
        )
        con.execute(
            """UPDATE background_job_steps SET
               usage_request_ref=CASE WHEN usage_request_ref='' THEN ? ELSE usage_request_ref END,
               updated_at=? WHERE id=?""",
            (str(usage_handle), _ts(), claim_value.step_id),
        )
    return usage["request_id"]


def linked_usage_requests(claim_value: Claim, ctx=None) -> list[str]:
    """Return all usage handles for the active claim, including old v17 rows."""
    actor = _actor(ctx)
    with repo.db() as con:
        step = _claimed(con, claim_value, actor)
        rows = con.execute(
            """SELECT usage_request_ref FROM background_job_step_usage
               WHERE step_id=? AND job_id=? ORDER BY created_at,usage_request_ref""",
            (claim_value.step_id, claim_value.job_id),
        ).fetchall()
    handles = [row["usage_request_ref"] for row in rows]
    legacy = step["usage_request_ref"]
    if legacy and legacy not in handles:
        handles.insert(0, legacy)
    return handles


def complete(claim_value: Claim, result_ref: str, *, current_revision: str,
             ctx=None) -> dict:
    actor = _actor(ctx)
    result_ref = _identifier(result_ref, "result reference")
    current_revision = _identifier(current_revision, "profile revision")
    updated = _ts()
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        step = _claimed(con, claim_value, actor)
        if step["job_status"] == "cancel_requested":
            _cancel_in_transaction(con, claim_value.job_id, updated)
            return _view(_owned(con, claim_value.job_id, actor))
        if not hmac.compare_digest(step["profile_revision_token"], current_revision):
            con.execute(
                """UPDATE background_job_steps SET status='superseded',result_ref=?,
                   lease_owner='',lease_token_hash='',lease_until=0,finished_at=?,updated_at=?
                   WHERE id=?""", (result_ref, updated, updated, claim_value.step_id),
            )
            con.execute(
                """UPDATE background_job_steps SET status='superseded',finished_at=?,updated_at=?
                   WHERE job_id=? AND status IN ('pending','queued')""",
                (updated, updated, claim_value.job_id),
            )
            con.execute(
                """UPDATE background_jobs SET status='superseded',result_ref=?,finished_at=?,updated_at=?
                   WHERE job_id=?""", (result_ref, updated, updated, claim_value.job_id),
            )
            con.execute(
                "INSERT INTO background_job_events(job_id,event_type,step_sequence,created_at) VALUES(?,'superseded',?,?)",
                (claim_value.job_id, claim_value.step_sequence, updated),
            )
            return _view(_owned(con, claim_value.job_id, actor))
        con.execute(
            """UPDATE background_job_steps SET status='succeeded',result_ref=?,
               lease_owner='',lease_token_hash='',lease_until=0,finished_at=?,updated_at=?
               WHERE id=?""", (result_ref, updated, updated, claim_value.step_id),
        )
        next_step = con.execute(
            "SELECT * FROM background_job_steps WHERE job_id=? AND status='pending' ORDER BY sequence LIMIT 1",
            (claim_value.job_id,),
        ).fetchone()
        if next_step:
            con.execute(
                """UPDATE background_jobs SET status='dispatch_pending',progress_current=?,updated_at=?
                   WHERE job_id=?""", (claim_value.step_sequence, updated, claim_value.job_id),
            )
            con.execute(
                """INSERT OR IGNORE INTO background_job_outbox(
                   job_id,step_sequence,contract_version,state,created_at,updated_at)
                   VALUES(?,?,?,'pending',?,?)""",
                (claim_value.job_id, next_step["sequence"], CONTRACT_VERSION, updated, updated),
            )
            con.execute(
                "INSERT INTO background_job_events(job_id,event_type,step_sequence,created_at) VALUES(?,'progress',?,?)",
                (claim_value.job_id, claim_value.step_sequence, updated),
            )
        else:
            con.execute(
                """UPDATE background_jobs SET status='succeeded',progress_current=progress_total,
                   result_ref=?,finished_at=?,updated_at=? WHERE job_id=?""",
                (result_ref, updated, updated, claim_value.job_id),
            )
            con.execute(
                "INSERT INTO background_job_events(job_id,event_type,step_sequence,created_at) VALUES(?,'completed',?,?)",
                (claim_value.job_id, claim_value.step_sequence, updated),
            )
        return _view(_owned(con, claim_value.job_id, actor))


def fail(claim_value: Claim, error_code: str, *, retryable: bool = False,
         max_attempts: int = 8, ctx=None) -> dict:
    actor = _actor(ctx)
    if error_code not in ERROR_CODES:
        raise ValueError("Unsupported job error code.")
    updated = _ts()
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        step = _claimed(con, claim_value, actor)
        # Cancellation wins every race with a late handler outcome. A running
        # provider may still settle its usage, but neither success nor failure
        # may put this job back into the queue.
        if step["job_status"] == "cancel_requested":
            _cancel_in_transaction(con, claim_value.job_id, updated)
            return _view(_owned(con, claim_value.job_id, actor))
        retry = bool(retryable and int(step["attempt_count"]) < _positive(max_attempts, "attempt limit", 20)
                     and error_code != "provider_outcome_unknown")
        if retry:
            con.execute(
                """UPDATE background_job_steps SET status='queued',error_code=?,lease_owner='',
                   lease_token_hash='',lease_until=0,updated_at=? WHERE id=?""",
                (error_code, updated, claim_value.step_id),
            )
            con.execute("UPDATE background_jobs SET status='queued',error_code=?,updated_at=? WHERE job_id=?",
                        (error_code, updated, claim_value.job_id))
            event = "retrying"
        else:
            final_code = "poison_message" if retryable and int(step["attempt_count"]) >= max_attempts else error_code
            con.execute(
                """UPDATE background_job_steps SET status='failed',error_code=?,lease_owner='',
                   lease_token_hash='',lease_until=0,finished_at=?,updated_at=? WHERE id=?""",
                (final_code, updated, updated, claim_value.step_id),
            )
            con.execute(
                """UPDATE background_job_steps SET status='cancelled',finished_at=?,updated_at=?
                   WHERE job_id=? AND status IN ('pending','queued')""",
                (updated, updated, claim_value.job_id),
            )
            con.execute(
                """UPDATE background_jobs SET status='failed',error_code=?,finished_at=?,updated_at=?
                   WHERE job_id=?""", (final_code, updated, updated, claim_value.job_id),
            )
            event = "failed"
        con.execute(
            "INSERT INTO background_job_events(job_id,event_type,step_sequence,created_at) VALUES(?,?,?,?)",
            (claim_value.job_id, event, claim_value.step_sequence, updated),
        )
        return _view(_owned(con, claim_value.job_id, actor))


def _cancel_in_transaction(con, job_id: str, updated: str) -> None:
    con.execute(
        """UPDATE background_job_steps SET status='cancelled',lease_owner='',lease_token_hash='',
           lease_until=0,finished_at=?,updated_at=?
           WHERE job_id=? AND status IN ('pending','queued','running')""",
        (updated, updated, job_id),
    )
    con.execute(
        """UPDATE background_jobs SET status='cancelled',finished_at=?,updated_at=?
           WHERE job_id=? AND status NOT IN ('succeeded','failed','cancelled','superseded')""",
        (updated, updated, job_id),
    )
    con.execute(
        "INSERT INTO background_job_events(job_id,event_type,created_at) VALUES(?,'cancelled',?)",
        (job_id, updated),
    )


def request_cancel(job_id: str, ctx=None) -> dict:
    actor = _actor(ctx)
    updated = _ts()
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        job = _owned(con, job_id, actor)
        if job["status"] in TERMINAL_STATES:
            return _view(job)
        if job["status"] == "running":
            con.execute("UPDATE background_jobs SET status='cancel_requested',updated_at=? WHERE job_id=?",
                        (updated, job_id))
            con.execute(
                "INSERT INTO background_job_events(job_id,event_type,created_at) VALUES(?,'cancel_requested',?)",
                (job_id, updated),
            )
        else:
            _cancel_in_transaction(con, job_id, updated)
        return _view(_owned(con, job_id, actor))


def retry(job_id: str, ctx=None) -> dict:
    actor = _actor(ctx)
    current_revision = profile_revision_token(actor)
    with repo.db() as con:
        job = _owned(con, job_id, actor)
        if job["status"] not in TERMINAL_STATES:
            raise JobConflict("Vetëm një punë e përfunduar mund të provohet përsëri.")
        if not hmac.compare_digest(
            job["profile_revision_token"], current_revision
        ):
            raise JobConflict("Profili ndryshoi. Nise veprimin përsëri me profilin aktual.")
        values = json.loads(job["input_json"])
        job_type = job["job_type"]
    return create(job_type, values, retry_of_job_id=job_id, ctx=actor)


def events(job_id: str, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    with repo.db() as con:
        _owned(con, job_id, actor)
        rows = con.execute(
            "SELECT event_type,step_sequence,created_at FROM background_job_events WHERE job_id=? ORDER BY id",
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]
