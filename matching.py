"""Shërbimi i përputhjes/skorimit (spec §14) — i dedikuar, deterministik.

I ndarë nga repository dhe konektorët. Pa AI (Faza 3B e shton si faktorë
opsionalë mbi të njëjtën kontratë).

Kontrata e rezultatit (kanonike):
    final_score   int 0–100
    excluded      bool — përjashtim i fortë deterministic (fjalë të përjashtuara)
    factors[]     {id, label, status: match|mismatch|unknown,
                   value: 0–100 | None, weight: float>0, evidence: str}
    matched[]     terma/fusha që përputhen
    missing[]     terma që mungojnë
    unknown[]     id-të e faktorëve pa të dhëna
    exclusions[]  arsyet e përjashtimit të fortë
    version       versioni i skemës së skorimit

Rregullat:
- UNKNOWN s'llogaritet as pozitive as negative: hiqet nga emëruesi (pesha e
  tij rillogaritet mbi faktorët e njohur).
- MISMATCH llogaritet 0 dhe pesha e tij RRI në emërues (ndëshkon).
- final = Σ(value×weight mbi të njohur) / Σ(weight mbi të njohur), i rrumbullakuar.
- E njëjta hyrje → i njëjti rezultat (funksion i pastër; freskësia varet vetëm
  nga datat e hyrjes dhe çasti i vlerësimit, siç duhet).

Faktori "skills" (v4-relevance, OE-007B):
- v3b normalizonte pikët me 2 × TË GJITHA terma e profilit — profili i gjerë
  ndëshkohej thjesht sepse di shumë gjëra të palidhura (max real ~59/100).
- v4 masi mbulimin e dëshmive përkatëse: value = min(100, pikët / SATURIM) × 100.
  Termat që s'përmenden në mundësi NUK hyjnë as në numërues as në emërues,
  ndaj gjerësia e profilit është e pavlefshme me ndërtim.
- Dëshmia rëndohet si më parë: term në titull = 2 pikë, në përshkrim/tags = 1.
- SATURIM = 8 pikë (4 terma në titull ose 8 në tekst) — pika e mjaftueshmërisë
  ku dëshmia shtesë s'e rrit mbi faktorin; e zgjedhur mbi sasiun e 95-te
  percentil të masës së dëshmive të vëzhguar (p95=6 pikë), jo për të dalë
  me përqindje "më të bukura".
"""
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone

VERSION = "4-relevance"

MATCH, MISMATCH, UNKNOWN = "match", "mismatch", "unknown"

# Faktorët që mund të rillogariten pa lexim përshkrimesh (OE-007E):
# inputet e tyre janë kolona të vogla (titull, vendndodhje, remote_type,
# salary_min, kind_data). Çdo faktor tjetër (p.sh. skills/languages që
# skanojnë tekstin e plotë) → rillogarim i plotë.
PARTIAL_RESCORE_FACTORS = frozenset({"location", "role", "workmode", "salary", "budget"})

# pesha sipas llojit (spec §14.3) — të rregullt, > 0
WEIGHTS = {
    "job": {
        "skills": 2.0, "role": 0.6, "location": 0.6, "workmode": 0.6,
        "salary": 0.6, "languages": 0.6, "experience": 0.3, "freshness": 0.6,
    },
    "freelance": {
        "skills": 2.0, "project_type": 1.0, "budget": 1.0,
        "workmode": 0.6, "availability": 0.3,
    },
    "product_lead": {
        "capability": 2.0, "problem": 1.0, "customization": 0.5,
    },
    "local_job": {
        "distance": 1.0, "pay": 1.0, "category": 0.8,
        "transport": 0.6, "schedule": 0.3, "freshness": 0.5,
    },
}

FACTOR_LABELS = {
    "skills": "Aftësi", "role": "Roli/titulli", "location": "Vendndodhja",
    "workmode": "Remote/hybrid/zyrë", "salary": "Paga", "languages": "Gjuhët",
    "experience": "Eksperienca/senioriteti", "freshness": "Freskësia",
    "project_type": "Lloji i projektit", "budget": "Buxheti/tarifa",
    "availability": "Disponueshmëria", "capability": "Përputhja me produktin",
    "problem": "Rëndësia e problemit", "customization": "Personalizimi i duhur",
    "distance": "Distanca", "pay": "Pagesa", "category": "Kategoria",
    "transport": "Transporti/udhëtimi", "schedule": "Orari",
}

_SENIORITY = ["senior", "junior", "lead", "principal", "staff", "mid", "entry"]
_STOP = {"und", "and", "the", "für", "der", "die", "das", "mwd", "m/f/d", "with"}

# v4-relevance: pika e mjaftueshmërisë për faktorin "skills" — 4 terma në
# titull ose 8 në tekst/përbërës. Mbi këtë prag dëshmia shtesë s'e ngre
# faktorin (saturohet te 100); nën të, vlera është proporcionale me dëshminë.
SKILL_SATURATION_POINTS = 8


# --------------------------------------------- konteksti i përgatitur (007E)

@dataclass(frozen=True)
class PreparedContext:
    """Fakte të profilit/filtrave të normalizuara NJË herë për aktor —
    konsumohet nga çdo score() i kalimit. Pa gjendje globale: lidhet
    shprehimisht me aktorin përmes repo.scoring_context()."""
    terms: tuple                      # (original, lower) — rendi/dedup i _terms
    title_tokens: tuple               # roli: tokena >3 nga titulli i profilit
    location_tokens: tuple            # vendndodhja: tokena >3
    language_names: tuple             # gjuhët e normalizuara (>2 shkronja)
    exp_seniority: frozenset          # senioritet i deklaruar në eksperiencë
    salary_floor: int
    remote_ok: bool
    onsite_ok: bool
    hybrid_ok: bool
    hourly_floor: float
    products: tuple                   # (original, lower) për produkt_lead/freelance
    keywords: tuple                   # fjalët kyçe të filtrave (pa bosh)
    exclude_keywords: tuple           # të vogla/lowered
    gaps: tuple                       # gaps aktivë siç jepen
    learnable_labels: tuple           # etiketa gap të normalizuara 'po mësohet'
    hard_gap_labels: tuple            # etiketa lower të gap-eve hard
    signals: tuple                    # (signal, ((term, regex), …)) për career
    local_prefs: dict                 # preferencat lokale (faktorët lokalë)


def _norm(v: str) -> str:
    """Normalizim i etiketës së gap-it për kreditin 'po mësohet' (OE-007B:
    _norm mungonte te matching dhe rruga e kreditit bënte NameError)."""
    return re.sub(r"\s+", " ", (v or "").lower()).strip()


def prepare_context(profile: dict, filters: dict,
                    local_prefs: dict | None = None,
                    gaps: list | None = None) -> PreparedContext:
    """Ndërton PreparedContext nga fakte të dhëna — pa akses DB. Funksion
    i pastër: i njëjtat fakte → i njëjti kontekst → i njëjtat pikë."""
    if profile is None:
        profile = {}
    if filters is None:
        filters = {}
    pairs, seen = [], set()
    for s in (profile.get("skills") or []) + (filters.get("keywords") or []):
        s = str(s).strip()
        low = s.lower()
        if s and low not in seen:
            seen.add(low)
            pairs.append((s, low))
    prof_title = (profile.get("title") or "").strip().lower()
    title_tokens = tuple(t for t in re.split(r"[^\wåäöüé+]+", prof_title)
                         if len(t) > 3 and t not in _STOP)
    location_tokens = tuple(t for t in re.split(r"[,\s]+", (profile.get("location") or "").lower())
                            if len(t) > 3)
    langs = []
    for entry in profile.get("languages") or []:
        name = re.split(r"[–—-]", str(entry))[0].strip().lower()
        if len(name) > 2:
            langs.append(name)
    exp = (profile.get("experience") or "").strip().lower()
    learnable, hard_labels = [], []
    for g in (gaps or []):
        lbl = _norm(g.get("label") or g.get("target_state") or "")
        if g.get("active") and g.get("willingness") in ("learning", "willing_to_learn"):
            learnable.append(lbl)
        if g.get("severity") == "hard":
            kw = (g.get("label") or "").lower().strip()
            if kw:
                hard_labels.append(kw)
    signals = []
    for signal in (profile.get("search_signals") or []):
        if signal.get("status") not in ("active", "confirmed"):
            continue
        compiled = []
        for t in signal.get("terms", []):
            if isinstance(t, str) and len(t.strip()) >= 3:
                cf = t.casefold()
                compiled.append((t, cf, re.compile(r"(?<!\w)" + re.escape(cf) + r"(?!\w)")))
        if compiled:
            signals.append((signal, tuple(compiled)))
    products = tuple((str(p), str(p).lower()) for p in (profile.get("products") or []))
    keywords = tuple(k for k in (filters.get("keywords") or []) if k.strip())
    excludes = tuple((str(k).strip(), str(k).strip().lower())
                     for k in (filters.get("exclude_keywords") or []) if str(k).strip())
    return PreparedContext(
        terms=tuple(pairs), title_tokens=title_tokens, location_tokens=location_tokens,
        language_names=tuple(langs),
        exp_seniority=frozenset(s for s in _SENIORITY if s in exp),
        salary_floor=int(filters.get("salary_min") or 0),
        remote_ok=bool(filters.get("remote_ok")), onsite_ok=bool(filters.get("onsite_ok")),
        hybrid_ok=bool(filters.get("hybrid_ok")),
        hourly_floor=float(profile.get("hourly_rate_min") or 0),
        products=products, keywords=keywords, exclude_keywords=excludes,
        gaps=tuple(gaps or []), learnable_labels=tuple(learnable),
        hard_gap_labels=tuple(hard_labels), signals=tuple(signals),
        local_prefs=local_prefs or {},
    )


class PreparedOp:
    """Pamje e mundësisë e normalizuar NJË herë — pa json.loads/fromisoformat
    të përsëritura brenda një kalimi pikimi."""
    __slots__ = ("op", "kind", "title_l", "text", "text_cf", "desc_l", "remote",
                 "salary_min", "hourly_rate", "otype", "kd", "kind_data_raw",
                 "is_preview", "location_l", "age_days")

    def __init__(self, op: dict):
        self.op = op
        self.kind = op.get("kind") or "job"
        self.title_l = (op.get("title") or "").lower()
        self.desc_l = (op.get("description") or "").lower()
        self.text = _txt(op)
        self.text_cf = self.text.casefold()
        self.remote = op.get("remote_type") == "remote" or bool(op.get("remote"))
        sm = op.get("salary_min")
        self.salary_min = int(sm) if sm else 0
        self.hourly_rate = op.get("hourly_rate")
        self.otype = (op.get("opportunity_type") or "").strip().lower()
        kd = op.get("kd") or op.get("kind_data") or {}
        if isinstance(kd, str):
            try:
                kd = json.loads(kd)
            except ValueError:
                kd = {}
        self.kd = kd if isinstance(kd, dict) else {}
        # Faktorët lokalë (transport/orari) lexojnë kind_data të paprokessuar —
        # e njëjta burim si para 007E (kd e deco mund të jetë më e vjetër).
        kdraw = op.get("kind_data") or {}
        self.kind_data_raw = kdraw if isinstance(kdraw, dict) else {}
        self.is_preview = self.kd.get("verification_status") == "search_preview"
        self.location_l = (op.get("location") or "").lower()
        self.age_days = _age_days(op)


def _skills_factor(p: PreparedOp, ctx: PreparedContext,
                   weight: float) -> tuple[Factor, list, list]:
    """v4-relevance: mbulim i dëshmive përkatëse, i pangopur nga gjerësia e
    profilit. Termat e papërmendur nuk hyjnë në formulë; pikët saturohen te
    SKILL_SATURATION_POINTS (dokumentuar te docstring-u i modulit)."""
    if not ctx.terms:
        return Factor("skills", FACTOR_LABELS["skills"], UNKNOWN, None, weight,
                      "profil pa aftësi/fjalë kyçe"), [], []
    t_hits = [orig for orig, low in ctx.terms if low in p.title_l]
    t_set = set(t_hits)
    x_hits = [orig for orig, low in ctx.terms
              if low in p.text and orig not in t_set]
    points = 2 * len(t_hits) + len(x_hits)
    value = min(100, round(points / SKILL_SATURATION_POINTS * 100))
    matched = t_hits + x_hits
    missing = [orig for orig, low in ctx.terms if orig not in t_set and orig not in x_hits]
    status = MATCH if value > 0 else MISMATCH
    ev = f"{len(matched)} terma përkatës ({len(t_hits)} në titull)" if matched \
        else "pa terma përkatës"
    return Factor("skills", FACTOR_LABELS["skills"], status, value, weight, ev), matched, missing


@dataclass
class Factor:
    id: str
    label: str
    status: str          # match | mismatch | unknown
    value: float | None  # 0–100, None kur unknown
    weight: float
    evidence: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoreResult:
    kind: str
    final_score: int
    excluded: bool
    factors: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    unknown: list = field(default_factory=list)
    exclusions: list = field(default_factory=list)
    version: str = VERSION

    def factor_dict(self) -> dict:
        """Për persistim në opportunities.match_factors (JSON)."""
        return {
            "version": self.version,
            "final_score": self.final_score,
            "excluded": self.excluded,
            "factors": [f.to_dict() for f in self.factors],
            "matched": self.matched,
            "missing": self.missing,
            "unknown": self.unknown,
            "exclusions": self.exclusions,
        }


# ----------------------------------------------------------- ndihmës

def _is_web_preview(op):
    kd = op.get('kd') or op.get('kind_data') or {}
    if isinstance(kd, str):
        try: kd = json.loads(kd)
        except ValueError: kd = {}
    return isinstance(kd, dict) and kd.get('verification_status') == 'search_preview'


def _txt(op: dict) -> str:
    tags = " ".join(str(t).lower() for t in (op.get("tags") or []))
    return f"{(op.get('title') or '').lower()} {(op.get('description') or '').lower()} {tags}"


def _age_days(op: dict):
    if _is_web_preview(op) and not op.get('posted_at'):
        return None  # Index retrieval time is not the publication date.
    val = op.get("posted_at") or op.get("discovered_at") or ""
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def _freshness_factor(p: PreparedOp, weight: float) -> Factor:
    days = p.age_days
    if days is None:
        return Factor("freshness", FACTOR_LABELS["freshness"], UNKNOWN, None, weight, "pa datë")
    if days <= 7:
        v, note = 100, "≤ 7 ditë"
    elif days <= 14:
        v, note = 75, "≤ 14 ditë"
    elif days <= 30:
        v, note = 50, "≤ 30 ditë"
    elif days <= 60:
        v, note = 25, "≤ 60 ditë"
    else:
        v, note = 10, "> 60 ditë"
    return Factor("freshness", FACTOR_LABELS["freshness"], MATCH, v, weight, note)


def _workmode_factor(p: PreparedOp, ctx: PreparedContext, weight: float) -> Factor:
    if p.is_preview:
        return Factor('workmode', FACTOR_LABELS['workmode'], UNKNOWN, None, weight, 'e panjohur')
    if p.remote:
        if ctx.remote_ok:
            return Factor("workmode", FACTOR_LABELS["workmode"], MATCH, 100, weight, "remote · pranohet")
        return Factor("workmode", FACTOR_LABELS["workmode"], MISMATCH, 0, weight, "remote · jo preferim")
    if ctx.onsite_ok or ctx.hybrid_ok:
        return Factor("workmode", FACTOR_LABELS["workmode"], MATCH, 100, weight, "në zyrë / hybrid")
    return Factor("workmode", FACTOR_LABELS["workmode"], MISMATCH, 0, weight, "në zyrë · jo preferim")


def _location_factor(p: PreparedOp, ctx: PreparedContext, weight: float) -> Factor:
    if not ctx.location_tokens or not p.location_l:
        return Factor("location", FACTOR_LABELS["location"], UNKNOWN, None, weight, "e panjohur")
    hit = any(t in p.location_l for t in ctx.location_tokens)
    if hit:
        return Factor("location", FACTOR_LABELS["location"], MATCH, 100, weight, "brenda zonës")
    return Factor("location", FACTOR_LABELS["location"], MISMATCH, 0, weight, "jashtë zonës")


def _salary_factor(p: PreparedOp, ctx: PreparedContext, weight: float) -> Factor:
    if not ctx.salary_floor or not p.salary_min:
        # pa të dyja anët s'gjykohet — UNKNOWN, jo ndëshkim
        return Factor("salary", FACTOR_LABELS["salary"], UNKNOWN, None, weight, "e panjohur")
    if p.salary_min >= ctx.salary_floor:
        return Factor("salary", FACTOR_LABELS["salary"], MATCH, 100, weight, "mbi minimum")
    return Factor("salary", FACTOR_LABELS["salary"], MISMATCH, 0, weight, "nën minimum")


def _languages_factor(p: PreparedOp, ctx: PreparedContext,
                      weight: float) -> tuple[Factor, list, list]:
    langs = ctx.language_names
    if not langs:
        return Factor("languages", FACTOR_LABELS["languages"], UNKNOWN, None, weight,
                      "profil pa gjuhë"), [], []
    found = [l for l in langs if l in p.text]
    value = round(len(found) / len(langs) * 100)
    missing = [l for l in langs if l not in found]
    status = MATCH if value > 0 else MISMATCH
    return (Factor("languages", FACTOR_LABELS["languages"], status, value, weight,
                   f"{len(found)}/{len(langs)} gjuhë"),
            found, missing)


def _role_factor(p: PreparedOp, ctx: PreparedContext, weight: float) -> Factor:
    if not ctx.title_tokens:
        return Factor("role", FACTOR_LABELS["role"], UNKNOWN, None, weight, "profil pa titull")
    hits = [t for t in ctx.title_tokens if t in p.title_l]
    value = round(len(hits) / len(ctx.title_tokens) * 100)
    status = MATCH if value > 0 else MISMATCH
    return Factor("role", FACTOR_LABELS["role"], status, value, weight,
                  f"titulli i profilit {len(hits)}/{len(ctx.title_tokens)}")


def _experience_factor(p: PreparedOp, ctx: PreparedContext, weight: float) -> Factor:
    title_lvl = [s for s in _SENIORITY if s in p.title_l]
    if not title_lvl or not ctx.exp_seniority:
        return Factor("experience", FACTOR_LABELS["experience"], UNKNOWN, None, weight,
                      "të dhëna të pamjaftueshme")
    if set(title_lvl) & ctx.exp_seniority:
        return Factor("experience", FACTOR_LABELS["experience"], MATCH, 100, weight,
                      f"niveli përputhet ({title_lvl[0]})")
    if ({"senior", "lead", "principal", "staff"} & set(title_lvl)) and "junior" in ctx.exp_seniority:
        return Factor("experience", FACTOR_LABELS["experience"], MISMATCH, 0, weight,
                      "nivel më i lartë se eksperjenca")
    return Factor("experience", FACTOR_LABELS["experience"], MATCH, 60, weight, "nivel i përzier")


# --------------------------------------------------- faktorë sipas llojit

def _budget_factor(p: PreparedOp, ctx: PreparedContext, weight: float) -> Factor:
    """Buxheti freelance — e njëjta degëzim si _freelance_factors (007E:
    e nxjerr që rillogarimi i pjesshëm të mos e kopjojë logjikën)."""
    if ctx.hourly_floor and p.hourly_rate:
        ok = float(p.hourly_rate) >= ctx.hourly_floor
        return Factor("budget", FACTOR_LABELS["budget"], MATCH if ok else MISMATCH,
                      100 if ok else 0, weight, "tarifa orare")
    if ctx.salary_floor and p.salary_min:
        ok = p.salary_min >= ctx.salary_floor
        return Factor("budget", FACTOR_LABELS["budget"], MATCH if ok else MISMATCH,
                      100 if ok else 0, weight, "buxheti mbi/pragun")
    return Factor("budget", FACTOR_LABELS["budget"], UNKNOWN, None, weight, "e panjohur")


def _job_factors(p: PreparedOp, ctx: PreparedContext):
    w = WEIGHTS["job"]
    sk, matched, missing = _skills_factor(p, ctx, w["skills"])
    # 6C: aftësi që mungojnë por janë "po mësohen" (gap aktiv) → kredit i pjesshëm
    learnable = []
    if missing:
        for lbl in ctx.learnable_labels:
            learnable += [t for t in missing if lbl and (lbl in t.lower() or t.lower() in lbl)]
    if learnable:
        sk = Factor(sk.id, sk.label, sk.status,
                    min(100, (sk.value or 0) + 15 * len(learnable)), sk.weight,
                    sk.evidence + f" · po mësohet: {', '.join(learnable)}")
    langs, l_matched, l_missing = _languages_factor(p, ctx, w["languages"])
    matched += [f"gjuhë: {x}" for x in l_matched]
    missing += [f"gjuhë: {x}" for x in l_missing]
    return [
        sk,
        _role_factor(p, ctx, w["role"]),
        _location_factor(p, ctx, w["location"]),
        _workmode_factor(p, ctx, w["workmode"]),
        _salary_factor(p, ctx, w["salary"]),
        langs,
        _experience_factor(p, ctx, w["experience"]),
        _freshness_factor(p, w["freshness"]),
    ], matched, missing


def _freelance_factors(p: PreparedOp, ctx: PreparedContext):
    w = WEIGHTS["freelance"]
    sk, matched, missing = _skills_factor(p, ctx, w["skills"])
    otype = p.otype
    if otype:
        if ctx.products:
            lowered = [low for _, low in ctx.products]
            hit = any(o in low or low in o for low in lowered for o in [otype] if o)
            pt = Factor("project_type", FACTOR_LABELS["project_type"],
                        MATCH if hit else MISMATCH, 100 if hit else 0, w["project_type"],
                        f"lloji: {otype}")
        else:
            pt = Factor("project_type", FACTOR_LABELS["project_type"], UNKNOWN, None,
                        w["project_type"], "pa katalog produktesh/aftësish")
    else:
        pt = Factor("project_type", FACTOR_LABELS["project_type"], UNKNOWN, None,
                    w["project_type"], "lloji i panjohur")
    b = _budget_factor(p, ctx, w["budget"])
    avail = Factor("availability", FACTOR_LABELS["availability"], UNKNOWN, None,
                   w["availability"], "pa të dhëna disponueshmërie")
    return [sk, pt, b, _workmode_factor(p, ctx, w["workmode"]), avail], matched, missing


def _product_lead_factors(p: PreparedOp, ctx: PreparedContext):
    w = WEIGHTS["product_lead"]
    products = [orig for orig, low in ctx.products]
    prod_text = " ".join(low for _, low in ctx.products)
    need = f"{p.title_l} {p.otype}" if p.otype else p.title_l
    if products and p.otype:
        hits = [orig for orig, low in ctx.products if low in need or p.otype in low]
        cap = Factor("capability", FACTOR_LABELS["capability"],
                     MATCH if hits else MISMATCH, 100 if hits else 0, w["capability"],
                     f"produkte: {', '.join(hits[:2])}" if hits else "pa produkt përkatës")
    elif ctx.products:
        # Legacy: pa otype, aftësia përafërohet me skanim produktesh si terma.
        scan_ctx = ctx if ctx.terms else replace(ctx, terms=ctx.products)
        sk, matched, missing = _skills_factor(p, scan_ctx, w["capability"])
        cap = Factor("capability", sk.label, sk.status, sk.value, w["capability"],
                     sk.evidence or "përafërsisht nga terma")
    else:
        cap = Factor("capability", FACTOR_LABELS["capability"], UNKNOWN, None,
                     w["capability"], "katalog produktesh bosh")
        matched, missing = [], []
    if ctx.keywords:
        hits = [k for k in ctx.keywords if k.lower() in p.text]
        prob = Factor("problem", FACTOR_LABELS["problem"],
                      MATCH if hits else MISMATCH, 100 if hits else 0, w["problem"],
                      f"stile problem: {len(hits)}/{len(ctx.keywords)}")
        matched, missing = hits, [k for k in ctx.keywords if k not in hits]
    else:
        prob = Factor("problem", FACTOR_LABELS["problem"], UNKNOWN, None, w["problem"],
                      "pa fjalë kyçe")
        matched, missing = [], []
    if prod_text:
        cust = Factor("customization", FACTOR_LABELS["customization"], MATCH, 60, w["customization"],
                      "vlerësim heuristik — personalizim i vogël i pritshëm")
    else:
        cust = Factor("customization", FACTOR_LABELS["customization"], UNKNOWN, None,
                      w["customization"], "e panjohur")
    return [cap, prob, cust], matched, missing


def _local_factors(p: PreparedOp, ctx: PreparedContext):
    import geo  # 5D: distanca/transport me dëshmi
    w = WEIGHTS["local_job"]
    op = p.op
    prefs = ctx.local_prefs
    loc = geo.resolve_op_location(op, prefs)

    if loc["status"] == "known":
        inside = loc.get("inside_radius")
        d = Factor("distance", FACTOR_LABELS["distance"],
                   MATCH if inside in (True, None) else MISMATCH,
                   100 if inside in (True, None) else 0, w["distance"], loc["evidence"])
    else:
        d = Factor("distance", FACTOR_LABELS["distance"], UNKNOWN, None, w["distance"],
                   loc["evidence"])

    hr = op.get("hourly_rate")
    floor = float(prefs.get("min_hourly_rate") or 0)
    if hr:
        okh = float(hr) >= floor if floor else True
        pay = Factor("pay", FACTOR_LABELS["pay"], MATCH if okh else MISMATCH,
                     100 if okh else 0, w["pay"], f"{hr} €/h" + (f" (min {floor:g})" if floor else ""))
    elif op.get("salary_min") and ctx.salary_floor:
        okh = op["salary_min"] >= ctx.salary_floor
        pay = Factor("pay", FACTOR_LABELS["pay"], MATCH if okh else MISMATCH,
                     100 if okh else 0, w["pay"], "pagesa kundrejt pragut")
    else:
        pay = Factor("pay", FACTOR_LABELS["pay"], UNKNOWN, None, w["pay"], "e panjohur")

    cats = [c.strip().lower() for c in (prefs.get("categories") or []) if c.strip()]
    ocat = p.otype
    if not cats:
        c = Factor("category", FACTOR_LABELS["category"], UNKNOWN, None, w["category"],
                   "pa preferencë kategorish")
    elif ocat in cats:
        c = Factor("category", FACTOR_LABELS["category"], MATCH, 100, w["category"],
                   f"kategoria {ocat}")
    else:
        c = Factor("category", FACTOR_LABELS["category"], MISMATCH, 0, w["category"],
                   f"{ocat or 'pa kategori'} jashtë preferencave")

    compat = geo.transport_compatible(
        p.kind_data_raw.get("transport_required", ""), prefs)
    if compat == "match":
        tr = Factor("transport", FACTOR_LABELS["transport"], MATCH, 100, w["transport"],
                    "transport i përputhur")
    elif compat == "mismatch":
        tr = Factor("transport", FACTOR_LABELS["transport"], MISMATCH, 0, w["transport"],
                    "kërkon makinë · profili pa makinë")
    else:
        tr = Factor("transport", FACTOR_LABELS["transport"], UNKNOWN, None, w["transport"],
                    "transport i panjohur")

    if p.kind_data_raw.get("schedule"):
        sc = Factor("schedule", FACTOR_LABELS["schedule"], MATCH, 100, w["schedule"],
                    str(p.kind_data_raw["schedule"])[:60])
    else:
        sc = Factor("schedule", FACTOR_LABELS["schedule"], UNKNOWN, None, w["schedule"],
                    "pa të dhëna orari")

    fr = _freshness_factor(p, w["freshness"])

    sk, matched, missing = _skills_factor(p, ctx, 0.0)  # vetëm chips
    cat = Factor("category", FACTOR_LABELS["category"], sk.status, sk.value, w["category"],
                 sk.evidence)
    c = _constraint_factor(p, ctx, 0.8)
    return [d, pay, cat, tr, sc, fr, c], matched, missing


def _constraint_factor(p: PreparedOp, ctx: PreparedContext, weight: float):
    """Kufizim i fortë i shprehur nga përdoruesi (gap severity=hard) që teksti
    i mundësisë e prek — deterministic, s'mund të anashkalohet nga AI."""
    for kw in ctx.hard_gap_labels:
        if kw in p.title_l or kw in p.desc_l:
            return Factor("constraints", "Kufizim i fortë", MISMATCH, 0, weight,
                          f" kufizim: {kw}")
    return Factor("constraints", "Kufizim i fortë", UNKNOWN, None, weight,
                  "pa kufizim përkatës")


_FACTORS_BY_KIND = {
    "job": _job_factors,
    "freelance": _freelance_factors,
    "product_lead": _product_lead_factors,
    "local_job": _local_factors,
}


# ------------------------------------------------------------- hyrja

def score(op: dict, profile: dict | None = None, filters: dict | None = None,
          local_prefs: dict | None = None, gaps: list | None = None,
          ctx: PreparedContext | None = None) -> ScoreResult:
    """Funksion i pastër mbi hyrje — i riprodhueshëm.

    OE-007E: me `ctx` (PreparedContext nga prepare_context/repo.scoring_context)
    profili NUK normalizohet përsëri për çdo mundësi — një kontekst për kalim.
    Pa ctx, zgjidhen ambientet si më parë dhe ndërtohet një kontekst i brendshëm;
    konsumi është I NJËJTI shteg, prandaj pariteti është nga konstruktimi."""
    import repository as repo  # import i vendosur: cikli i lehtë vetëm për default-e

    if ctx is None:
        if profile is None or filters is None:
            profile = profile if profile is not None else repo.matching_profile()
            filters = filters if filters is not None else repo.get_filters()
        kind_probe = op.get("kind") or "job"
        if kind_probe in ("job", "local_job") and gaps is None:
            import repository as _r
            gaps = [g for g in _r.list_gaps(active_only=True)]
        if kind_probe == "local_job" and local_prefs is None:
            import repository as _r
            local_prefs = _r.get_local_prefs()
        ctx = prepare_context(profile, filters, local_prefs, gaps)

    p = PreparedOp(op)
    kind = p.kind
    build = _FACTORS_BY_KIND.get(kind, _job_factors)
    factors, matched, missing = build(p, ctx)

    # Match related roles/tasks across languages without claiming qualifications.
    if ctx.signals and kind in ('job', 'freelance', 'local_job'):
        hits = []
        for signal, compiled in ctx.signals:
            # Parafiltër i barabartë semantikisht: përputhja e regex-it me kufij
            # fjalësh e nënkupton nëntekstin — rreshtat pa asnjë nëntekst s'provojnë
            # asnjë regex (007E: 86 sinjale × çdo rresht ishin 92% e kalimit).
            if not any(cf in p.text_cf for _, cf, _ in compiled):
                continue
            term = next((t for t, cf, rx in compiled if rx.search(p.text_cf)), None)
            if term:
                hits.append((signal, term))
        if hits:
            evidence = ' | '.join(f"{s['labels']['en']} → {term}: {s['evidence'][0][:180]}" for s, term in hits[:3])
            factors.append(Factor('career_connections', 'Lidhje me profilin', MATCH,
                                  min(100, 65 + len(hits)*10), (.3 if all(s.get('basis')=='interest' for s,_ in hits) else .9) + (.1 if any(s.get('status')=='confirmed' for s,_ in hits) else 0), evidence))
            matched.extend(s['labels']['en'] for s, _ in hits)

    # përjashtime të forta — deterministic, e pavarur nga pikët
    exclusions = [orig for orig, low in ctx.exclude_keywords if low in p.text]

    known = [f for f in factors if f.status != UNKNOWN]
    total_w = sum(f.weight for f in known)
    final = round(sum((f.value or 0) * f.weight for f in known) / total_w) if total_w else 0
    final = max(0, min(100, int(final)))

    return ScoreResult(
        kind=kind,
        final_score=final,
        excluded=bool(exclusions),
        factors=factors,
        matched=matched,
        missing=missing,
        unknown=[f.id for f in factors if f.status == UNKNOWN],
        exclusions=exclusions,
    )


def rescore_factor(factor_id: str, op: dict, ctx: PreparedContext,
                   weight: float) -> Factor:
    """Rillogarim i pjesshëm (OE-007E): vetëm faktorët me inpute kolonash të
    vogla. I njëjtat funksione faktorësh → matematikë identike me score()."""
    p = PreparedOp(op)
    if factor_id == "location":
        return _location_factor(p, ctx, weight)
    if factor_id == "role":
        return _role_factor(p, ctx, weight)
    if factor_id == "workmode":
        return _workmode_factor(p, ctx, weight)
    if factor_id == "salary":
        return _salary_factor(p, ctx, weight)
    if factor_id == "budget":
        return _budget_factor(p, ctx, weight)
    raise ValueError(f"faktor i pambuluar për rillogarim të pjesshëm: {factor_id}")


def score_job_compat(op: dict, profile: dict | None = None,
                     filters: dict | None = None) -> tuple[int, bool]:
    """Adapter legacy: (rule_score, excluded) për thirrësit ekzistues."""
    r = score(op, profile, filters)
    return r.final_score, r.excluded


def breakdown_dict(result: ScoreResult) -> dict:
    """Për panelin/drawer-in: faktorë si dicts + terma matched/missing."""
    return {
        "version": result.version,
        "factors": [
            {"label": f.label, "id": f.id, "status": f.status,
             "value": f.value, "evidence": f.evidence}
            for f in result.factors
        ],
        "matched": result.matched,
        "missing": result.missing,
        "unknown": result.unknown,
        "excluded": result.excluded,
        "exclusions": result.exclusions,
    }
