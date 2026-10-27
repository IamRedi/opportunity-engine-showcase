"""Schema v17 for durable background jobs (OE-004B).

Only operational metadata and validated canonical references belong here.  CV
text, prompts, provider bodies, secrets and credential-bearing URLs are kept in
their domain stores and are never copied into the job runtime.
"""

SCHEMA_VERSION = 17

DDL = [
    """
    CREATE TABLE IF NOT EXISTS background_work_requests (
        request_id TEXT PRIMARY KEY,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        request_type TEXT NOT NULL CHECK(request_type IN (
            'profile_discovery','profile_intelligence',
            'resume_generation','application_draft')),
        request_json TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        profile_revision_token TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        UNIQUE(tenant_id,user_id,request_type,profile_revision_token,request_fingerprint)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_background_requests_owner ON background_work_requests(tenant_id,user_id,created_at)",
    """
    CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        job_type TEXT NOT NULL CHECK(job_type IN (
            'profile_discovery','profile_intelligence',
            'resume_generation','application_draft')),
        status TEXT NOT NULL CHECK(status IN (
            'dispatch_pending','queued','running','cancel_requested',
            'succeeded','failed','cancelled','superseded')),
        profile_revision_token TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        input_json TEXT NOT NULL DEFAULT '{}',
        progress_current INTEGER NOT NULL DEFAULT 0 CHECK(progress_current >= 0),
        progress_total INTEGER NOT NULL CHECK(progress_total > 0),
        result_ref TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        retry_of_job_id TEXT REFERENCES background_jobs(job_id),
        request_id TEXT REFERENCES background_work_requests(request_id),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_background_jobs_active_input
       ON background_jobs(tenant_id,user_id,job_type,profile_revision_token,input_fingerprint)
       WHERE status IN ('dispatch_pending','queued','running','cancel_requested')""",
    "CREATE INDEX IF NOT EXISTS idx_background_jobs_owner ON background_jobs(tenant_id,user_id,created_at)",
    "CREATE INDEX IF NOT EXISTS idx_background_jobs_status ON background_jobs(status,updated_at)",
    """
    CREATE TABLE IF NOT EXISTS background_job_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES background_jobs(job_id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK(sequence >= 1),
        step_type TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'pending','queued','running','succeeded','failed','cancelled','superseded')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        usage_request_ref TEXT NOT NULL DEFAULT '',
        result_ref TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT '',
        lease_owner TEXT NOT NULL DEFAULT '',
        lease_token_hash TEXT NOT NULL DEFAULT '',
        lease_until INTEGER NOT NULL DEFAULT 0,
        fence INTEGER NOT NULL DEFAULT 0 CHECK(fence >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        UNIQUE(job_id,sequence)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_background_steps_claim ON background_job_steps(job_id,status,sequence,lease_until)",
    """
    CREATE TABLE IF NOT EXISTS background_job_step_usage (
        step_id INTEGER NOT NULL REFERENCES background_job_steps(id) ON DELETE CASCADE,
        job_id TEXT NOT NULL REFERENCES background_jobs(job_id) ON DELETE CASCADE,
        usage_request_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(step_id,usage_request_ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_background_step_usage_job ON background_job_step_usage(job_id,step_id)",
    """
    CREATE TABLE IF NOT EXISTS background_job_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES background_jobs(job_id) ON DELETE CASCADE,
        step_sequence INTEGER NOT NULL CHECK(step_sequence >= 1),
        contract_version INTEGER NOT NULL CHECK(contract_version > 0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sent')),
        publish_attempts INTEGER NOT NULL DEFAULT 0 CHECK(publish_attempts >= 0),
        next_attempt_at INTEGER NOT NULL DEFAULT 0,
        transport_message_id TEXT NOT NULL DEFAULT '',
        last_error_code TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        sent_at TEXT,
        UNIQUE(job_id,step_sequence,contract_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_background_outbox_pending ON background_job_outbox(state,next_attempt_at,id)",
    """
    CREATE TABLE IF NOT EXISTS background_job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES background_jobs(job_id) ON DELETE CASCADE,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'queued','started','progress','retrying','completed',
            'failed','cancel_requested','cancelled','superseded')),
        step_sequence INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_background_events_job ON background_job_events(job_id,id)",
]


def ensure_additive(con) -> None:
    """Finish additive v17 columns when upgrading an earlier local v17 build."""
    columns = {str(row[1]).casefold() for row in con.execute(
        "PRAGMA table_info(background_jobs)"
    ).fetchall()}
    if "request_id" not in columns:
        con.execute(
            "ALTER TABLE background_jobs ADD COLUMN request_id TEXT REFERENCES background_work_requests(request_id)"
        )
