"""Shtresa e të dhënave (repository) — i vetmi modul me SQL.

Rrugët në app.py nuk prekin SQLite direkt (spec §12, TD-1). Përmban:
- skemën kanonike v1 (opportunities + opportunity_sources + status_events, FTS5)
- migrimin idempotent nga tabela legacy `jobs` (me backup file para migrimit)
- normalizimin e URL-ve kanonike + deduplikim L2 (spec §13)
- kërkimin full-text FTS5 me fallback LIKE (spec §24)
- shërbimin e skorimit deterministik + shpjegimin e përputhjes (spec §14)
"""
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import classifier
import background_job_schema
import config
import context
import matching
import usage_schema
from cv_identity import identity as cv_entry_identity

SCHEMA_VERSION = background_job_schema.SCHEMA_VERSION

# ------------------------------------------------------------- skema v1

BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '[]',
    languages TEXT NOT NULL DEFAULT '[]',
    experience TEXT NOT NULL DEFAULT '',
    products TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS filters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    keywords TEXT NOT NULL DEFAULT '[]',
    exclude_keywords TEXT NOT NULL DEFAULT '[]',
    locations TEXT NOT NULL DEFAULT '[]',
    remote_ok INTEGER NOT NULL DEFAULT 1,
    onsite_ok INTEGER NOT NULL DEFAULT 1,
    hybrid_ok INTEGER NOT NULL DEFAULT 1,
    salary_min INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO profile (id) VALUES (1);
INSERT OR IGNORE INTO filters (id) VALUES (1);
"""

OPPORTUNITIES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS opportunities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id INTEGER,
        kind TEXT NOT NULL DEFAULT 'job',
        opportunity_type TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        external_id TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        dedup_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        org_name TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        remote_type TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '[]',
        salary_text TEXT NOT NULL DEFAULT '',
        salary_min INTEGER,
        salary_max INTEGER,
        salary_currency TEXT NOT NULL DEFAULT '',
        posted_at TEXT NOT NULL DEFAULT '',
        discovered_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        rule_score INTEGER NOT NULL DEFAULT 0,
        ai_score INTEGER,
        final_score INTEGER NOT NULL DEFAULT 0,
        match_factors TEXT,
        matched_requirements TEXT,
        missing_requirements TEXT,
        matched_product_ids TEXT,
        product_fit_score INTEGER,
        product_fit_reason TEXT NOT NULL DEFAULT '',
        distance_km REAL,
        travel_time_min INTEGER,
        transport_mode TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'new',
        saved_at TEXT,
        notes TEXT NOT NULL DEFAULT '',
        last_action_at TEXT,
        active_application_id INTEGER,
        kind_data TEXT NOT NULL DEFAULT '{}',
        excluded INTEGER NOT NULL DEFAULT 0
    )
    """,
    # idx_opp_dedup_user (per-user) krijohet te _v15_migrate — pas shtimit të user_id
    "CREATE INDEX IF NOT EXISTS idx_opp_kind_status ON opportunities(kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_opp_kind_score ON opportunities(kind, rule_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_opp_posted ON opportunities(posted_at)",
    "CREATE INDEX IF NOT EXISTS idx_opp_source ON opportunities(source)",
    """
    CREATE TABLE IF NOT EXISTS opportunity_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_os_uniq ON opportunity_sources(opportunity_id, source, url)",
    "CREATE INDEX IF NOT EXISTS idx_os_opp ON opportunity_sources(opportunity_id)",
    """
    CREATE TABLE IF NOT EXISTS status_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
        from_status TEXT,
        to_status TEXT NOT NULL,
        at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_se_opp ON status_events(opportunity_id)",
    # FTS5 mbi titull / organizatë / përshkrim (fallback: LIKE)
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS opportunities_fts USING fts5(
        title, org_name, description,
        content='opportunities', content_rowid='id'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS opp_fts_ai AFTER INSERT ON opportunities BEGIN
        INSERT INTO opportunities_fts(rowid, title, org_name, description)
        VALUES (new.id, new.title, new.org_name, new.description);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS opp_fts_ad AFTER DELETE ON opportunities BEGIN
        INSERT INTO opportunities_fts(opportunities_fts, rowid, title, org_name, description)
        VALUES ('delete', old.id, old.title, old.org_name, old.description);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS opp_fts_au AFTER UPDATE OF title, org_name, description ON opportunities BEGIN
        INSERT INTO opportunities_fts(opportunities_fts, rowid, title, org_name, description)
        VALUES ('delete', old.id, old.title, old.org_name, old.description);
        INSERT INTO opportunities_fts(rowid, title, org_name, description)
        VALUES (new.id, new.title, new.org_name, new.description);
    END
    """,
]

# ------------------------------------------------- skema v2 (Faza 2)

V2_DDL = [
    """
    CREATE TABLE IF NOT EXISTS search_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        kinds TEXT NOT NULL DEFAULT '["job"]',
        keywords TEXT NOT NULL DEFAULT '[]',
        exclude_keywords TEXT NOT NULL DEFAULT '[]',
        location TEXT NOT NULL DEFAULT '',
        radius_km INTEGER,
        remote_ok INTEGER NOT NULL DEFAULT 1,
        onsite_ok INTEGER NOT NULL DEFAULT 1,
        hybrid_ok INTEGER NOT NULL DEFAULT 1,
        salary_min INTEGER NOT NULL DEFAULT 0,
        hourly_rate_min REAL,
        budget_min INTEGER,
        min_match INTEGER NOT NULL DEFAULT 0,
        sources TEXT NOT NULL DEFAULT '[]',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS search_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER REFERENCES search_profiles(id) ON DELETE SET NULL,
        source TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        discovered INTEGER NOT NULL DEFAULT 0,
        new_count INTEGER NOT NULL DEFAULT 0,
        duplicate_count INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'running'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_runs_source ON search_runs(source, id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_profile ON search_runs(profile_id)",
]

# ------------------------------------------------- skema v3 (Faza 3B)

V3_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ai_cache (
        cache_key TEXT PRIMARY KEY,
        task TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        output TEXT NOT NULL,
        meta TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_aicache_task ON ai_cache(task, created_at)",
]

# ------------------------------------------------- skema v6 (Faza 4A)

V6_DDL = [
    """
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
        application_type TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT '',
        language TEXT NOT NULL DEFAULT 'sq',
        subject TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft',
        latest_version INTEGER NOT NULL DEFAULT 0,
        approved_at TEXT,
        submitted_at TEXT,
        generator TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_apps_opp ON applications(opportunity_id)",
    """
    CREATE TABLE IF NOT EXISTS application_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        version_number INTEGER NOT NULL,
        kind TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(application_id, version_number)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_appver_app ON application_versions(application_id, version_number)",
]

# ------------------------------------------------- skema v7 (Faza 4C)

# seksione JSON-dokumente të profilit (spec §6.2) — shtesa pa migrime të mëdha
PROFILE_SECTIONS = (
    "skills", "languages", "experience", "education", "certifications",
    "portfolio", "projects", "products",
)

V7_DDL = [
    """
    CREATE TABLE IF NOT EXISTS profile_sections (
        profile_id INTEGER NOT NULL DEFAULT 1,
        section TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (profile_id, section)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_cv (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL DEFAULT 1,
        filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        mime TEXT NOT NULL DEFAULT '',
        size_bytes INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT NOT NULL DEFAULT '',
        parse_status TEXT NOT NULL DEFAULT 'pending',
        parsed_text TEXT NOT NULL DEFAULT '',
        uploaded_at TEXT NOT NULL
    )
    """,
]

# ------------------------------------------------- skema v8 (Faza 5A)

V8_DDL = [
    "ALTER TABLE opportunities ADD COLUMN l3_key TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_opp_l3 ON opportunities(l3_key)",
]

# profilet e mbjella marrin burimet e reja 5A (backfill idempotent për emra njësoj)
PROFILE_SOURCE_BACKFILL = {
    "Freelance": ["hackernews_freelance", "remotive_contract", "jobicy_contract"],
    "Product Leads": ["hn_leads"],
    "Local / Mini Jobs": ["local_feed"],
}

# ------------------------------------------------- skema v9 (Faza 5B)

V9_DDL = [
    """
    CREATE TABLE IF NOT EXISTS opportunity_lenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
        lens TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'derived',
        detected_need TEXT NOT NULL DEFAULT '',
        opportunity_type TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(opportunity_id, lens)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_lens_opp ON opportunity_lenses(lens, opportunity_id)",
]

# ------------------------------------------------- skema v10 (Faza 7A)

V10_DDL = [
    """
    CREATE TABLE IF NOT EXISTS schedule_configs (
        profile_id INTEGER PRIMARY KEY REFERENCES search_profiles(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 1,
        cadence TEXT NOT NULL DEFAULT 'daily',
        timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
        run_window_time TEXT NOT NULL DEFAULT '08:00',
        min_match INTEGER NOT NULL DEFAULT 0,
        notify_new_only INTEGER NOT NULL DEFAULT 1,
        kinds TEXT NOT NULL DEFAULT '[]',
        sources TEXT NOT NULL DEFAULT '[]',
        last_run_at TEXT,
        next_run_at TEXT,
        last_success_at TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        lease_until TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sched_due ON schedule_configs(enabled, next_run_at)",
    """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER,
        opportunity_id INTEGER,
        type TEXT NOT NULL,
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        dedup_key TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        read_at TEXT,
        dismissed_at TEXT,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(created_at)",
]

# ------------------------------------------------- skema v11 (Faza 7B)

V11_DDL = [
    """
    CREATE TABLE IF NOT EXISTS email_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedup_key TEXT NOT NULL UNIQUE,
        channel TEXT NOT NULL DEFAULT 'email',
        status TEXT NOT NULL DEFAULT 'sent',
        error TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        recipient TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        sent_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edlv_created ON email_deliveries(created_at)",
    """
    CREATE TABLE IF NOT EXISTS digest_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sent_at TEXT NOT NULL,
        since_at TEXT NOT NULL DEFAULT '',
        items_count INTEGER NOT NULL DEFAULT 0,
        recipient TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ok',
        error TEXT NOT NULL DEFAULT ''
    )
    """,
]

# ------------------------------------------------- skema v12 (Faza 4D)

V12_DDL = [
    "ALTER TABLE profile_cv ADD COLUMN extract_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE profile_cv ADD COLUMN extract_method TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE profile_cv ADD COLUMN parser_version TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE profile_cv ADD COLUMN extracted_text TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE profile_cv ADD COLUMN extracted_at TEXT",
    "ALTER TABLE profile_cv ADD COLUMN extract_error TEXT NOT NULL DEFAULT ''",
    """
    CREATE TABLE IF NOT EXISTS cv_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cv_id INTEGER NOT NULL REFERENCES profile_cv(id) ON DELETE CASCADE,
        section TEXT NOT NULL,
        field TEXT NOT NULL,
        proposed_value TEXT NOT NULL,
        normalized_value TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '',
        confidence REAL NOT NULL DEFAULT 0.8,
        status TEXT NOT NULL DEFAULT 'pending',
        source_ref TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        UNIQUE(cv_id, section, field, normalized_value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cvsugg_cv ON cv_suggestions(cv_id, status)",
]

# ------------------------------------------------- skema v13 (Faza 6)

V13_DDL = [
    """
    CREATE TABLE IF NOT EXISTS profile_gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        label TEXT NOT NULL,
        current_state TEXT NOT NULL DEFAULT '',
        target_state TEXT NOT NULL DEFAULT '',
        severity TEXT NOT NULL DEFAULT 'soft',
        willingness TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        section TEXT NOT NULL,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        normalized_value TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        source_ref TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        UNIQUE(section, kind, normalized_value)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pgap ON profile_gaps(active)",
    "CREATE INDEX IF NOT EXISTS idx_psugg ON profile_suggestions(section, status)",
]

SEED_PROFILES = [
    ("Professional Jobs", ["job"], []),
    ("Freelance", ["freelance"], ["hackernews_freelance", "remotive_contract", "jobicy_contract"]),
    ("Product Leads", ["product_lead"], ["hn_leads"]),
    ("Local / Mini Jobs", ["local_job"], ["local_feed"]),
]

# ------------------------------------------------- skema v5 (Faza 3D)

V5_DDL = [
    "ALTER TABLE eval_items ADD COLUMN human_ext TEXT NOT NULL DEFAULT '{}'",
]

# ------------------------------------------------- skema v4 (Faza 3C)

V4_DDL = [
    """
    CREATE TABLE IF NOT EXISTS eval_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL DEFAULT '',
        task TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        base_url TEXT NOT NULL DEFAULT '',
        seed INTEGER NOT NULL,
        sample_size INTEGER NOT NULL,
        opportunity_ids TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'running',
        max_calls INTEGER NOT NULL DEFAULT 25,
        max_seconds INTEGER NOT NULL DEFAULT 300,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        calls INTEGER NOT NULL DEFAULT 0,
        ok_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        cached_count INTEGER NOT NULL DEFAULT 0,
        total_latency_ms INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        blocked_reason TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eval_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
        opportunity_id INTEGER NOT NULL,
        seq INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        error TEXT NOT NULL DEFAULT '',
        cached INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER NOT NULL DEFAULT 0,
        usage_tokens INTEGER NOT NULL DEFAULT 0,
        output TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '{}',
        human_rating INTEGER,
        human_notes TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_evalitems_run ON eval_items(run_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_evalruns_task ON eval_runs(task, id)",
]

# ------------------------------------------------------ URL kanonike (L2)

_TRACKING_KEYS = {"utm", "gclid", "fbclid", "igshid", "mc_cid", "mc_eid"}


def canonical_url(url: str) -> str:
    """URL i normalizuar për dedup — host lowercase, pa fragment, pa tracking
    params, pa slash fundor, query i renditur. URL origjinal ruhet veç."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlsplit(raw)
    except ValueError:
        return raw
    scheme = p.scheme.lower() or "https"
    host = p.netloc.lower()
    if scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    if scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    path = p.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    qsl = parse_qsl(p.query, keep_blank_values=True)
    kept = [
        (k, v) for k, v in qsl
        if not k.lower().startswith("utm_")
        and k.lower() not in _TRACKING_KEYS
        and not k.lower().startswith("_ga")
    ]
    kept.sort()
    return urlunsplit((scheme, host, path, urlencode(kept), ""))


# ------------------------------------------------------------ ndihmës

def db(path=None) -> sqlite3.Connection:
    import cloud_database
    if path is None and cloud_database.enabled():
        return cloud_database.connect()
    con = sqlite3.connect(path or config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _parse_dt(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except ValueError:
        return None


def time_ago(val: str, fallback: str = "") -> str:
    dt = _parse_dt(val)
    if not dt:
        return fallback[:10] or "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = datetime.now(timezone.utc) - dt
    if d < timedelta(minutes=1):
        return "tani"
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} min më parë"
    if d < timedelta(hours=36):
        return f"{int(d.total_seconds() // 3600)} orë më parë"
    if d < timedelta(days=30):
        return f"{d.days} ditë më parë"
    return dt.strftime("%d.%m.%Y")


def _lines(text: str) -> list[str]:
    if isinstance(text,(list,tuple)):
        return [str(value).strip() for value in text if str(value).strip()]
    return [l.strip() for l in (text or "").splitlines() if l.strip()]


def deco(row) -> dict:
    """Rresht opportunities → dict i gatshëm për UI (spec §19)."""
    j = dict(row)
    try:
        j["tags_list"] = json.loads(j.get("tags") or "[]")
    except (ValueError, TypeError):
        j["tags_list"] = []
    j["ago"] = time_ago(j.get("posted_at") or j.get("discovered_at", ""),
                        j.get("discovered_at", ""))
    if not j.get("salary_text") and (j.get("salary_min") or j.get("salary_max")):
        lo = f"{j['salary_min']:,.0f}".replace(",", ".") if j.get("salary_min") else ""
        hi = f"{j['salary_max']:,.0f}".replace(",", ".") if j.get("salary_max") else ""
        j["salary_display"] = f"{lo}–{hi}" if lo or hi else ""
    else:
        j["salary_display"] = j.get("salary_text") or ""
    j["remote"] = j.get("remote_type") == "remote"
    j["score"] = j.get("rule_score") or 0
    j["status_label"] = config.STATUS_LABELS.get(j.get("status"), j.get("status", ""))
    kd = {}
    if j.get("kind_data"):
        try:
            kd = json.loads(j["kind_data"])
        except (ValueError, TypeError):
            kd = {}
    j["kd"] = kd
    j["hourly_rate"] = kd.get("hourly_rate")
    j["schedule"] = kd.get("schedule")
    j["category_label"] = config.LOCAL_CATEGORY_LABELS.get(
        kd.get("category") or j.get("opportunity_type") or "", "")
    return j


# ------------------------------------------- konteksti i aktorit (8C2)

def _actor(explicit=None) -> context.ActorContext:
    """Zgjidh ActorContext: explicit → ContextVar → (production: GABIM
    fail-closed) → (local/test: sistem, pa filtra — kompatibiteti i vjetër)."""
    return context.require_context(explicit)


def _uwhere(actor: context.ActorContext, col: str = "user_id",
            tcol: str | None = None) -> tuple[str, list]:
    """(klauzolë, args) për scoping user/tenant. Sistem (user_id=0) → pa filtra
    — VETËM për operacione të emërta të brendshme (migrim/backfill)."""
    if actor.is_system:
        return "1=1", []
    clause, args = f"{col} = ?", [actor.user_id]
    if tcol:
        clause += f" AND {tcol} = ?"
        args.append(actor.tenant_id)
    return clause, args


def _uid(actor: context.ActorContext) -> int:
    """user_id efektiv për lexime: sistem → zotëruesi legacy (vetëm local)."""
    return context.DEFAULT_OWNER_ID if actor.is_system else actor.user_id


def _stamp(actor: context.ActorContext) -> tuple[int, int]:
    """(user_id, tenant_id) për INSERT-e. Sistem → zotëruesi legacy —
    production kurrë nuk arrin këtu pa kontekst user (fail-closed)."""
    if actor.is_system:
        return context.DEFAULT_OWNER_ID, context.DEFAULT_TENANT_ID
    return actor.user_id, actor.tenant_id


# ------------------------------------------------- profil / filtra / settings

def get_profile(ctx=None) -> dict:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        con.execute("INSERT OR IGNORE INTO profile (user_id) VALUES (?)", (uid,))
        row = con.execute("SELECT * FROM profile WHERE user_id=?", (uid,)).fetchone()
    d = dict(row)
    d["skills"] = json.loads(d["skills"])
    d["languages"] = json.loads(d["languages"])
    d["products"] = json.loads(d.get("products") or "[]")
    # Search-only concepts stay separate from facts used in applications.
    import profile_intelligence
    with db() as con:
        d['search_signals'] = profile_intelligence.search_signals(con, uid)
        sections={r['section']:json.loads(r['data']) for r in con.execute('SELECT section,data FROM profile_sections WHERE user_id=?',(uid,))
                  if r['section'] in ('education','certifications','projects','preferences','interests')}
    for section,keys in {'education':('title','org','period'),'certifications':('name','details','org','year'),
                         'projects':('title','description','stack')}.items():
        d[section]=[' | '.join(str(e.get(k) or '') for k in keys if e.get(k)) for e in sections.get(section,[])]
    d['interests_not_experience']=[e['name'] for e in sections.get('interests',[]) if e.get('name')]
    d['stated_preferences']=[v for e in sections.get('preferences',[]) for k,v in e.items() if k!='source' and isinstance(v,str)]
    local=get_local_prefs(ctx=actor)
    if local:d['stated_preferences'].append('Local work preferences: '+json.dumps(local,ensure_ascii=False,sort_keys=True))
    with db() as con:
        filters=con.execute('SELECT salary_min,locations,remote_ok,onsite_ok,hybrid_ok FROM filters WHERE user_id=?',(uid,)).fetchone()
    if filters:d['stated_preferences'].append('Search preferences: '+json.dumps(dict(filters),ensure_ascii=False,sort_keys=True))
    return d


def save_profile(fields: dict, ctx=None) -> None:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        con.execute("INSERT OR IGNORE INTO profile (user_id) VALUES (?)", (uid,))
        before=dict(con.execute('SELECT * FROM profile WHERE user_id=?',(uid,)).fetchone())
        con.execute(
            """UPDATE profile SET name=?, title=?, email=?, phone=?, location=?,
               summary=?, skills=?, languages=?, experience=?, products=? WHERE user_id=?""",
            (
                fields["name"], fields["title"], fields["email"], fields["phone"],
                fields["location"], fields["summary"],
                json.dumps(_lines(fields["skills"]), ensure_ascii=False),
                json.dumps(_lines(fields["languages"]), ensure_ascii=False),
                fields["experience"],
                json.dumps(_lines(fields["products"]), ensure_ascii=False),
                uid,
            ),
        )
        for section in ('skills','languages','products'):
            values=_lines(fields[section])
            if values!=json.loads(before.get(section) or '[]'):
                data=[{'name':value} for value in values]
                if section=='languages':
                    for entry in data:
                        match=re.fullmatch(r'(.+?)\s+[–-]\s+(.+)',entry['name'])
                        if match:entry['name'],entry['level']=match.groups()
                con.execute('INSERT INTO profile_sections(user_id,profile_id,section,data,updated_at) VALUES (?,1,?,?,?) ON CONFLICT(user_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                            (uid,section,json.dumps(data,ensure_ascii=False),now()))
        if fields['experience']!=before.get('experience',''):
            # A manually replaced narrative becomes authoritative; old structured roles must not override it.
            con.execute("UPDATE profile_sections SET data='[]',updated_at=? WHERE user_id=? AND section='experience'",(now(),uid))
        after = dict(con.execute('SELECT * FROM profile WHERE user_id=?', (uid,)).fetchone())
    # OE-007E: thirrësi vendos scope-un e rillogarimit nga ndryshimi real.
    return rescore_scope_for_profile(before, after)


def get_filters(ctx=None) -> dict:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        con.execute("INSERT OR IGNORE INTO filters (user_id) VALUES (?)", (uid,))
        d = dict(con.execute("SELECT * FROM filters WHERE user_id=?", (uid,)).fetchone())
    for k in ("keywords", "exclude_keywords", "locations"):
        d[k] = json.loads(d[k])
    return d


def save_filters(f: dict, ctx=None) -> object:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        con.execute("INSERT OR IGNORE INTO filters (user_id) VALUES (?)", (uid,))
        before = dict(con.execute("SELECT * FROM filters WHERE user_id=?", (uid,)).fetchone())
        con.execute(
            """UPDATE filters SET keywords=?, exclude_keywords=?, locations=?,
               remote_ok=?, onsite_ok=?, hybrid_ok=?, salary_min=? WHERE user_id=?""",
            (
                json.dumps(_lines(f["keywords"]), ensure_ascii=False),
                json.dumps(_lines(f["exclude_keywords"]), ensure_ascii=False),
                json.dumps(_lines(f["locations"]), ensure_ascii=False),
                int(f["remote_ok"]), int(f["onsite_ok"]), int(f["hybrid_ok"]),
                int(f["salary_min"] or 0),
                uid,
            ),
        )
        after = dict(con.execute("SELECT * FROM filters WHERE user_id=?", (uid,)).fetchone())
    # OE-007E: scope sipas faktorëve të prekur nga filtrat.
    return rescore_scope_for_filters(before, after)


def _ukey(key: str, actor: context.ActorContext) -> str:
    """Celes per-user (u<uid>:key) kur aktori është user real; sistem → global."""
    return f"u{actor.user_id}:{key}" if not actor.is_system else key


def get_setting(key: str, default: str = "", ctx=None) -> str:
    """SETTINGS = konfigurim sistemi (global) mejas parazgjedhjes. Per-user
    LEXOHET VETËM me ctx EKSPLIKTIF (p.sh. preferencat email në notifier) —
    kurrë nëpërmjet ContextVar, që të mos mbishkruhen konfigurimet globale."""
    if ctx is not None and not ctx.is_system:
        with db() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?",
                              (_ukey(key, ctx),)).fetchone()
        if row:
            return row["value"]
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, ctx=None) -> None:
    key = _ukey(key, ctx) if ctx is not None and not ctx.is_system else key
    with db() as con:
        con.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_all_settings(ctx=None) -> dict:
    """Celesat globale; me ctx EKSPLIKTIF per-user → celesat u<uid>:* mbishkruajnë
    globalët (pa prefiks). Pa ctx → vetëm globalët (sjellja e vjetër)."""
    with db() as con:
        out = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}
    if ctx is not None and not ctx.is_system:
        prefix = f"u{ctx.user_id}:"
        with db() as con:
            for r in con.execute("SELECT key, value FROM settings WHERE key LIKE ?",
                                 (prefix + "%",)):
                out[r["key"][len(prefix):]] = r["value"]
    return out


# ------------------------------------------------- skorimi deterministik

def score_job(op: dict) -> tuple[int, bool]:
    """Adapter legacy → matching.score_job_compat (spec §14; kontrata e vjetër ruhet)."""
    return matching.score_job_compat(op)


def matching_profile(ctx=None):
    """Analyzed profiles use structured facts; preserve legacy scoring otherwise."""
    profile = get_profile(ctx)
    if not get_profile_section('intelligence', ctx)[0]:
        return profile
    for section in ('skills', 'languages', 'experience'):
        entries, _ = get_profile_section(section, ctx)
        if not entries:
            continue
        if section == 'skills':
            profile[section] = [e['name'] for e in entries if e.get('name')]
        elif section == 'languages':
            profile[section] = [' – '.join(str(e.get(k) or '') for k in ('name','level')).strip(' –') for e in entries]
        else:
            profile[section] = '\n'.join(' | '.join(str(e.get(k) or '') for k in ('title','org','period','description')) for e in entries)
    return profile


@dataclass
class ScoringContext:
    """Kontekst i përgatitur NJË herë për aktor/profile (OE-007E) — kalohet
    shprehimisht te rescore/upsert/import. Pa gjendje globale: izolimi i
    përdoruesit mbetet i lidhur me aktorin."""
    actor: object
    profile: dict
    filters: dict
    gaps: list
    local_prefs: dict
    matching: object  # matching.PreparedContext


def scoring_context(ctx=None) -> ScoringContext:
    """Përgatit NJË herë faket e pikimit për aktorin (profil, filtra, gaps,
    preferenca lokale + konteksti i përgatitur i matching)."""
    import matching
    actor = _actor(ctx)
    profile = matching_profile(actor)
    filters = get_filters(actor)
    gaps = list_gaps(active_only=True, ctx=actor)
    local_prefs = get_local_prefs(ctx=actor)
    return ScoringContext(actor=actor, profile=profile, filters=filters, gaps=gaps,
                          local_prefs=local_prefs,
                          matching=matching.prepare_context(profile, filters, local_prefs, gaps))


def rescore_scope_for_profile(before: dict, after: dict) -> object:
    """OE-007E: cili rillogarim kërkon ndryshimi i profilit?
    Kthen 'full', 'none', ose set faktorësh të pjesshëm. Konservativ:
    dyshimi → 'full'."""
    consumed_by = {
        "skills": "full", "products": "full",  # terma/katalog — skanojnë gjithçka
        "languages": "full",
        "experience": "full",  # faktor 'experience' — kërkon tituj, po rruga e plotë është e sigurt
        "location": {"location"},
        "title": {"role"},
    }
    scope = None
    for field in ("skills", "products", "languages", "experience", "location", "title"):
        if (before.get(field) or "") != (after.get(field) or ""):
            need = consumed_by[field]
            if need == "full":
                return "full"
            scope = need if scope is None else (scope | need)
    return scope if scope is not None else "none"


def rescore_scope_for_filters(before: dict, after: dict) -> object:
    """OE-007E: scope për ndryshimin e filtrave. `locations` s'konsumohet
    nga asnjë faktor; salary/workmode janë të pjesshëm; keywords/exclude → full."""
    scope = None
    for field in ("keywords", "exclude_keywords"):
        if (before.get(field) or "") != (after.get(field) or ""):
            return "full"
    if (before.get("salary_min") or 0) != (after.get("salary_min") or 0):
        scope = {"salary", "budget"} if scope is None else scope | {"salary", "budget"}
    for field in ("remote_ok", "onsite_ok", "hybrid_ok"):
        if bool(before.get(field)) != bool(after.get(field)):
            scope = {"workmode"} if scope is None else scope | {"workmode"}
    return scope if scope is not None else "none"


def rescore_scope_for_sections(sections) -> object:
    """OE-007E: scope sipas seksioneve të profilit që u ndryshuan.
    Seksionet pa konsum skorimesh → 'none' (asnjë rillogarim)."""
    full_sections = {"skills", "languages", "products", "experience", "intelligence",
                     "preferences", "local"}
    for section in sections:
        if section in full_sections:
            return "full"
        if section == "location":
            return {"location"}
    return "none"


def rescore_all(ctx=None, factors=None, prepared=None) -> int:
    """Rillogarit pikët + persiston faktorët strukturore (match_factors) —
    VETËM për mundësitë e aktorit (sistemi: të gjitha, për migrime).

    OE-007E:
    - `prepared` (repo.scoring_context()) → kontekst i përgatitur NJË herë
      për kalim; pa të, ndërtohet këtu.
    - `factors` ⊆ matching.PARTIAL_RESCORE_FACTORS → rillogarim i pjesshëm:
      vetëm këta faktorë rishkruhen mbi match_factors të ruajtura, pa lexim
      përshkrimesh; përndryshe kalim i plotë.
    - Shkrim vetëm kur dalja ndryshon materialisht (pikë/faktorë)."""
    actor = _actor(ctx)
    if factors == "none":
        return 0  # ndryshim pa konsum pikimesh — asgjë për të bërë
    if prepared is None:
        prepared = scoring_context(actor)
    prof, flt = prepared.profile, prepared.filters
    gaps, local_prefs, mctx = prepared.gaps, prepared.local_prefs, prepared.matching
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    partial = factors is not None and factors != "full"
    if partial:
        unsupported = set(factors) - matching.PARTIAL_RESCORE_FACTORS
        if unsupported:
            raise ValueError(
                f"faktorë pa mbështetje për rillogarim të pjesshëm: {sorted(unsupported)}")
    if partial:
        with db() as con:
            rows = con.execute(
                f"""SELECT id, title, location, remote_type, salary_min, kind_data,
                       match_factors, rule_score, final_score, excluded
                    FROM opportunities WHERE {uw}""", uargs).fetchall()
        updates = []
        for r in rows:
            try:
                stored = json.loads(r["match_factors"] or "{}")
            except (ValueError, TypeError):
                continue  # pa faktorë të ruajtur nuk ka mbi çfarë shkruhet
            stored_factors = stored.get("factors") or []
            touched = [f for f in stored_factors if f.get("id") in factors]
            if not touched:
                continue
            try:
                kd = json.loads(r["kind_data"] or "{}")
            except (ValueError, TypeError):
                kd = {}
            op = {"title": r["title"], "location": r["location"],
                  "remote_type": r["remote_type"], "salary_min": r["salary_min"],
                  "kind_data": kd}
            new_factors, unknown = [], []
            for f in stored_factors:
                if f.get("id") in factors:
                    nf = matching.rescore_factor(f["id"], op, mctx, f.get("weight") or 0.0)
                    nf = nf.to_dict()
                else:
                    nf = f
                new_factors.append(nf)
                if nf.get("status") == matching.UNKNOWN:
                    unknown.append(nf.get("id"))
            known = [f for f in new_factors if f.get("status") != "unknown"]
            total_w = sum(f.get("weight") or 0.0 for f in known)
            final = round(sum((f.get("value") or 0) * (f.get("weight") or 0.0)
                              for f in known) / total_w) if total_w else 0
            final = max(0, min(100, int(final)))
            stored["factors"] = new_factors
            stored["unknown"] = unknown
            stored["final_score"] = final
            blob = json.dumps(stored, ensure_ascii=False)
            if (r["rule_score"], r["final_score"], r["excluded"], r["match_factors"]) != \
                    (final, final, r["excluded"], blob):
                updates.append((final, final, r["excluded"], blob, r["id"]))
        if updates:  # pa transaksion shkrimi kur asnjë rresht s'ndryshon
            with db() as con:
                con.executemany("""UPDATE opportunities SET rule_score=?, final_score=?, excluded=?,
                                   match_factors=? WHERE id=?""", updates)
        return len(updates)

    with db() as con:
        rows = con.execute(f"SELECT id, title, description, tags, kind, location, remote_type,"
                           " salary_min, posted_at, discovered_at, opportunity_type,"
                           " rule_score, final_score, excluded, match_factors FROM opportunities"
                           f" WHERE {uw}", uargs).fetchall()
    updates = []
    # Finish reads/calculations before taking a write lock. Gaps and local
    # preferences are preloaded above, so scoring opens no other connections.
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except (ValueError, TypeError):
            tags = []
        op = dict(r) | {"tags": tags}
        result = matching.score(op, prof, flt, local_prefs=local_prefs, gaps=gaps, ctx=mctx)
        bd = result.factor_dict()
        blob = json.dumps(bd, ensure_ascii=False)
        matched_blob = json.dumps(bd.get("matched", []), ensure_ascii=False)
        missing_blob = json.dumps(bd.get("missing", []), ensure_ascii=False)
        # OE-007E: pa shkrim kur asgjë s'ndryshon (frekuenca e madhe në cloud).
        if (r["rule_score"], r["final_score"], r["excluded"], r["match_factors"]) != \
                (result.final_score, result.final_score, int(result.excluded), blob):
            updates.append((result.final_score, result.final_score, int(result.excluded),
                            blob, matched_blob, missing_blob, r["id"]))
    if updates:  # pa transaksion shkrimi kur asnjë rresht s'ndryshon
        with db() as con:
            con.executemany("""UPDATE opportunities SET rule_score=?, final_score=?, excluded=?,
                               match_factors=?, matched_requirements=?, missing_requirements=? WHERE id=?""", updates)
    return len(rows)


def breakdown_for(op: dict) -> dict:
    """Breakdown i freskët për panel (ngjit dekoden e UI mbi kontratën e matching)."""
    result = matching.score(op)
    return matching.breakdown_dict(result)


def match_breakdown(j: dict) -> dict:
    """Breakdown i strukturuar për panel (spec §14.5) — delegon te matching.score."""
    return breakdown_for(j)

def _norm_desc(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def l3_key(title: str, org: str, location: str) -> str:
    """Dedup L3: titull + organizatë + vend i normalizuar (spec §13).
    None kur titulli ose organizata bosh — L3 e sigurt kërkon të dyja."""
    def norm(s):
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()
    t, o = norm(title), norm(org)
    if not t or not o:
        return ""
    return f"{t}|{o}|{norm(location)}"


def upsert_opportunity(op: dict, ctx=None) -> str:
    """Kompatibitet: kthen new|dup|skip. Për ID përdor upsert_opportunity_full."""
    return upsert_opportunity_full(op, ctx)[0]


def upsert_opportunity_full(op: dict, ctx=None, scoring: "ScoringContext | None" = None) -> tuple[str, int | None]:
    """Shto/përditëso mundësi me dedup L2 (URL kanonik) + L3 (titull+org+vend)
    — dedup BRENDA user-it të aktorit (izolim i plotë mes përdoruesve).
    Kthen (new|dup|skip, opportunity_id|None).

    OE-007E: `scoring` (repo.scoring_context()) e përgatit kontekstin NJË herë
    për gjithë importin — pa pyetje profili/filtrash për çdo rresht; dup-i i
    pandryshuar nuk rishkruan kolonat e pikëve."""
    actor = _actor(ctx)
    uid, tid = _stamp(actor)
    uw, uargs = _uwhere(actor, "user_id")
    url = (op.get("url") or "").strip()
    canon = canonical_url(url)
    if not canon:
        return "skip", None
    kind = config.canon_kind(op.get("kind") or config.KIND_JOB)
    if kind == config.KIND_LOCAL:
        op = dict(op)
        kd = op.get("kind_data") if isinstance(op.get("kind_data"), dict) else {}
        if op.get("hourly_rate") and not kd.get("hourly_rate"):
            kd["hourly_rate"] = op["hourly_rate"]
        if kd:
            op["kind_data"] = kd
        # kategori kanonike nga teksti (pa shpikje — vetëm fjalë-kyçe njehse)
        if not (op.get("opportunity_type") or "").strip():
            text = f"{op.get('title') or ''} {op.get('description') or ''}".lower()
            for cat, keywords in config.LOCAL_CATEGORY_KEYWORDS.items():
                if any(kw in text for kw in keywords):
                    op["opportunity_type"] = cat
                    break
            else:
                op["opportunity_type"] = "other_local"
    ts = now()
    # Import-time scoring must see the same freshness fallback as rescore:
    # an undated listing is fresh by discovery (web previews stay UNKNOWN).
    if not (op.get("posted_at") or op.get("discovered_at")):
        op["discovered_at"] = ts
    if scoring is not None:
        result = matching.score(op, ctx=scoring.matching)
    else:
        result = matching.score(op)
    factors_json = json.dumps(result.factor_dict(), ensure_ascii=False)
    key = l3_key(op.get("title"), op.get("company") or op.get("org_name") or "",
                 op.get("location") or "")
    with db() as con:
        existing = con.execute(
            f"""SELECT id, kind, rule_score, final_score, excluded, match_factors
                FROM opportunities WHERE dedup_key=? AND {uw}""",
            (canon, *uargs)).fetchone()
        if existing is None and key:
            # L3 "safe": merge vetëm kur edhe përshkrimi normalizuar është i njëjtë
            # (ripost i vërtetë); përndryshe tituj të njëjtë në kompani të njëjtën
            # janë përkufizime të pavona → zë i re. Gjithmonë brenda user-it.
            cand = con.execute(
                f"""SELECT id, kind, rule_score, final_score, excluded, match_factors, description
                    FROM opportunities WHERE l3_key=? AND {uw} LIMIT 1""",
                (key, *uargs)).fetchone()
            if cand and _norm_desc(cand["description"]) == _norm_desc(op.get("description") or ""):
                existing = cand
        if existing:
            opp_id = existing["id"]
            # Indexed snippets must not replace the score of a full listing.
            if matching._is_web_preview(op):
                con.execute('UPDATE opportunities SET updated_at=? WHERE id=?', (ts, opp_id))
            else:
                score_unchanged = (
                    existing["rule_score"], existing["final_score"], existing["excluded"],
                    existing["match_factors"]) == (
                    result.final_score, result.final_score, int(result.excluded), factors_json)
                if score_unchanged:
                    # Dup i pandryshuar: vetëm dëshmia e parjeve — pa rishkrim pikësh.
                    con.execute('UPDATE opportunities SET updated_at=? WHERE id=?', (ts, opp_id))
                else:
                    con.execute(
                        """UPDATE opportunities SET updated_at=?, rule_score=?, final_score=?,
                           excluded=?, match_factors=? WHERE id=?""",
                        (ts, result.final_score, result.final_score, int(result.excluded),
                         factors_json, opp_id),
                    )
            _add_source_row(con, opp_id, op, url, ts, ts)
            try:
                classifier.attach_lenses(opp_id, {**op, 'kind':existing['kind']}, connection=con)  # 5B
            except Exception:
                pass  # lentët janë shtesë — kurrë s'thyejnë importin
            return "dup", opp_id
        cur = con.execute(
            """INSERT INTO opportunities
               (kind, opportunity_type, source, external_id, source_url, canonical_url,
                dedup_key, title, org_name, description, location, remote_type, tags,
                salary_text, salary_min, salary_max, posted_at, discovered_at, updated_at,
                rule_score, final_score, excluded, status, kind_data, match_factors, l3_key,
                user_id, tenant_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               RETURNING id""",
            (
                kind,
                op.get("opportunity_type") or "",
                op.get("source") or "manual",
                str(op.get("external_id") or ""),
                url, canon, canon,
                (op.get("title") or "").strip() or "(pa titull)",
                op.get("company", "") or op.get("org_name", "") or "",
                (op.get("description") or "")[:20000],
                op.get("location") or "",
                "remote" if op.get("remote") else "",
                json.dumps(op.get("tags") or [], ensure_ascii=False),
                op.get("salary_text") or "",
                int(op["salary_min"]) if op.get("salary_min") else None,
                int(op["salary_max"]) if op.get("salary_max") else None,
                op.get("posted_at") or "",
                ts, ts,
                result.final_score, result.final_score, int(result.excluded), "new",
                json.dumps(op.get("kind_data") or {}, ensure_ascii=False),
                factors_json,
                key,
                uid, tid,
            ),
        )
        inserted_rows = cur.fetchall()
        if len(inserted_rows) != 1:
            raise RuntimeError("Opportunity insert did not return an ID.")
        inserted = inserted_rows[0]
        opp_id = int(inserted["id"])
        if kind == config.KIND_LOCAL:
            try:
                import geo
                prefs = get_local_prefs()
                fresh = con.execute("SELECT kind_data FROM opportunities WHERE id=?",
                                    (opp_id,)).fetchone()
                op2 = dict(op) | {"kind_data": json.loads(fresh["kind_data"] or "{}")}
                loc = geo.resolve_op_location(op2, prefs)
                if loc["distance_km"] is not None:
                    con.execute("UPDATE opportunities SET distance_km=? WHERE id=?",
                                (loc["distance_km"], opp_id))
            except Exception:
                pass  # distanca është shtesë — pa të dhëna → UNKNOWN
        _add_source_row(con, opp_id, op, url, ts, ts)
        # Keep the cursor and lens write inside the same live transaction.
        # RETURNING is required because remote libSQL lastrowid may reflect an
        # FTS trigger insert rather than the canonical opportunities row.
        classifier.attach_lenses(opp_id, op, connection=con)
    return "new", opp_id


def _add_source_row(con, opp_id: int, op: dict, url: str, first_seen: str, last_seen: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO opportunity_sources "
        "(opportunity_id, source, external_id, url, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
        (opp_id, op.get("source") or "manual", str(op.get("external_id") or ""),
         url, first_seen, last_seen),
    )


# ---------------------------------------------------------------- pyetjet

def _fts_available() -> bool:
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.close()
        return True
    except sqlite3.Error:
        return False


_FTS_OK = None  # lazy


def _fts_ids(con, q: str):
    """ID-të nga FTS5; None kur FTS s'punon; [] kur s'ka match (→ fallback LIKE)."""
    global _FTS_OK
    if _FTS_OK is None:
        _FTS_OK = _fts_available()
    if not _FTS_OK:
        return None
    tokens = [t.replace('"', "").replace("*", "") for t in re.split(r"\s+", q.strip()) if t]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    expr = " AND ".join(f'"{t}"*' for t in tokens)
    try:
        return [r["rowid"] for r in
                con.execute("SELECT rowid FROM opportunities_fts WHERE opportunities_fts MATCH ?", (expr,))]
    except sqlite3.OperationalError:
        _FTS_OK = False
        return None


def list_opportunities(q="", kind=None, status="", source="", loc="",
                       remote="", mins=0, days=0, salary=0, sort="match",
                       limit=300, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    where, args = [], []
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    where.append(uw)
    args.extend(uargs)
    if kind:
        # 5B: lentët analitike — mundësia me lens përkatës hyn në feed pa duplikim
        where.append("(kind = ? OR EXISTS(SELECT 1 FROM opportunity_lenses L "
                     "WHERE L.opportunity_id = opportunities.id AND L.lens = ?))")
        args.extend([kind, kind])
    if status:
        where.append("status = ?")
        args.append(status)
    if source:
        where.append("source = ?")
        args.append(source)
    if loc:
        where.append("location LIKE ?")
        args.append(f"%{loc}%")
    if remote == "remote":
        where.append("remote_type = 'remote'")
    elif remote == "onsite":
        where.append("remote_type != 'remote'")
    if mins:
        where.append("rule_score >= ?")
        args.append(int(mins))
    if salary:
        where.append("salary_min >= ?")
        args.append(int(salary))
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M")
        where.append("COALESCE(NULLIF(posted_at,''), discovered_at) >= ?")
        args.append(cutoff)

    order = "rule_score DESC, discovered_at DESC" if sort != "new" else "discovered_at DESC"
    with db() as con:
        if q:
            ids = _fts_ids(con, q)
            if ids:
                where.append("id IN (%s)" % ",".join(["?"] * len(ids)))
                args.extend(ids)
            else:
                where.append("(title LIKE ? OR org_name LIKE ?)")
                args += [f"%{q}%"] * 2
        rows = con.execute(
            f"SELECT * FROM opportunities WHERE {' AND '.join(where)} "
            f"ORDER BY excluded ASC, {order} LIMIT ?",
            args + [limit],
        ).fetchall()
    out = [deco(r) for r in rows]
    _attach_lenses(out)
    _attach_local_meta(out)
    return out


def _attach_local_meta(jobs: list[dict]) -> None:
    """Worth-the-trip + distanca e freskët për rreshtat local_job (5D)."""
    import geo
    prefs = get_local_prefs()
    if not any(j.get("kind") == config.KIND_LOCAL for j in jobs):
        return
    for j in jobs:
        if j.get("kind") != config.KIND_LOCAL:
            continue
        loc = geo.resolve_op_location(j, prefs)
        j["distance_km"] = loc.get("distance_km")
        j["worth"] = geo.worth_the_trip(j, prefs, loc)


def _attach_lenses(jobs: list[dict]) -> None:
    ids = [j["id"] for j in jobs]
    for j, lenses in classifier.lenses_for_ids(ids).items():
        for j_row in jobs:
            if j_row["id"] == j:
                j_row["lenses"] = lenses


def status_counts(kind=None, ctx=None, *, include_lenses=False) -> dict:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    where, args = (f"WHERE kind = ? AND {uw}", [kind, *uargs]) if kind \
        else (f"WHERE {uw}", uargs)
    if kind and include_lenses:
        # Workspace counts use the same membership as list_opportunities.
        # Keep primary-kind counting as the default for existing callers.
        where = ("WHERE (kind = ? OR EXISTS(SELECT 1 FROM opportunity_lenses L "
                 "WHERE L.opportunity_id = opportunities.id AND L.lens = ?)) "
                 f"AND {uw}")
        args = [kind, kind, *uargs]
    with db() as con:
        return dict(con.execute(
            f"SELECT status, COUNT(*) FROM opportunities {where} GROUP BY status", args
        ).fetchall())


def distinct_sources(ctx=None) -> list[str]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    with db() as con:
        return [r["source"] for r in
                con.execute(f"SELECT DISTINCT source FROM opportunities WHERE {uw} "
                            "ORDER BY source", uargs)]


def best_matches(kind, limit=4, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM opportunities WHERE kind=? AND {uw} AND status='new' "
            "AND excluded=0 AND rule_score>0 "
            "ORDER BY rule_score DESC, COALESCE(NULLIF(posted_at,''), discovered_at) DESC LIMIT ?",
            (kind, *uargs, limit),
        ).fetchall()
    return [deco(r) for r in rows]


def get_opportunity(opp_id: int, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    with db() as con:
        row = con.execute(f"SELECT * FROM opportunities WHERE id=? AND {uw}",
                          (opp_id, *uargs)).fetchone()
    if not row:
        return None
    j = deco(row)
    j["lenses"] = classifier.lenses_for_ids([opp_id]).get(opp_id, [])
    if j["kind"] == config.KIND_LOCAL:
        import geo
        prefs = get_local_prefs()
        loc = geo.resolve_op_location(j, prefs)
        j["distance_km"] = loc.get("distance_km")
        j["worth"] = geo.worth_the_trip(j, prefs, loc)
    return j


def update_opportunity(opp_id: int, status=None, notes=None, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    ts = now()
    with db() as con:
        row = con.execute(f"SELECT * FROM opportunities WHERE id=? AND {uw}",
                          (opp_id, *uargs)).fetchone()
        if not row:
            return None
        if status is not None and status != row["status"]:
            con.execute("UPDATE opportunities SET status=?, last_action_at=? WHERE id=?",
                        (status, ts, opp_id))
            if status in ("saved", "lead_saved") and not row["saved_at"]:
                con.execute("UPDATE opportunities SET saved_at=? WHERE id=?", (ts, opp_id))
            con.execute(
                "INSERT INTO status_events (opportunity_id, from_status, to_status, at) VALUES (?,?,?,?)",
                (opp_id, row["status"], status, ts),
            )
        if notes is not None and notes != row["notes"]:
            con.execute("UPDATE opportunities SET notes=?, last_action_at=? WHERE id=?",
                        (notes, ts, opp_id))
        out = con.execute("SELECT * FROM opportunities WHERE id=?", (opp_id,)).fetchone()
    return deco(out)


def delete_opportunity(opp_id: int, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "user_id", "tenant_id")
    with db() as con:
        cur = con.execute(f"DELETE FROM opportunities WHERE id=? AND {uw}",
                          (opp_id, *uargs))
        return cur.rowcount > 0


# ------------------------------------------------------- migrimi (Faza 1)

def _backup_file(db_path: Path):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db_path.with_name(f"{db_path.name}.backup-{ts}")
    shutil.copy2(db_path, bak)
    return bak


def _migrate_legacy(con) -> int:
    """jobs → opportunities brenda një transaksioni; id-të ruhen të paprekura."""
    rows = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    for r in rows:
        j = dict(r)
        canon = canonical_url(j["url"]) or j["url"]
        status = config.canon_status(j["status"])
        kind = config.canon_kind(j.get("kind") or config.KIND_JOB)
        existing = con.execute("SELECT id FROM opportunities WHERE dedup_key=?", (canon,)).fetchone()
        if existing:
            opp_id = existing["id"]
        else:
            saved_at = j["first_seen"] if status in ("saved", "lead_saved") else None
            cur = con.execute(
                """INSERT INTO opportunities
                   (id, kind, opportunity_type, source, external_id, source_url,
                    canonical_url, dedup_key, title, org_name, description, location,
                    remote_type, tags, salary_text, salary_min, salary_max, posted_at,
                    discovered_at, updated_at, rule_score, final_score, excluded,
                    status, saved_at, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    j["id"], kind, j.get("opportunity_type") or "", j["source"], "",
                    j["url"], canon, canon,
                    j["title"], j["company"], j["description"], j["location"],
                    "remote" if j["remote"] else "",
                    j["tags"], j["salary_text"], j["salary_min"], j["salary_max"],
                    j.get("posted_at") or "",
                    j["first_seen"], j["last_seen"],
                    j["score"], j["score"], j["excluded"],
                    status, saved_at, j["notes"],
                ),
            )
            opp_id = cur.lastrowid
        con.execute(
            "INSERT OR IGNORE INTO opportunity_sources "
            "(opportunity_id, source, external_id, url, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
            (opp_id, j["source"], "", j["url"], j["first_seen"], j["last_seen"]),
        )
    return len(rows)


# ------------------------------------------------- migrimi v15 (Faza 8C2)
# Izolim i plotë per-user: profile/filters/profile_sections/profile_suggestions
# kalojnë nga rreshta globalë (id=1) → rreshta per-user; dedup per-user;
# tenant backfill; eval_runs.user_id; notifications.tenant_id.

def _table_cols(con, tbl: str) -> list[str]:
    return [r["name"] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]


def _pk_cols(con, tbl: str) -> list[str]:
    rows = sorted((r for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()
                   if r["pk"]), key=lambda r: r["pk"])
    return [r["name"] for r in rows]


def _has_unique(con, tbl: str, cols: list[str]) -> bool:
    """True nëse ekziston UNIQUE index/PK që mbulon saktësisht kolonat `cols`."""
    want = list(cols)
    for idx in con.execute(f"PRAGMA index_list({tbl})").fetchall():
        if not idx["unique"]:
            continue
        got = [r["name"] for r in
               con.execute(f"PRAGMA index_info({idx['name']})").fetchall()]
        if got == want:
            return True
    return _pk_cols(con, tbl) == want


def _v15_migrate(con) -> None:
    # profile: nga CHECK(id=1) → rresht per-user (user_id UNIQUE)
    if "user_id" not in _table_cols(con, "profile"):
        con.execute("""
            CREATE TABLE profile_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                skills TEXT NOT NULL DEFAULT '[]',
                languages TEXT NOT NULL DEFAULT '[]',
                experience TEXT NOT NULL DEFAULT '',
                products TEXT NOT NULL DEFAULT '[]'
            )""")
        con.execute("""
            INSERT INTO profile_new (id, user_id, name, title, email, phone, location,
                                     summary, skills, languages, experience, products)
            SELECT id, 1, name, title, email, phone, location, summary, skills,
                   languages, experience, products FROM profile""")
        con.execute("DROP TABLE profile")
        con.execute("ALTER TABLE profile_new RENAME TO profile")
    # filters: e njëjta gjë
    if "user_id" not in _table_cols(con, "filters"):
        con.execute("""
            CREATE TABLE filters_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                keywords TEXT NOT NULL DEFAULT '[]',
                exclude_keywords TEXT NOT NULL DEFAULT '[]',
                locations TEXT NOT NULL DEFAULT '[]',
                remote_ok INTEGER NOT NULL DEFAULT 1,
                onsite_ok INTEGER NOT NULL DEFAULT 1,
                hybrid_ok INTEGER NOT NULL DEFAULT 1,
                salary_min INTEGER NOT NULL DEFAULT 0
            )""")
        con.execute("""
            INSERT INTO filters_new (id, user_id, keywords, exclude_keywords, locations,
                                     remote_ok, onsite_ok, hybrid_ok, salary_min)
            SELECT id, 1, keywords, exclude_keywords, locations, remote_ok, onsite_ok,
                   hybrid_ok, salary_min FROM filters""")
        con.execute("DROP TABLE filters")
        con.execute("ALTER TABLE filters_new RENAME TO filters")
    # profile_sections: PK duhet të jetë (user_id, section)
    if not _has_unique(con, "profile_sections", ["user_id", "section"]):
        con.execute("""
            CREATE TABLE profile_sections_new (
                user_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL DEFAULT 1,
                section TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, section)
            )""")
        con.execute("""
            INSERT INTO profile_sections_new (user_id, profile_id, section, data, updated_at)
            SELECT COALESCE(user_id, 1), profile_id, section, data, updated_at
            FROM profile_sections""")
        con.execute("DROP TABLE profile_sections")
        con.execute("ALTER TABLE profile_sections_new RENAME TO profile_sections")
    # profile_suggestions: UNIQUE per-user (përndryshe user-i dytë bllokohet
    # nga UNIQUE(section, kind, normalized_value) global)
    if not _has_unique(con, "profile_suggestions",
                       ["user_id", "section", "kind", "normalized_value"]):
        con.execute("""
            CREATE TABLE profile_suggestions_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 1,
                section TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                normalized_value TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                source_ref TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                UNIQUE(user_id, section, kind, normalized_value)
            )""")
        # forma tolerante ndaj skemave të vjetra (v13 pa source_ref/user_id)
        ps_cols = set(_table_cols(con, "profile_suggestions"))
        src_ref = "source_ref" if "source_ref" in ps_cols else "''"
        reviewed = "reviewed_at" if "reviewed_at" in ps_cols else "NULL"
        uid_expr = "COALESCE(user_id, 1)" if "user_id" in ps_cols else "1"
        con.execute(f"""
            INSERT INTO profile_suggestions_new (id, user_id, section, kind, value,
                normalized_value, reason, status, source_ref, created_at, reviewed_at)
            SELECT id, {uid_expr}, section, kind, value, normalized_value,
                   reason, status, {src_ref}, created_at, {reviewed}
            FROM profile_suggestions""")
        con.execute("DROP TABLE profile_suggestions")
        con.execute("ALTER TABLE profile_suggestions_new RENAME TO profile_suggestions")
        con.execute("CREATE INDEX IF NOT EXISTS idx_psugg ON profile_suggestions(section, status)")
    # opportunities: tenant backfill + dedup per-user (hiqet uniku global)
    if "user_id" not in _table_cols(con, "eval_runs"):
        con.execute("ALTER TABLE eval_runs ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    con.execute("UPDATE opportunities SET tenant_id=1 WHERE tenant_id IS NULL")
    con.execute("DROP INDEX IF EXISTS idx_opp_dedup")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_opp_dedup_user "
                "ON opportunities(user_id, dedup_key)")
    if "tenant_id" not in _table_cols(con, "notifications"):
        con.execute("ALTER TABLE notifications ADD COLUMN tenant_id INTEGER")
    con.execute("UPDATE notifications SET tenant_id=1 WHERE tenant_id IS NULL")
    # profile_cv: siguro user_id (v14 e shtoi vetëm nëse tabela ekzistonte)
    if "user_id" not in _table_cols(con, "profile_cv"):
        con.execute("ALTER TABLE profile_cv ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    con.execute("UPDATE profile_cv SET user_id=1 WHERE user_id IS NULL")


def init(path=None) -> dict:
    """Krijo/azhnorua skemën. Idempotent: ekzekutimi i dytë s'bën asgjë.
    Backup file bëhet para çdo transformimi real (legacy `jobs` ose bump versioni).
    v1 → v2 (Faza 2): shton search_profiles + search_runs, mbush profilet bazë."""
    import cloud_database
    if path is None and cloud_database.enabled():
        # Compatibility build accepts the deployed S15/S16 schemas and S17.
        # Background dispatch remains fail-closed unless S17 is active.
        with db() as con:
            row = con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        if not row or row[0] not in ('15','16',str(SCHEMA_VERSION)):
            raise RuntimeError('Cloud database migration is required before deployment')
        return {'migrated':False,'upgraded':False,'rows':0,'backup':None,
                'schema_version':row[0],'compatibility':row[0]!=str(SCHEMA_VERSION)}
    db_path = Path(path) if path else config.DB_PATH
    info = {"migrated": False, "upgraded": False, "rows": 0, "backup": None}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(BOOTSTRAP_DDL)
        ver_row = con.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        version = ver_row["value"] if ver_row else ""
        legacy = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        needs_backup = bool(legacy) or (version not in ("", str(SCHEMA_VERSION)))
        if needs_backup and db_path.exists() and db_path.stat().st_size > 0:
            info["backup"] = str(_backup_file(db_path))
        with con:
            for stmt in OPPORTUNITIES_DDL:
                con.execute(stmt)
            if legacy:
                info["rows"] = _migrate_legacy(con)
                con.execute("DROP TABLE jobs")
                info["migrated"] = True
            for stmt in V2_DDL:
                con.execute(stmt)
            for stmt in V3_DDL:
                con.execute(stmt)
            for stmt in V4_DDL:
                con.execute(stmt)
            for stmt in V5_DDL:
                try:
                    con.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # kolona ekziston (migrim i pjesshëm i ndërprerë)
            for stmt in V6_DDL:
                con.execute(stmt)
            for stmt in V7_DDL:
                con.execute(stmt)
            for stmt in V8_DDL:
                try:
                    con.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # kolona/index ekziston (rigjenerim i pjesshëm)
            for stmt in V9_DDL:
                con.execute(stmt)
            for stmt in V10_DDL:
                con.execute(stmt)
            for stmt in V11_DDL:
                con.execute(stmt)
            for stmt in V12_DDL:
                try:
                    con.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # kolona ekziston
            for stmt in V13_DDL:
                con.execute(stmt)
            # v14: multi-user + tenant foundation
            _v14_stmts = [
                "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_login_at TEXT)",
                "CREATE TABLE IF NOT EXISTS tenants (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'active', branding TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS tenant_memberships (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'member', created_at TEXT NOT NULL, UNIQUE(tenant_id, user_id))",
            ]
            for stmt in _v14_stmts:
                con.execute(stmt)
            for tbl in ("opportunities", "applications",
                        "profile_sections", "profile_cv", "cv_suggestions",
                        "profile_gaps", "profile_suggestions", "schedule_configs",
                        "notifications", "search_runs", "email_deliveries",
                        "digest_runs", "search_profiles"):
                try:
                    con.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
                except sqlite3.OperationalError:
                    pass
            # v15: izolim per-user i plotë (8C2)
            _v15_migrate(con)
            # v16: OE-003B internal usage ledger; dispatch remains disabled.
            for stmt in usage_schema.DDL:
                con.execute(stmt)
            # v17: OE-004B durable job/outbox store; queue dispatch remains
            # separately disabled until the rollout gate is enabled.
            for stmt in background_job_schema.DDL:
                con.execute(stmt)
            background_job_schema.ensure_additive(con)
            con.execute("INSERT OR IGNORE INTO tenants (id,name,slug,status,branding,created_at,updated_at) VALUES (1,'Default','default','active','{}',datetime('now'),datetime('now'))")
            # 8C2: user-i legacy (id 1) mbillet VETËM jashtë production —
            # aty admin-i provisionohet nga env (auth_service.provision_env_user);
            # zero 'default user 1' me fjalëkalim të njohur në production.
            import os as _os
            if _os.environ.get("APP_ENV", "local") != "production":
                prof_row = con.execute("SELECT name, email FROM profile WHERE id=1 ORDER BY id LIMIT 1").fetchone()
                email = (prof_row[1] or "owner@local").strip() if prof_row else "owner@local"
                import hashlib as _hl
                pw_hash = _hl.sha256(b"changeme").hexdigest()
                con.execute("INSERT OR IGNORE INTO users (id,email,username,password_hash,display_name,status,created_at,updated_at) VALUES (1,?,?,?,'Owner','active',datetime('now'),datetime('now'))",
                            (email, email.split("@")[0], pw_hash))
                con.execute("INSERT OR IGNORE INTO tenant_memberships (tenant_id,user_id,role,created_at) VALUES (1,1,'owner',datetime('now'))")
            for tbl in ("opportunities", "applications", "notifications"):
                con.execute(f"UPDATE {tbl} SET user_id=1 WHERE user_id IS NULL")
            if version != str(SCHEMA_VERSION):
                # backfill 5B: lentët për mundësitë ekzistuese (pa duplikim)
                rows = con.execute("SELECT id, kind, title, description, opportunity_type, tags, kind_data FROM opportunities").fetchall()
                for r in rows:
                    op = dict(r)
                    try:
                        op["kind_data"] = json.loads(op.get("kind_data") or "{}")
                    except (ValueError, TypeError):
                        op["kind_data"] = {}
                    for lens in classifier.detect_lenses(op):
                        con.execute(
                            "INSERT OR IGNORE INTO opportunity_lenses (opportunity_id, lens, source, detected_need, opportunity_type, reason, created_at) VALUES (?,?,?,?,?,?,?)",
                            (op["id"], lens["lens"], lens["source"], lens["detected_need"],
                             lens["opportunity_type"], lens["reason"], now()))
            # mbjellja e seksioneve nga fushat legacy (vetëm nëse bosh — idempotent)
            if con.execute("SELECT COUNT(*) FROM profile_sections").fetchone()[0] == 0:
                _seed_profile_sections(con)
            # backfill l3_key për rreshtat e vjetër (v<8)
            rows = con.execute(
                "SELECT id, title, org_name, location FROM opportunities WHERE l3_key=''").fetchall()
            for r in rows:
                con.execute("UPDATE opportunities SET l3_key=? WHERE id=?",
                            (l3_key(r["title"], r["org_name"], r["location"]), r["id"]))
            if version != str(SCHEMA_VERSION):
                info["upgraded"] = True
                if con.execute("SELECT COUNT(*) FROM search_profiles").fetchone()[0] == 0:
                    ts = now()
                    for seed in SEED_PROFILES:
                        name, kinds = seed[0], seed[1]
                        sources = seed[2] if len(seed) > 2 else []
                        con.execute(
                            """INSERT INTO search_profiles
                               (name, kinds, sources, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (name, json.dumps(kinds), json.dumps(sources), ts, ts),
                        )
                else:
                    # backfill 5A: profileve të mbjella pa burime u shtohen burimet e reja
                    for name, kinds, sources in SEED_PROFILES:
                        if not sources:
                            continue
                        con.execute(
                            """UPDATE search_profiles SET sources=? WHERE name=? AND sources='[]'""",
                            (json.dumps(sources), name),
                        )
        with con:
            con.execute(
                "INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
    finally:
        con.close()
    return info


# ------------------------------------------------ profilet e kërkimit

_PROFILE_LIST_COLS = {"kinds", "keywords", "exclude_keywords", "sources"}


def _profile_deco(row) -> dict:
    p = dict(row)
    for col in _PROFILE_LIST_COLS:
        try:
            p[col] = json.loads(p.get(col) or "[]")
        except (ValueError, TypeError):
            p[col] = []
    p["active"] = bool(p.get("active"))
    return p


def list_search_profiles(active_only: bool = False, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    where = f"WHERE active=1 AND {uw}" if active_only else f"WHERE {uw}"
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM search_profiles {where} ORDER BY id", uargs
        ).fetchall()
    return [_profile_deco(r) for r in rows]


def get_search_profile(pid: int, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(f"SELECT * FROM search_profiles WHERE id=? AND {uw}",
                          (pid, *uargs)).fetchone()
    return _profile_deco(row) if row else None


_PROFILE_COLUMNS = {
    "name", "kinds", "keywords", "exclude_keywords", "location", "radius_km",
    "remote_ok", "onsite_ok", "hybrid_ok", "salary_min", "hourly_rate_min",
    "budget_min", "min_match", "sources", "active",
}


def create_search_profile(fields: dict, ctx=None) -> int:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    cols, vals = ["user_id", "created_at", "updated_at"], [uid, now(), now()]
    for k, v in fields.items():
        if k in _PROFILE_COLUMNS:
            cols.append(k)
            vals.append(v)
    ph = ",".join("?" * len(cols))
    with db() as con:
        cur = con.execute(
            f"INSERT INTO search_profiles ({','.join(cols)}) VALUES ({ph})", vals)
        return cur.lastrowid


def update_search_profile(pid: int, fields: dict, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    sets, vals = [], []
    for k, v in fields.items():
        if k in _PROFILE_COLUMNS:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        with db() as con:
            return con.execute(f"SELECT COUNT(*) FROM search_profiles WHERE id=? AND {uw}",
                               (pid, *uargs)).fetchone()[0] > 0
    sets.append("updated_at=?")
    vals.append(now())
    vals.extend([pid, *uargs])
    with db() as con:
        cur = con.execute(
            f"UPDATE search_profiles SET {', '.join(sets)} WHERE id=? AND {uw}", vals)
        return cur.rowcount > 0


def delete_search_profile(pid: int, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        cur = con.execute(f"DELETE FROM search_profiles WHERE id=? AND {uw}",
                          (pid, *uargs))
        return cur.rowcount > 0


# ------------------------------------------------- historiku i kërkimeve

def start_search_run(source: str, profile_id: int | None = None, ctx=None) -> int:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    with db() as con:
        cur = con.execute(
            "INSERT INTO search_runs (profile_id, source, started_at, status, user_id) "
            "VALUES (?, ?, ?, 'running', ?)",
            (profile_id, source, now(), uid),
        )
        return cur.lastrowid


def finish_search_run(run_id: int, discovered: int = 0, new: int = 0,
                      dup: int = 0, error: str = "", status: str = "ok",
                      ctx=None) -> None:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        con.execute(
            f"""UPDATE search_runs SET finished_at=?, discovered=?, new_count=?,
               duplicate_count=?, error=?, status=? WHERE id=? AND {uw}""",
            (now(), discovered, new, dup, error, status, run_id, *uargs),
        )


def recent_search_runs(limit: int = 15, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM search_runs WHERE {uw} ORDER BY id DESC LIMIT ?", (*uargs, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def last_run_per_source(ctx=None) -> dict:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        rows = con.execute(
            f"""SELECT r.* FROM search_runs r
               JOIN (SELECT source, MAX(id) AS m FROM search_runs WHERE {uw}
                     GROUP BY source) x
                 ON r.id = x.m""", uargs
        ).fetchall()
    return {
        r["source"]: {
            "status": r["status"],
            "new_count": r["new_count"],
            "error": r["error"],
            "ago": time_ago(r["finished_at"] or r["started_at"]),
        }
        for r in rows
    }


# ------------------------------------------------- cache AI (Faza 3B)

def ai_cache_get(key: str):
    with db() as con:
        row = con.execute("SELECT * FROM ai_cache WHERE cache_key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        output = json.loads(row["output"])
        meta = json.loads(row["meta"] or "{}")
    except (ValueError, TypeError):
        return None
    return {"output": output, "meta": meta, "created_at": row["created_at"]}


def ai_cache_put(key: str, task: str, provider: str, model: str,
                 prompt_version: str, output: dict, meta: dict) -> None:
    with db() as con:
        con.execute(
            """INSERT INTO ai_cache (cache_key, task, provider, model, prompt_version,
                                     output, meta, created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET output=excluded.output,
                   meta=excluded.meta, created_at=excluded.created_at""",
            (key, task, provider, model, prompt_version,
             json.dumps(output, ensure_ascii=False),
             json.dumps(meta, ensure_ascii=False), now()),
        )


def ai_cache_count() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM ai_cache").fetchone()[0]


# --------------------------------- seksionet e profilit (Faza 4C)

def _seed_profile_sections(con) -> int:
    """Kopjon fushat legacy të profilit në seksione strukturore (pa i fshirë
    ato — konsumatorët legacy vazhdojnë të punojnë). Idempotent: s'shkruan
    mbi seksione që ekzistojnë."""
    row = con.execute("SELECT skills, languages, products FROM profile WHERE id=1").fetchone()
    if not row:
        return 0
    skills = json.loads(row["skills"] or "[]")
    languages = json.loads(row["languages"] or "[]")
    products = json.loads(row["products"] or "[]")
    ts = now()
    n = 0
    def put(section, data):
        nonlocal n
        exists = con.execute(
            "SELECT 1 FROM profile_sections WHERE user_id=1 AND section=?", (section,)
        ).fetchone()
        if not exists:
            con.execute(
                "INSERT INTO profile_sections (user_id, profile_id, section, data, updated_at) VALUES (1,1,?,?,?)",
                (section, json.dumps(data, ensure_ascii=False), ts),
            )
            n += 1
    put("skills", [{"name": str(s), "level": ""} for s in skills])
    # "Gjuhë – nivel" → {name, level}
    lang_entries = []
    for entry in languages:
        parts = re.split(r"[–—-]", str(entry), maxsplit=1)
        lang_entries.append({"name": parts[0].strip(), "level": parts[1].strip() if len(parts) > 1 else ""})
    put("languages", lang_entries)
    put("products", [{"name": str(p), "kind": "", "capabilities": [], "link": ""} for p in products])
    return n


def get_profile_section(section: str, ctx=None):
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        row = con.execute("SELECT data, updated_at FROM profile_sections WHERE user_id=? AND section=?",
                          (uid, section)).fetchone()
    if not row:
        return [], None
    try:
        return json.loads(row["data"] or "[]"), row["updated_at"]
    except (ValueError, TypeError):
        return [], None


def set_profile_section(section: str, data: list, ctx=None) -> None:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        con.execute(
            """INSERT INTO profile_sections (user_id, profile_id, section, data, updated_at)
               VALUES (?, 1, ?, ?, ?)
               ON CONFLICT(user_id, section) DO UPDATE SET data=excluded.data,
                   updated_at=excluded.updated_at""",
            (uid, section, json.dumps(data, ensure_ascii=False), now()),
        )
        if section in ('skills','languages','products','experience'):
            con.execute('INSERT OR IGNORE INTO profile(user_id) VALUES (?)',(uid,))
            if section=='experience':
                value='\n'.join(' | '.join(str(e.get(k) or '') for k in ('title','org','period','description')).strip(' |') for e in data)
            else:
                names=[' – '.join([e.get('name',''),e.get('level','')]).strip(' –') if section=='languages' else e.get('name','') for e in data]
                value=json.dumps(names,ensure_ascii=False)
            con.execute(f'UPDATE profile SET {section}=? WHERE user_id=?',(value,uid))
    # OE-007E: scope sipas seksionit — shumë seksione s'pikohen fare.
    return rescore_scope_for_sections([section])


def all_profile_sections(ctx=None) -> dict:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        rows = con.execute("SELECT section, data, updated_at FROM profile_sections WHERE user_id=?",
                           (uid,)).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["section"]] = {"data": json.loads(r["data"] or "[]"), "updated_at": r["updated_at"]}
        except (ValueError, TypeError):
            out[r["section"]] = {"data": [], "updated_at": r["updated_at"]}
    return out


def save_cv(filename: str, stored_path: str, mime: str, size_bytes: int, sha256: str,
            ctx=None) -> int:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        existing=con.execute('SELECT id,sha256,size_bytes,stored_path FROM profile_cv WHERE user_id=?',(uid,)).fetchone()
        if existing and existing['sha256']==sha256 and existing['size_bytes']==size_bytes and Path(existing['stored_path']).suffix.lower()==Path(stored_path).suffix.lower():
            return existing['id']
        # një CV aktiv për user — i riu zëvendëson metadatën (file-i vjetër mbetet në disk)
        con.execute("DELETE FROM profile_cv WHERE user_id=?", (uid,))
        cur = con.execute(
            """INSERT INTO profile_cv (user_id, profile_id, filename, stored_path, mime,
               size_bytes, sha256, parse_status, uploaded_at) VALUES (?,1,?,?,?,?,?, 'pending', ?)""",
            (uid, filename, stored_path, mime, size_bytes, sha256, now()),
        )
        return cur.lastrowid


def get_cv(ctx=None):
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        row = con.execute("SELECT * FROM profile_cv WHERE user_id=? ORDER BY id DESC LIMIT 1",
                          (uid,)).fetchone()
    return dict(row) if row else None


def get_cv_by_id(cv_id: int, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(f"SELECT * FROM profile_cv WHERE id=? AND {uw}",
                          (cv_id, *uargs)).fetchone()
    return dict(row) if row else None


def update_cv_extract(cv_id: int, fields: dict, ctx=None) -> bool:
    """Përditëson rezultatet e ekstraktimit të CV-së — vetëm nëse CV i përket aktorit."""
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    allowed = {"extract_status", "extract_method", "parser_version", "extracted_text",
               "extracted_at", "extract_error", "parse_status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return False
    vals.extend([cv_id, *uargs])
    with db() as con:
        cur = con.execute(f"UPDATE profile_cv SET {', '.join(sets)} WHERE id=? AND {uw}", vals)
        return cur.rowcount > 0


def update_profile_fields(fields: dict, ctx=None) -> None:
    """Plotëson fusha bosh të profilit të aktorit (merge nga CV — manual fiton)."""
    actor = _actor(ctx)
    uid = _uid(actor)
    allowed = {"name", "title", "location", "email", "phone", "summary"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(uid)
    with db() as con:
        con.execute("INSERT OR IGNORE INTO profile (user_id) VALUES (?)", (uid,))
        con.execute(f"UPDATE profile SET {', '.join(sets)} WHERE user_id=?", vals)


def delete_cv(ctx=None) -> bool:
    actor = _actor(ctx)
    uid = _uid(actor)
    with db() as con:
        cur = con.execute("DELETE FROM profile_cv WHERE user_id=?", (uid,))
        return cur.rowcount > 0


# ------------------------------ katalogu i produkteve (Faza 5C)

def _norm(v: str) -> str:
    import re
    return re.sub(r"\s+", " ", (v or "").lower()).strip()


def get_local_prefs(ctx=None) -> dict:
    """Preferencat lokale si dict (seksioni 'local', objekt i vetëm)."""
    data, _ = get_profile_section("local", ctx=ctx)
    import profile_preferences
    statements,_=get_profile_section('preferences', ctx=ctx)
    values=profile_preferences.local_values(statements)
    if data and isinstance(data[0], dict):
        values.update({k:v for k,v in data[0].items() if v not in ('',None)})
    return values


def local_feed_urls():
    actor=_actor(None);uid=_uid(actor)
    with db() as con:
        row=con.execute('SELECT value FROM settings WHERE key=?',(f'u{uid}:local_feed_urls',)).fetchone()
    return row['value'] if row else (get_setting('local_feed_urls','') if uid==1 else '')


# ------------------------------ multi-user helpers (Faza 8B)

def get_user(uid: int):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    return d


def get_user_by_email(email: str):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT id, email, username, display_name, status, created_at, last_login_at FROM users ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def create_user(email: str, password_hash: str, display_name: str = "",
                username: str = "") -> int:
    ts = now()
    with db() as con:
        cur = con.execute(
            """INSERT INTO users (email, username, password_hash, display_name,
               status, created_at, updated_at)
               VALUES (?,?,?,?, 'active', ?, ?)""",
            (email, username or email.split("@")[0], password_hash,
             display_name, ts, ts))
        return cur.lastrowid


def set_user_status(uid: int, status: str) -> bool:
    with db() as con:
        cur = con.execute(
            "UPDATE users SET status=?, updated_at=datetime('now') WHERE id=?",
            (status, uid))
        return cur.rowcount > 0


def get_tenant(tid: int):
    with db() as con:
        row = con.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else None


def get_default_tenant_id() -> int:
    with db() as con:
        return con.execute("SELECT id FROM tenants WHERE slug='default'").fetchone()[0]


def list_products(active_only: bool = False, ctx=None) -> list[dict]:
    """Produkte nga seksioni strukturuar 'products' i aktorit; fushat e vjetra
    (name/kind/capabilities/link) mbeten të lexueshme."""
    entries, _ = get_profile_section("products", ctx)
    out = []
    for i, e in enumerate(entries):
        e = dict(e)
        e.setdefault("status", "active")
        e.setdefault("type", e.get("kind", ""))
        e.setdefault("capabilities", [])
        e.setdefault("priority", i)
        e["idx"] = i
        if active_only and e["status"] != "active":
            continue
        out.append(e)
    return out


def save_products(entries: list, ctx=None) -> None:
    set_profile_section("products", entries, ctx)


def reorder_product(idx: int, direction: str, ctx=None) -> bool:
    entries, _ = get_profile_section("products", ctx)
    j = idx - 1 if direction == "up" else idx + 1
    if idx < 0 or idx >= len(entries) or j < 0 or j >= len(entries):
        return False
    entries[idx], entries[j] = entries[j], entries[idx]
    save_products(entries, ctx)
    return True


# --------------------------------- schedule + njoftime (Faza 7A)

def get_schedule(profile_id: int, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(f"SELECT * FROM schedule_configs WHERE profile_id=? AND {uw}",
                          (profile_id, *uargs)).fetchone()
    if not row:
        return None
    d = dict(row)
    for col in ("kinds", "sources"):
        try:
            d[col] = json.loads(d.get(col) or "[]")
        except (ValueError, TypeError):
            d[col] = []
    return d


def upsert_schedule(profile_id: int, fields: dict, ctx=None) -> None:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    uid, _tid = _stamp(actor)
    allowed = {"enabled", "cadence", "timezone", "run_window_time", "min_match",
               "notify_new_only", "kinds", "sources", "last_run_at", "next_run_at",
               "last_success_at", "consecutive_failures", "lease_until"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            if k in ("kinds", "sources") and isinstance(v, (list, tuple)):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
    ts = now()
    with db() as con:
        exists = con.execute(f"SELECT 1 FROM schedule_configs WHERE profile_id=? AND {uw}",
                             (profile_id, *uargs)).fetchone()
        if exists:
            if sets:
                con.execute(f"UPDATE schedule_configs SET {', '.join(sets)}, updated_at=? WHERE profile_id=? AND {uw}",
                            vals + [ts, profile_id, *uargs])
        else:
            try:
                con.execute(
                    """INSERT INTO schedule_configs (profile_id, enabled, cadence, timezone,
                       run_window_time, min_match, created_at, updated_at, user_id)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (profile_id, 1, "daily", "Europe/Berlin", "08:00", 0, ts, ts, uid))
            except sqlite3.IntegrityError:
                return  # rrjesht i pronarit tjetër — pa mbishkrim (8C2)
            # rikthe setet e mbetura (upsert i dytë kur erdhi i ri)
            if sets:
                con.execute(f"UPDATE schedule_configs SET {', '.join(sets)}, updated_at=? WHERE profile_id=? AND {uw}",
                            vals + [ts, profile_id, *uargs])


def claim_schedule(profile_id: int, expected_next: str, new_next: str,
                   lease_minutes: int = 30, ctx=None) -> bool:
    """CAS: avancON next_run_at vetëm nëse është ende vlera e pritur —
    pamundëson dy ekzekutime të të njëjtës occasion (restart/worker)."""
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        cur = con.execute(
            f"""UPDATE schedule_configs SET next_run_at=?, lease_until=?, updated_at=?
               WHERE profile_id=? AND next_run_at=? AND {uw}""",
            (new_next, now(), now(), profile_id, expected_next, *uargs))
        return cur.rowcount > 0


def finish_schedule_run(profile_id: int, success: bool, last_run_at: str,
                        last_success_at: str | None = None, ctx=None) -> None:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        if success:
            con.execute(
                f"""UPDATE schedule_configs SET last_run_at=?, last_success_at=?,
                   consecutive_failures=0, updated_at=? WHERE profile_id=? AND {uw}""",
                (last_run_at, last_success_at, now(), profile_id, *uargs))
        else:
            con.execute(
                f"""UPDATE schedule_configs SET last_run_at=?,
                   consecutive_failures=consecutive_failures+1, updated_at=? WHERE profile_id=? AND {uw}""",
                (last_run_at, now(), profile_id, *uargs))


def list_schedules(ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "s.user_id")
    with db() as con:
        rows = con.execute(
            f"""SELECT s.*, p.name AS profile_name, p.kinds AS profile_kinds,
                      p.sources AS profile_sources
               FROM schedule_configs s JOIN search_profiles p ON p.id = s.profile_id
               WHERE {uw}
               ORDER BY s.profile_id""", uargs).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for col in ("kinds", "sources", "profile_kinds", "profile_sources"):
            try:
                d[col] = json.loads(d.get(col) or "[]")
            except (ValueError, TypeError):
                d[col] = []
        out.append(d)
    return out


def create_notification(profile_id, opportunity_id, ntype, title, summary,
                        dedup_key, metadata=None, ctx=None) -> int | None:
    """INSERT OR IGNORE — deduplikim i njoftimeve në nivel DB. Njoftimi
    i takon GJITHMONë aktorit (user_id/tenant_id nga konteksti)."""
    actor = _actor(ctx)
    uid, tid = _stamp(actor)
    with db() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO notifications (profile_id, opportunity_id, type,
               title, summary, dedup_key, metadata, created_at, user_id, tenant_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (profile_id, opportunity_id, ntype, title, summary, dedup_key,
             json.dumps(metadata or {}, ensure_ascii=False), now(), uid, tid),
        )
        return cur.lastrowid if cur.rowcount else None


def list_notifications(limit: int = 30, unread_only: bool = False,
                       user_id: int | None = None, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    if not actor.is_system:
        user_id = actor.user_id  # forco pronarin nga konteksti
    where = "WHERE dismissed_at IS NULL"
    args: list = []
    if unread_only:
        where += " AND read_at IS NULL"
    if user_id is not None:
        where += " AND user_id = ?"
        args.append(user_id)
    with db() as con:
        rows = con.execute(
            f"""SELECT id, profile_id, opportunity_id, type, title, summary,
                dedup_key, created_at, read_at, dismissed_at, metadata
                FROM notifications {where} ORDER BY id DESC LIMIT ?""",
            args + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["metadata"] = json.loads(d["metadata"] or "{}")
        except (ValueError, TypeError):
            d["metadata"] = {}
        out.append(d)
    return out


def unread_notification_count(ctx=None) -> int:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        return con.execute(
            f"SELECT COUNT(*) FROM notifications WHERE read_at IS NULL "
            f"AND dismissed_at IS NULL AND {uw}", uargs
        ).fetchone()[0]


def mark_notification_read(nid: int, user_id: int | None = None, ctx=None) -> bool:
    actor = _actor(ctx)
    if not actor.is_system:
        user_id = actor.user_id
    where = "id=? AND read_at IS NULL"
    args: list = [now(), nid]
    if user_id is not None:
        where += " AND user_id = ?"
        args.append(user_id)
    with db() as con:
        cur = con.execute(
            f"UPDATE notifications SET read_at=? WHERE {where}", args)
        return cur.rowcount > 0


def dismiss_notification(nid: int, user_id: int | None = None, ctx=None) -> bool:
    actor = _actor(ctx)
    if not actor.is_system:
        user_id = actor.user_id
    where = "id=?"
    args: list = [now(), now(), nid]
    if user_id is not None:
        where += " AND user_id = ?"
        args.append(user_id)
    with db() as con:
        cur = con.execute(
            f"UPDATE notifications SET dismissed_at=?, read_at=COALESCE(read_at, ?) WHERE {where}",
            args)
        return cur.rowcount > 0


def mark_all_notifications_read(ctx=None) -> int:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        cur = con.execute(
            f"UPDATE notifications SET read_at=? WHERE read_at IS NULL "
            f"AND dismissed_at IS NULL AND {uw}", (now(), *uargs))
        return cur.rowcount


# ------------------------------ email deliveries + digest (Faza 7B)

def email_delivery_seen(dedup_key: str, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        return con.execute(f"SELECT 1 FROM email_deliveries WHERE dedup_key=? AND {uw}",
                           (dedup_key, *uargs)).fetchone() is not None


def record_email_delivery(dedup_key: str, status: str, error: str = "",
                          recipient: str = "", ctx=None) -> None:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    with db() as con:
        con.execute(
            """INSERT INTO email_deliveries (dedup_key, channel, status, error,
               attempts, recipient, created_at, sent_at, user_id)
               VALUES (?, 'email', ?, ?, 1, ?, ?, ?, ?)
               ON CONFLICT(dedup_key) DO UPDATE SET status=excluded.status,
                   error=excluded.error, attempts=excluded.attempts+1,
                   sent_at=excluded.sent_at""",
            (dedup_key, status, error, recipient, now(),
             now() if status == "sent" else None, uid),
        )


def record_digest_run(since_at: str, items: int, recipient: str,
                      status: str = "ok", error: str = "", ctx=None) -> None:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    with db() as con:
        con.execute(
            """INSERT INTO digest_runs (sent_at, since_at, items_count, recipient,
               status, error, user_id) VALUES (?,?,?,?,?,?,?)""",
            (now(), since_at, items, recipient, status, error, uid))


def last_digest(ctx=None) -> dict:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(
            f"SELECT * FROM digest_runs WHERE {uw} ORDER BY id DESC LIMIT 1", uargs).fetchone()
    return dict(row) if row else None


# --------------------------------- cv sugjerime (Faza 4D)

def create_cv_suggestion(cv_id, section, field, proposed, normalized, evidence,
                         confidence, source_ref, ctx=None) -> int | None:
    """INSERT OR IGNORE — i njëjti (cv, section, field, normalized) s'dyfishohet.
    Kthen id ose None nëse ekziston. I takon aktorit (user_id)."""
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    with db() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO cv_suggestions (cv_id, section, field,
               proposed_value, normalized_value, evidence, confidence, status,
               source_ref, created_at, user_id)
               SELECT ?,?,?,?,?,?,?, 'pending', ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM profile_cv WHERE id=? AND user_id=?)""",
            (cv_id, section, field, proposed, normalized, evidence, confidence,
             source_ref, now(), uid, cv_id, uid),
        )
        return cur.lastrowid if cur.rowcount else None


def cv_suggestions(cv_id: int, status: str | None = None, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    where = "WHERE cv_id=? AND " + uw + (" AND status=?" if status else "")
    args = [cv_id, *uargs] + ([status] if status else [])
    with db() as con:
        rows = con.execute(
            f"""SELECT * FROM cv_suggestions {where} ORDER BY section, id""",
            args).fetchall()
    return [dict(r) for r in rows]


def all_cv_suggestions(status: str | None = None, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    where = "WHERE " + uw + (" AND status=?" if status else "")
    args = [*uargs] + ([status] if status else [])
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM cv_suggestions {where} ORDER BY section, id",
            args).fetchall()
    return [dict(r) for r in rows]


def get_cv_suggestion(sid: int, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(f"SELECT * FROM cv_suggestions WHERE id=? AND {uw}",
                          (sid, *uargs)).fetchone()
    return dict(row) if row else None


ACCEPT_SECTION_MAP = {
    "interests": "interests", "personal": "personal", "preferences": "preferences",
    "skills": "skills", "languages": "languages", "experience": "experience",
    "education": "education", "certifications": "certifications",
    "portfolio": "portfolio", "projects": "projects", "products": "products",
}


def cv_suggestions_accepted_entries(ctx=None) -> dict:
    """Zërat e strukturuar nga sugjerimet e CV-së të APROVUARA të aktorit,
    të grupuar sipas seksionit — me provenancë cv:<cv_id>:<sid> (Faza 4D)."""
    out: dict = {}
    for s in all_cv_suggestions("accepted", ctx):
        target = ACCEPT_SECTION_MAP.get(s["section"])
        if not target:
            continue
        entry = _entry_for_cv_section(s)
        out.setdefault(target, []).append(entry)
    return out


def _entry_for_cv_section(s: dict) -> dict:
    value = s["proposed_value"]
    provenance = f"cv:{s['cv_id']}:{s['id']}"
    entry = {"source": provenance}
    section = s["section"]
    if s.get('field') == 'entry':
        import cv_ai
        parsed = json.loads(value)
        allowed = cv_ai.ENTRY_FIELDS.get(section, ())
        entry.update({k: v for k, v in parsed.items() if k in allowed and isinstance(v, str)})
        return entry
    if section == "skills":
        entry.update({"name": value, "level": s.get("field") and "" or ""})
    elif section == "languages":
        import re
        m = re.match(r"(.+?)\s*[–—-]\s*(.+)$", value)
        if m:
            entry.update({"name": m.group(1).strip(), "level": m.group(2).strip()})
        else:
            entry.update({"name": value, "level": ""})
    elif section == "experience":
        entry.update({"title": value.split(" @ ")[0],
                      "org": value.split(" @ ")[-1] if " @ " in value else "",
                      "period": ""})
    elif section == "education":
        entry.update({"title": value, "org": "", "period": ""})
    elif section == "certifications":
        entry.update({"name": value, "org": "", "year": ""})
    elif section in ("portfolio", "projects"):
        entry.update({"title": value,
                      "url": value if value.startswith("http") else ""})
    elif section == "products":
        entry.update({"name": value, "kind": "", "capabilities": []})
    return entry


def set_cv_suggestion_status(sid: int, status: str, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        cur = con.execute(
            f"UPDATE cv_suggestions SET status=?, reviewed_at=? WHERE id=? AND {uw}",
            (status, now(), sid, *uargs))
        return cur.rowcount > 0


# ------------------------------ gaps + sugjerime profili (Faza 6)

GAP_CATEGORIES = ("missing_skill", "certification_missing", "language_gap",
                  "experience_gap", "education_gap", "licence_missing",
                  "availability_constraint", "travel_constraint",
                  "work_mode_constraint", "other")
GAP_SEVERITIES = ("info", "soft", "hard")
GAP_WILLINGNESS = ("learning", "willing_to_learn", "willing_to_obtain",
                   "unavailable", "not_interested", "")


def list_gaps(active_only: bool = False, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    where = f"WHERE active=1 AND {uw}" if active_only else f"WHERE {uw}"
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM profile_gaps {where} ORDER BY severity DESC, id", uargs).fetchall()
    return [dict(r) for r in rows]


def create_gap(g: dict, ctx=None) -> int:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    ts = now()
    with db() as con:
        cur = con.execute(
            """INSERT INTO profile_gaps (category, label, current_state, target_state,
               severity, willingness, notes, evidence, active, created_at, updated_at, user_id)
               VALUES (?,?,?,?,?,?,?,?,1,?,?,?)""",
            (g["category"], g["label"], g.get("current_state", ""),
             g.get("target_state", ""), g.get("severity", "soft"),
             g.get("willingness", ""), g.get("notes", ""), g.get("evidence", ""),
             ts, ts, uid))
        return cur.lastrowid


def update_gap(gid: int, fields: dict, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    allowed = {"category", "label", "current_state", "target_state", "severity",
               "willingness", "notes", "evidence", "active"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.extend([now(), gid, *uargs])
    with db() as con:
        cur = con.execute(
            f"UPDATE profile_gaps SET {', '.join(sets)} WHERE id=? AND {uw}", vals)
        return cur.rowcount > 0


def delete_gap(gid: int, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        cur = con.execute(f"DELETE FROM profile_gaps WHERE id=? AND {uw}", (gid, *uargs))
        return cur.rowcount > 0


def get_suggestions(section: str | None = None, status: str | None = None,
                    ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    where = [uw]
    args = [*uargs]
    if section:
        where.append("section=?"); args.append(section)
    if status:
        where.append("status=?"); args.append(status)
    sql = "SELECT * FROM profile_suggestions WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT 100"
    with db() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def create_profile_suggestion(section: str, kind: str, value: str, reason: str = "",
                              ctx=None) -> int | None:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    norm = (value or "").lower().strip()
    if not norm:
        return None
    with db() as con:
        cur = con.execute(
            """INSERT OR IGNORE INTO profile_suggestions (section, kind, value,
               normalized_value, reason, status, created_at, user_id)
               VALUES (?,?,?,?,?,'pending',?,?)""",
            (section, kind, value.strip(), norm, reason, now(), uid))
        return cur.lastrowid if cur.rowcount else None


def confirm_profile_suggestion(sid: int, ctx=None) -> bool:
    """Konfirmo sugjerimin E AKTORIT → bëhet fakte profili (seksioni përkatës)."""
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(f"SELECT * FROM profile_suggestions WHERE id=? AND {uw}",
                          (sid, *uargs)).fetchone()
        if not row:
            return False
        s = dict(row)
        if s["status"] == "accepted":
            return True
    section = s["section"]
    value = s["value"]
    data, _ = get_profile_section(section, actor)
    if section == "skills":
        if not any(_norm(e.get("name")) == s["normalized_value"] for e in data):
            data.append({"name": value, "level": "",
                         "source": f"confirmed-suggestion:{sid}"})
            set_profile_section("skills", data, actor)
    elif section == "languages":
        import re
        m = re.match(r"(.+?)\s*[–—-]\s*(.+)$", value)
        name = m.group(1).strip() if m else value
        level = m.group(2).strip() if m else ""
        if not any(_norm(e.get("name")) == name.lower() for e in data):
            data.append({"name": name, "level": level,
                         "source": f"confirmed-suggestion:{sid}"})
            set_profile_section("languages", data, actor)
    elif section == "certifications":
        if not any(s["normalized_value"] in _norm(e.get("name", "")) for e in data):
            data.append({"name": value, "org": "", "year": "",
                         "source": f"confirmed-suggestion:{sid}"})
            set_profile_section("certifications", data, actor)
    with db() as con:
        con.execute(f"UPDATE profile_suggestions SET status='accepted', reviewed_at=? WHERE id=? AND {uw}",
                    (now(), sid, *uargs))
    return True


# --------------------------------------- aplikimet (Faza 4A)

def create_application(opp_id: int, app_type: str, platform: str, language: str,
                       subject: str, content: str, generator: dict, ctx=None) -> int:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    ts = now()
    with db() as con:
        cur = con.execute(
            """INSERT INTO applications (opportunity_id, application_type, platform,
               language, subject, status, latest_version, generator, created_at, updated_at, user_id)
               VALUES (?,?,?,?,?,'draft',1,?,?,?,?)""",
            (opp_id, app_type, platform, language, subject,
             json.dumps(generator, ensure_ascii=False), ts, ts, uid),
        )
        app_id = cur.lastrowid
        con.execute(
            """INSERT INTO application_versions (application_id, version_number, kind,
               content, created_at) VALUES (?,?,?,?,?)""",
            (app_id, 1, "generated", content, ts),
        )
        return app_id


def get_application(app_id: int, with_versions: bool = False, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(f"SELECT * FROM applications WHERE id=? AND {uw}",
                          (app_id, *uargs)).fetchone()
        if not row:
            return None
        app = dict(row)
        app["generator"] = json.loads(app["generator"] or "{}")
        if with_versions:
            vers = con.execute(
                """SELECT id, version_number, kind, content, created_at
                   FROM application_versions WHERE application_id=? ORDER BY version_number""",
                (app_id,),
            ).fetchall()
            app["versions"] = [dict(v) for v in vers]
            latest = con.execute(
                """SELECT kind, content FROM application_versions
                   WHERE application_id=? ORDER BY version_number DESC LIMIT 1""",
                (app_id,),
            ).fetchone()
            app["latest_kind"] = latest["kind"]
            app["latest_content"] = latest["content"]
        return app


def list_applications(opp_id: int, user_id: int | None = None, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    if not actor.is_system:
        user_id = actor.user_id
    with db() as con:
        if user_id is not None:
            rows = con.execute(
                """SELECT a.id, a.application_type, a.platform, a.language, a.subject,
                   a.status, a.latest_version, a.created_at, a.updated_at,
                   a.approved_at, a.submitted_at
                   FROM applications a
                   JOIN opportunities o ON o.id = a.opportunity_id
                   WHERE a.opportunity_id=? AND o.user_id=?
                   ORDER BY a.id DESC""", (opp_id, user_id)).fetchall()
        else:
            rows = con.execute(
                """SELECT id, application_type, platform, language, subject, status,
                   latest_version, created_at, updated_at, approved_at, submitted_at
                   FROM applications WHERE opportunity_id=? ORDER BY id DESC""",
                (opp_id,)).fetchall()
    return [dict(r) for r in rows]


def add_application_version(app_id: int, kind: str, content: str, ctx=None) -> int:
    """Shton version të re (append-only) — VETËM nëse aplikimi i përket
    aktorit (INSERT-i bëhet me SELECT të shtrënguar mbi pronarin).
    Kthen numrin e versionit; LookupError kur s'gjendet/pa të drejta."""
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    ts = now()
    with db() as con:
        owned = con.execute(f"SELECT 1 FROM applications WHERE id=? AND {uw}",
                            (app_id, *uargs)).fetchone()
        if not owned:
            raise LookupError("aplikimi nuk u gjet për aktorin")
        ver = con.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM application_versions WHERE application_id=?",
            (app_id,),
        ).fetchone()[0]
        con.execute(
            """INSERT INTO application_versions (application_id, version_number, kind,
               content, created_at) VALUES (?,?,?,?,?)""",
            (app_id, ver, kind, content, ts),
        )
        con.execute(
            "UPDATE applications SET latest_version=?, updated_at=? WHERE id=?",
            (ver, ts, app_id),
        )
        return ver


def set_application_fields(app_id: int, fields: dict, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    allowed = {"status", "subject", "approved_at", "submitted_at", "platform", "language",
               "generator"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            if k == "generator" and isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.extend([now(), app_id, *uargs])
    with db() as con:
        cur = con.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id=? AND {uw}", vals)
        return cur.rowcount > 0


def delete_application(app_id: int, ctx=None) -> bool:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        cur = con.execute(f"DELETE FROM applications WHERE id=? AND {uw}", (app_id, *uargs))
        return cur.rowcount > 0


# -------------------------------------------- eval runs (Faza 3C)

def create_eval_run(label, task, provider, model, base_url, seed, sample_size,
                    opportunity_ids, max_calls, max_seconds, ctx=None) -> int:
    actor = _actor(ctx)
    uid, _tid = _stamp(actor)
    with db() as con:
        cur = con.execute(
            """INSERT INTO eval_runs (label, task, provider, model, base_url, seed,
               sample_size, opportunity_ids, max_calls, max_seconds, started_at, status, user_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'running', ?)""",
            (label, task, provider, model, base_url, seed, sample_size,
             json.dumps(opportunity_ids), max_calls, max_seconds, now(), uid),
        )
        return cur.lastrowid


def finish_eval_run(run_id: int, ctx=None, **fields) -> None:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    sets, vals = ["finished_at=?"], [now()]
    for k, v in fields.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.extend([run_id, *uargs])
    with db() as con:
        con.execute(f"UPDATE eval_runs SET {', '.join(sets)} WHERE id=? AND {uw}", vals)


def add_eval_item(run_id: int, opportunity_id: int, seq: int, status: str,
                  error: str = "", cached: int = 0, latency_ms: int = 0,
                  usage_tokens: int = 0, output: dict | None = None,
                  meta: dict | None = None) -> None:
    with db() as con:
        con.execute(
            """INSERT INTO eval_items (run_id, opportunity_id, seq, status, error,
               cached, latency_ms, usage_tokens, output, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, opportunity_id, seq, status, error, cached, latency_ms,
             usage_tokens, json.dumps(output or {}, ensure_ascii=False),
             json.dumps(meta or {}, ensure_ascii=False)),
        )


def get_eval_run(run_id: int, with_items: bool = False, ctx=None):
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        row = con.execute(f"SELECT * FROM eval_runs WHERE id=? AND {uw}",
                          (run_id, *uargs)).fetchone()
        if not row:
            return None
        run = dict(row)
        run["opportunity_ids"] = json.loads(run["opportunity_ids"] or "[]")
        if with_items:
            items = con.execute(
                "SELECT * FROM eval_items WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
            run["items"] = []
            for it in items:
                d = dict(it)
                for jf in ("output", "meta", "human_ext"):
                    try:
                        d[jf] = json.loads(d[jf] or "{}")
                    except (ValueError, TypeError):
                        d[jf] = {}
                run["items"].append(d)
        return run


def list_eval_runs(limit: int = 50, ctx=None) -> list[dict]:
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor)
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM eval_runs WHERE {uw} ORDER BY id DESC LIMIT ?", (*uargs, limit)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["opportunity_ids"] = json.loads(d["opportunity_ids"] or "[]")
        out.append(d)
    return out


def set_eval_item_review(item_id: int, rating: int | None, notes: str,
                         ext: dict | None = None, ctx=None) -> bool:
    """Review i VETËM për item-in e një run-i që i përket aktorit (JOIN)."""
    actor = _actor(ctx)
    uw, uargs = _uwhere(actor, "r.user_id")
    with db() as con:
        owned = con.execute(
            f"SELECT 1 FROM eval_items it JOIN eval_runs r ON r.id = it.run_id "
            f"WHERE it.id=? AND {uw}", (item_id, *uargs)).fetchone()
        if not owned:
            return False
        if ext is not None:
            row = con.execute("SELECT human_ext FROM eval_items WHERE id=?",
                              (item_id,)).fetchone()
            if not row:
                return False
            try:
                merged = json.loads(row["human_ext"] or "{}")
            except (ValueError, TypeError):
                merged = {}
            merged.update(ext)
            cur = con.execute(
                """UPDATE eval_items SET human_ext=?, reviewed_at=? WHERE id=?""",
                (json.dumps(merged, ensure_ascii=False), now(), item_id),
            )
            return cur.rowcount > 0
        cur = con.execute(
            """UPDATE eval_items SET human_rating=?, human_notes=?, reviewed_at=?
               WHERE id=?""",
            (rating, notes, now(), item_id),
        )
        return cur.rowcount > 0


def autofill_cv_suggestions(cv_id: int, ctx=None, candidate_ids=None) -> dict:
    """Apply confident pending CV facts atomically, preserving existing values.

    Ownership and status are checked within the write transaction. Conflicts and
    low-confidence items remain pending for individual review.
    """
    actor = _actor(ctx)
    uid = _uid(actor)
    con = db()
    try:
        con.execute('BEGIN IMMEDIATE')
        if not con.execute('SELECT 1 FROM profile_cv WHERE id=? AND user_id=?', (cv_id, uid)).fetchone():
            return {'ok': False, 'reason': 'CV nuk u gjet'}
        con.execute('INSERT OR IGNORE INTO profile (user_id) VALUES (?)', (uid,))
        prof = dict(con.execute('SELECT * FROM profile WHERE user_id=?', (uid,)).fetchone())
        rows = [dict(r) for r in con.execute("SELECT * FROM cv_suggestions WHERE cv_id=? AND user_id=? AND status='pending' ORDER BY id", (cv_id, uid))]
        if candidate_ids is not None:
            rows = [s for s in rows if s['id'] in set(candidate_ids)]
        sections = {r['section']: json.loads(r['data']) for r in con.execute('SELECT section,data FROM profile_sections WHERE user_id=?', (uid,))}
        # Creating a structured section must not hide older manually entered facts.
        for section in ('skills', 'languages'):
            if not sections.get(section):
                sections[section] = []
                for value in json.loads(prof[section] or '[]'):
                    entry = _entry_for_cv_section({'section': section, 'proposed_value': value, 'cv_id': 0, 'id': 0})
                    entry['source'] = 'manual'
                    sections[section].append(entry)
        scalar_allowed = {'name', 'title', 'email', 'phone', 'location', 'summary'}
        ambiguous = {}
        for s in rows:
            if s['section'] in ('contact', 'summary'):
                key = 'summary' if s['section'] == 'summary' else s['field']
                ambiguous.setdefault(key, set()).add(_norm(s['proposed_value']))
        changed_fields, changed_sections = set(), set()
        merged = duplicates = conflicts = 0
        for s in rows:
            if s['confidence'] < .8:
                continue
            section, value = s['section'], s['proposed_value'].strip()
            changed = False
            if section in ('contact', 'summary'):
                field = 'summary' if section == 'summary' else s['field']
                if field not in scalar_allowed:
                    continue
                if len(ambiguous.get(field, [])) > 1:
                    conflicts += 1; continue
                if str(prof.get(field) or '').strip():
                    if _norm(prof[field]) != _norm(value):
                        conflicts += 1; continue
                else:
                    prof[field] = value; changed_fields.add(field); changed = True
            elif section in ('personal', 'preferences'):
                entry = _entry_for_cv_section(s)
                entries = sections.setdefault(section, [])
                existing = entries[0] if entries else None
                if existing:
                    if any(existing.get(k) and v and _norm(str(existing[k])) != _norm(str(v)) for k,v in entry.items() if k != 'source'):
                        conflicts += 1; continue
                    for k,v in entry.items():
                        if k != 'source' and v and not existing.get(k): existing[k] = v; changed = True
                else:
                    entries.append(entry); changed = True
                if changed: changed_sections.add(section)
            elif section in ACCEPT_SECTION_MAP and section != 'products':
                entry = _entry_for_cv_section(s)
                primary = 'name' if section in ('skills', 'languages', 'certifications', 'interests') else 'title'
                if not entry.get(primary):
                    continue
                entries = sections.setdefault(section, [])
                existing = next((e for e in entries if cv_entry_identity(section, e.get(primary, '')) == cv_entry_identity(section, entry[primary]) and
                    (section not in ('experience', 'education') or not e.get('org') or not entry.get('org') or _norm(e['org']) == _norm(entry['org'])) and
                    (section not in ('experience', 'education') or not e.get('period') or not entry.get('period') or _norm(e['period']) == _norm(entry['period']))), None)
                if existing is not None:
                    if any(existing.get(k) and v and _norm(str(existing[k])) != _norm(str(v)) for k,v in entry.items() if k not in ('source', primary)):
                        conflicts += 1; continue
                    for k,v in entry.items():
                        if k != 'source' and v and not existing.get(k):
                            existing[k] = v; changed = True
                else:
                    entries.append(entry); changed = True
                if changed:
                    changed_sections.add(section)
            else:
                continue
            con.execute("UPDATE cv_suggestions SET status='accepted', reviewed_at=? WHERE id=? AND user_id=?", (now(), s['id'], uid))
            if changed: merged += 1
            else: duplicates += 1
        # Keep legacy summaries used by AI/matching aligned with imported sections.
        for section in ('skills', 'languages'):
            if section not in changed_sections:
                continue
            values = json.loads(prof[section] or '[]')
            for entry in sections[section]:
                value = entry.get('name', '')
                if section == 'languages' and entry.get('level'):
                    value += ' – ' + entry['level']
                if value and _norm(value) not in {_norm(v) for v in values}:
                    values.append(value)
            prof[section] = json.dumps(values, ensure_ascii=False); changed_fields.add(section)
        if 'experience' in changed_sections and not prof['experience'].strip():
            prof['experience'] = '\n'.join(' | '.join(str(e.get(k) or '') for k in ('title','org','period','description')).strip(' |') for e in sections['experience'])
            changed_fields.add('experience')
        for field in changed_fields:
            con.execute(f'UPDATE profile SET {field}=? WHERE user_id=?', (prof[field], uid))
        for section in changed_sections:
            con.execute('INSERT INTO profile_sections (user_id,profile_id,section,data,updated_at) VALUES (?,1,?,?,?) ON CONFLICT(user_id,section) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                        (uid, section, json.dumps(sections[section],ensure_ascii=False), now()))
        pending = con.execute("SELECT COUNT(*) FROM cv_suggestions WHERE cv_id=? AND user_id=? AND status='pending'", (cv_id,uid)).fetchone()[0]
        con.commit()
        return {'ok': True, 'merged': merged, 'duplicates': duplicates, 'conflicts': conflicts, 'pending': pending,
                'fields': sorted(changed_fields), 'sections': sorted(changed_sections)}
    finally:
        con.close()
