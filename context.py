"""ActorContext — konteksti kanonik i përdoruesit/tenantit (Faza 8C2).

RREGULLAT E PATHYESHME:
- user_id vjen NGA SESSIoni (verifikuar server-side) — kurrë nga browser.
- tenant_id vjen nga ANËTARËSIA e verifikuar në DB (tenancy.resolve_actor)
  — kurrë nga request/session i manipulueshëm.
- Rrugët e mbrojtura në repository/service lexojnë kontekstin aktiv
  (ContextVar) ose e marrin atë EKSPLIKISHT si parametër.
- FAIL-CLOSED: në production, thirrja e mbrojtur PA kontekst →
  ActorContextRequired (asgjë nuk lexohet/shkruhet).
- Bypass-i sistem EKZISTON VETËM si emër i qartë: system() (migrime/backfill),
  scheduler_context(uid) (cikli i scheduluar per user), dev_context()
  (local/test — zotëruesi i vetëm i mëparshëm).

Sjellja e filtrimit në repository:
- ctx.user_id > 0  → çdo query user-owned filtrohet: user_id = ctx.user_id
                     (+ tenant_id = ctx.tenant_id ku kolona ekziston).
- ctx.user_id == 0 (sistem) → pa filtra — VETËM për operacione të emërta
  të brendshme (migrim, backfill); insertet nën sistem shënohen si zotërues
  legacy (user 1) që të dhënat lokale të mos humbin kurrë.
"""
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

DEFAULT_OWNER_ID = 1
DEFAULT_TENANT_ID = 1


class ActorContextRequired(PermissionError):
    """Production: thirrje e mbrojtur pa ActorContext — fail-closed."""


@dataclass(frozen=True)
class ActorContext:
    user_id: int
    tenant_id: int = DEFAULT_TENANT_ID
    role: str = "member"
    is_authenticated: bool = True

    @property
    def is_system(self) -> bool:
        return self.user_id == 0

    @classmethod
    def system(cls) -> "ActorContext":
        """Kontekst sistem i PLOTË (pa filtra) — vetëm migrime/backfill/
        operacione të emërta të brendshme. Kurrë për rrugë të autentikuara."""
        return cls(user_id=0, tenant_id=0, role="system", is_authenticated=True)

    @classmethod
    def anonymous(cls) -> "ActorContext":
        return cls(user_id=0, tenant_id=0, role="anonymous", is_authenticated=False)


def scheduler_context(user_id: int, tenant_id: int = DEFAULT_TENANT_ID) -> ActorContext:
    """Kontekst i emërtuar për ciklin e scheduluar — shtrëngohet te NJË
    përdorues: kërkimet/njoftime/email-et e atij profili s'prekin të tjerë."""
    return ActorContext(user_id=int(user_id), tenant_id=int(tenant_id),
                        role="scheduler", is_authenticated=True)


def migration_context() -> ActorContext:
    """Kontekst i emërtuar për migrime/backfill — akses i plotë, i ndaluar
    për rrugë HTTP."""
    return ActorContext(user_id=0, tenant_id=0, role="migration",
                        is_authenticated=True)


def dev_context() -> ActorContext:
    """Local/test: zotëruesi legacy (user 1, tenant 1) — e vetmja përjashtim
    pa auth, VETËM kur APP_ENV != production."""
    return ActorContext(user_id=DEFAULT_OWNER_ID, tenant_id=DEFAULT_TENANT_ID,
                        role="owner", is_authenticated=True)


_current: ContextVar[ActorContext | None] = ContextVar("oe_actor", default=None)


def set_current(ctx: ActorContext) -> Token:
    return _current.set(ctx)


def reset_current(token: Token) -> None:
    _current.reset(token)


def current() -> ActorContext | None:
    return _current.get()


@contextmanager
def run_as(ctx: ActorContext):
    """Vendos kontekstin për bllokun — përdor nga scheduler/migrime/skripta."""
    token = set_current(ctx)
    try:
        yield ctx
    finally:
        reset_current(token)


def in_production() -> bool:
    return os.environ.get("APP_ENV", "local") == "production"


def require_context(explicit: "ActorContext | None" = None) -> ActorContext:
    """Zgjidh kontekstin efektiv për thirrje të mbrojtura:
    1) parametri eksplicit; 2) ContextVar; 3) production → GABIM fail-closed;
    4) local/test → system() (kompatibiteti i testeve të vjetra).
    Konteksti ANONYMOUS nuk është bypass — trajtohet si mungesë konteksti."""
    if explicit is not None and explicit.is_authenticated:
        return explicit
    ctx = _current.get()
    if ctx is not None and ctx.is_authenticated:
        return ctx
    if in_production():
        raise ActorContextRequired(
            "ActorContext mungon për operacion të mbrojtur (production)")
    return ActorContext.system()


def for_user_row(row: dict) -> "ActorContext":
    """ActorContext nga rreshti i user-it + anëtarësia e verifikuar.
    Kërkon repo (import me vonesë për të shmangur ciklin)."""
    import tenancy
    return tenancy.resolve_actor(row["id"])
