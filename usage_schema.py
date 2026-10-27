"""Schema v16 for the internal usage ledger (OE-003B).

The schema stores accounting metadata only. Prompts, CV text, provider responses,
emails, phone numbers and credential-bearing URLs do not belong in these tables.
"""

SCHEMA_VERSION = 16

DDL = [
    """
    CREATE TABLE IF NOT EXISTS usage_price_cards (
        price_card_id TEXT PRIMARY KEY,
        meter TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_alias TEXT NOT NULL DEFAULT '',
        currency TEXT NOT NULL,
        currency_scale INTEGER NOT NULL CHECK(currency_scale > 0),
        billing_unit TEXT NOT NULL CHECK(billing_unit IN ('tokens','fetch','hop')),
        card_snapshot TEXT NOT NULL DEFAULT '{}',
        effective_from TEXT NOT NULL,
        effective_until TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','retired')),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_cards_meter ON usage_price_cards(meter,status,effective_from)",
    """
    CREATE TABLE IF NOT EXISTS usage_budget_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        meter TEXT NOT NULL,
        currency TEXT NOT NULL,
        currency_scale INTEGER NOT NULL CHECK(currency_scale > 0),
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        limit_amount INTEGER NOT NULL CHECK(limit_amount >= 0),
        limit_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(tenant_id,user_id,meter,period_start)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_period_actor ON usage_budget_periods(tenant_id,user_id,period_start)",
    """
    CREATE TABLE IF NOT EXISTS usage_requests (
        request_id TEXT PRIMARY KEY,
        tenant_id INTEGER NOT NULL REFERENCES tenants(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        operation TEXT NOT NULL,
        provider TEXT NOT NULL,
        model_alias TEXT NOT NULL DEFAULT '',
        meter TEXT NOT NULL,
        idempotency_key_hash TEXT NOT NULL DEFAULT '',
        input_fingerprint TEXT NOT NULL,
        fingerprint_key_version TEXT NOT NULL,
        price_card_id TEXT NOT NULL REFERENCES usage_price_cards(price_card_id),
        period_id INTEGER NOT NULL REFERENCES usage_budget_periods(id),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_request_idempotency ON usage_requests(tenant_id,user_id,operation,idempotency_key_hash) WHERE idempotency_key_hash <> ''",
    "CREATE INDEX IF NOT EXISTS idx_usage_request_actor ON usage_requests(tenant_id,user_id,created_at)",
    """
    CREATE TABLE IF NOT EXISTS usage_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL REFERENCES usage_requests(request_id) ON DELETE CASCADE,
        attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
        retry_of_attempt_id INTEGER REFERENCES usage_attempts(id),
        state TEXT NOT NULL CHECK(state IN ('reserved','dispatching','sent_unknown','reconciling','settled','released','cancelled')),
        reserved_amount INTEGER NOT NULL CHECK(reserved_amount >= 0),
        settled_charge INTEGER CHECK(settled_charge IS NULL OR settled_charge >= 0),
        currency TEXT NOT NULL,
        currency_scale INTEGER NOT NULL CHECK(currency_scale > 0),
        price_snapshot TEXT NOT NULL,
        claim_owner TEXT NOT NULL DEFAULT '',
        claim_token_hash TEXT NOT NULL DEFAULT '',
        claim_until INTEGER NOT NULL DEFAULT 0,
        fence INTEGER NOT NULL DEFAULT 0,
        provider_evidence_hash TEXT NOT NULL DEFAULT '',
        usage_meta TEXT NOT NULL DEFAULT '{}',
        error_class TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(request_id,attempt_no)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_attempt_state ON usage_attempts(state,claim_until)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_attempt_retry_once ON usage_attempts(retry_of_attempt_id) WHERE retry_of_attempt_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS usage_finalizations (
        attempt_id INTEGER PRIMARY KEY REFERENCES usage_attempts(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL REFERENCES usage_requests(request_id) ON DELETE CASCADE,
        final_state TEXT NOT NULL CHECK(final_state IN ('settled','released','cancelled')),
        settled_charge INTEGER,
        usage_meta TEXT NOT NULL DEFAULT '{}',
        evidence_hash TEXT NOT NULL DEFAULT '',
        finalized_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage_evidence_events (
        attempt_id INTEGER NOT NULL REFERENCES usage_attempts(id) ON DELETE CASCADE,
        event_id_hash TEXT NOT NULL,
        event_type TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(attempt_id,event_id_hash)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_usage_evidence_attempt ON usage_evidence_events(attempt_id,created_at)",
    "INSERT OR IGNORE INTO settings(key,value) VALUES('usage_enforcement_state','disabled')",
]
