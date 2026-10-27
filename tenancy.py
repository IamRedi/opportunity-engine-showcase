"""Tenancja (Faza 8C2) — vlefshmëria QENDRORE e anëtarësisë tenant.

Çdo ActorContext për të dhëna tenant-owned kalon përmes resolve_actor():
- anëtarësia (user_id, tenant_id) duhet të ekzistojë dhe user-i aktiv
- roli VJEN NGA DB (owner/admin/member) — kurrë nga request
- s'ka rrugë që user-i të deklarojë vetë tenant_id/role nga browser

Eskalimi i rolit është i pamundur: roli lexohet gjithmonë nga
tenant_memberships; ndryshimi kërkon shkrim direkt në DB (jashtë HTTP).
"""
import repository as repo

from context import ActorContext


class TenantMembershipError(PermissionError):
    """User-i s'ka anëtarësi aktive në tenant-in e kërkuar."""


def get_membership(user_id: int, tenant_id: int) -> dict | None:
    with repo.db() as con:
        row = con.execute(
            """SELECT m.tenant_id, m.user_id, m.role, t.status AS tenant_status
               FROM tenant_memberships m JOIN tenants t ON t.id = m.tenant_id
               WHERE m.user_id = ? AND m.tenant_id = ?""",
            (user_id, tenant_id)).fetchone()
    return dict(row) if row else None


def memberships_for_user(user_id: int) -> list[dict]:
    with repo.db() as con:
        rows = con.execute(
            """SELECT m.tenant_id, m.user_id, m.role, t.status AS tenant_status
               FROM tenant_memberships m JOIN tenants t ON t.id = m.tenant_id
               WHERE m.user_id = ? ORDER BY m.tenant_id""",
            (user_id,)).fetchall()
    return [dict(r) for r in rows]


def resolve_actor(user_id: int, tenant_id: int | None = None) -> ActorContext:
    """ActorContext i verifikuar për user-in (dhe tenant-in e zgjedhur).
    tenant_id None → anëtarësia e parë aktive (s'varet kurrë nga request).
    Nuk ka anëtarësi aktive → TenantMembershipError (fail-closed)."""
    ms = memberships_for_user(user_id)
    active = [m for m in ms if m["tenant_status"] == "active"]
    if tenant_id is None:
        if not active:
            raise TenantMembershipError(
                f"user {user_id} s'ka anëtarësi aktive në asnjë tenant")
        chosen = active[0]
    else:
        chosen = next((m for m in active if m["tenant_id"] == int(tenant_id)), None)
        if not chosen:
            raise TenantMembershipError(
                f"user {user_id} s'ka anëtarësi aktive në tenant {tenant_id}")
    return ActorContext(user_id=int(user_id), tenant_id=int(chosen["tenant_id"]),
                        role=str(chosen["role"]), is_authenticated=True)


def ensure_tenant(name: str, slug: str) -> int:
    """Krijo tenant të re (idempotent mbi slug) — vetëm operacione sistemi."""
    ts = repo.now()
    with repo.db() as con:
        row = con.execute("SELECT id FROM tenants WHERE slug=?", (slug,)).fetchone()
        if row:
            return row["id"]
        cur = con.execute(
            "INSERT INTO tenants (name, slug, status, branding, created_at, updated_at) "
            "VALUES (?,?,'active','{}',?,?)", (name, slug, ts, ts))
        return cur.lastrowid


def add_membership(user_id: int, tenant_id: int, role: str = "member") -> None:
    """Shto anëtarësi (idempotent) — vetëm operacione sistemi/provisioning."""
    with repo.db() as con:
        con.execute(
            "INSERT OR IGNORE INTO tenant_memberships (tenant_id, user_id, role, created_at) "
            "VALUES (?,?,?,?)", (tenant_id, user_id, role, repo.now()))
