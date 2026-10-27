"""Auth service (Faza 8B) — multi-user auth me password hashing + tenant context."""
import hashlib
import secrets

import repository as repo


def _hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    return f"scrypt${salt}${hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()}"


def _verify_pw(pw: str, stored: str) -> bool:
    try:
        if stored.startswith("sha256$"):
            import hashlib as _hl
            expected = stored.split("$", 1)[1]
            return hmac.compare_digest(_hl.sha256(pw.encode()).hexdigest(), expected)
        _, salt, key = stored.split("$")
        expected = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=32).hex()
        return hmac.compare_digest(key, expected)
    except (ValueError, TypeError):
        return False


import hmac


def find_user_by_login(login: str):
    """Rreshti i user-it sipas email ose username (pa password)."""
    with repo.db() as con:
        row = con.execute(
            "SELECT id, email, username, status FROM users WHERE email=? OR username=?",
            (login, login)).fetchone()
    return dict(row) if row else None


def provision_env_user() -> int | None:
    """Provisionon admin-in nga env (AUTH_USERNAME + AUTH_PASSWORD/_SHA256)
    si user REAL në DB — sesionet e production-i mapohen GJITHMONë te një
    rresht user i verifikuar (zero 'default user 1' i fshehur). Idempotent."""
    import config
    username = (config.AUTH_USERNAME or "").strip()
    if not username:
        return None
    existing = find_user_by_login(username)
    if existing:
        return existing["id"]
    sha = config.AUTH_PASSWORD_SHA256 or ""
    plain = config.AUTH_PASSWORD_PLAIN or ""
    if sha:
        pw_hash = f"sha256${sha.strip().lower()}"
    elif plain:
        pw_hash = _hash_pw(plain)
    else:
        return None
    ts = repo.now()
    with repo.db() as con:
        cur = con.execute(
            """INSERT INTO users (email, username, password_hash, display_name,
               status, created_at, updated_at) VALUES (?,?,?,?, 'active', ?, ?)""",
            (username, username, pw_hash, "Admin", ts, ts))
        uid = cur.lastrowid
    import tenancy
    tenancy.add_membership(uid, repo.get_default_tenant_id(), role="owner")
    return uid


def authenticate(email_or_username: str, password: str):
    """Kthen user_id ose None."""
    with repo.db() as con:
        row = con.execute(
            "SELECT id, password_hash, status FROM users WHERE email=? OR username=?",
            (email_or_username, email_or_username)).fetchone()
    if not row or row["status"] != "active":
        return None
    if not _verify_pw(password, row["password_hash"]):
        return None
    return row["id"]


def create_user(email: str, password: str, display_name: str = "",
                tenant_id: int = 1, role: str = "member") -> int:
    pw_hash = _hash_pw(password)
    ts = repo.now()
    with repo.db() as con:
        cur = con.execute(
            """INSERT INTO users (email, username, password_hash, display_name,
               status, created_at, updated_at)
               VALUES (?,?,?,?, 'active', ?, ?)""",
            (email, email.split("@")[0], pw_hash, display_name, ts, ts))
        uid = cur.lastrowid
        con.execute(
            """INSERT INTO tenant_memberships (tenant_id, user_id, role, created_at)
               VALUES (?,?,?,datetime('now'))""", (tenant_id, uid, role))
    return uid


def make_csrf() -> str:
    import secrets
    return secrets.token_urlsafe(24)


def csrf_ok(cookie: str, header: str) -> bool:
    import hmac
    return bool(cookie) and bool(header) and hmac.compare_digest(cookie, header)
