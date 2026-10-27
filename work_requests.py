"""Actor-owned domain inputs referenced by durable jobs, never queue payloads."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import uuid

import background_jobs
import context
import repository as repo

FORBIDDEN_KEYS = frozenset({
    "api_key", "token", "authorization", "password", "secret",
    "cv_text", "prompt", "provider_response", "headers",
})


class WorkRequestNotFound(LookupError): pass


def _actor(explicit=None):
    actor = context.require_context(explicit)
    if actor.is_system:
        raise PermissionError("Work requests require one authenticated user.")
    return actor


def _scan(value, depth=0):
    if depth > 8:
        raise ValueError("Work request is too deeply nested.")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in FORBIDDEN_KEYS:
                raise ValueError("Work request contains an unsupported field.")
            _scan(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise ValueError("Work request contains too many items.")
        for item in value:
            _scan(item, depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("Work request contains an unsupported value.")
    elif isinstance(value, str) and len(value) > 2_000:
        raise ValueError("Work request text is too long.")


def store(request_type: str, payload: dict, *, profile_revision_token: str,
          ctx=None) -> str:
    actor = _actor(ctx)
    if request_type not in background_jobs.JOB_TYPES or not isinstance(payload, dict):
        raise ValueError("Unsupported work request.")
    _scan(payload)
    raw = background_jobs._canonical(payload)
    if len(raw.encode()) > 64 * 1024:
        raise ValueError("Work request is too large.")
    fingerprint = background_jobs._digest("work-request-v1", raw)
    identity = background_jobs._digest(
        "work-request-id-v1",
        f"{actor.tenant_id}:{actor.user_id}:{request_type}:{profile_revision_token}:{fingerprint}",
    )
    request_id = str(uuid.UUID(hex=identity[:32]))
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=30)
    with repo.db() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            """INSERT OR IGNORE INTO background_work_requests(
               request_id,tenant_id,user_id,request_type,request_json,request_fingerprint,
               profile_revision_token,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (request_id, actor.tenant_id, actor.user_id, request_type, raw, fingerprint,
             profile_revision_token, created.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds")),
        )
        row = con.execute(
            """SELECT request_json,request_fingerprint,profile_revision_token
               FROM background_work_requests WHERE request_id=? AND tenant_id=? AND user_id=?""",
            (request_id, actor.tenant_id, actor.user_id),
        ).fetchone()
        if (not row or row["request_json"] != raw or row["request_fingerprint"] != fingerprint
                or row["profile_revision_token"] != profile_revision_token):
            raise RuntimeError("Work request identity conflict.")
    return request_id


def load(request_id: str, request_type: str, ctx=None) -> dict:
    actor = _actor(ctx)
    background_jobs._identifier(request_id, "work request ID")
    with repo.db() as con:
        row = con.execute(
            """SELECT * FROM background_work_requests
               WHERE request_id=? AND request_type=? AND tenant_id=? AND user_id=?""",
            (request_id, request_type, actor.tenant_id, actor.user_id),
        ).fetchone()
    if not row:
        raise WorkRequestNotFound("Work request not found.")
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        raise WorkRequestNotFound("Work request not found.") from None
    if expires <= datetime.now(timezone.utc):
        raise WorkRequestNotFound("Work request expired.")
    payload = json.loads(row["request_json"])
    _scan(payload)
    return payload
