"""Konfigurim qendror: branding, lloje mundësish, statuse, burime.

Asnjë emërtim/etiketë produkti s'duhet hardcoduar jashtë këtij moduli (spec §27).
Statuse: taste kanonike anglisht në DB + etiketa shqip për UI (spec §12.3).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- runtime configuration (Faza 8A: hosted readiness) ---
APP_ENV = os.environ.get("APP_ENV", "local")            # local | test | production
DEMO_MODE = os.environ.get("OPPORTUNITY_DEMO") == "1"
SECRET_KEY = os.environ.get("SECRET_KEY", "")           # produksjon: i detyrueshëm
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD_SHA256 = os.environ.get("AUTH_PASSWORD_SHA256", "")
AUTH_PASSWORD_PLAIN = os.environ.get("AUTH_PASSWORD", "")  # vetëm dev/8A; prod → sha256
SESSION_TTL_HOURS = int(os.environ.get("SESSION_TTL_HOURS", "12"))
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", '/tmp/opportunity-engine' if os.environ.get('VERCEL') else str(BASE_DIR)))
STORAGE_PROVIDER = os.environ.get("STORAGE_PROVIDER", "local")  # local | (s3 më vonë)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8765")
SEARCH_COUNTRIES={'de':'Gjermani','at':'Austri','ch':'Zvicër','nl':'Holandë','be':'Belgjikë','fr':'Francë','pl':'Poloni','it':'Itali','es':'Spanjë','gb':'Mbretëria e Bashkuar','us':'SHBA','ca':'Kanada','au':'Australi','nz':'Zelandë e Re','br':'Brazil','in':'Indi','mx':'Meksikë','sg':'Singapor','za':'Afrikë e Jugut'}
SCHEDULER_INTERVAL = int(os.environ.get("SCHEDULER_INTERVAL", "60"))
FEATURE_FLAGS = {f.strip() for f in os.environ.get("FEATURE_FLAGS", "").split(",") if f.strip()}


def auth_required() -> bool:
    """Prod: autentikim i detyrueshëm. Lokal/test/test-venv: hapur (dev-auth)."""
    return APP_ENV == "production"


def csrf_required() -> bool:
    return auth_required()


def validate_production(db_path: Path | None = None) -> list[str]:
    """Fail-fast: kthen listën e problemeve (bosh = konfig i plotë).
    Lexon env FRESH — testet/deploys e thërrasin me mjedis të ndryshëm."""
    errors = []
    if os.environ.get('VERCEL') and os.environ.get('APP_ENV') != 'production':
        return ['Vercel requires APP_ENV=production']
    if os.environ.get("APP_ENV", "local") != "production":
        return errors
    secret = os.environ.get("SECRET_KEY", "")
    if not secret or len(secret) < 32:
        errors.append("SECRET_KEY mungon ose është < 32 karaktere")
    if not os.environ.get("AUTH_USERNAME"):
        errors.append("AUTH_USERNAME mungon")
    if not os.environ.get("AUTH_PASSWORD_SHA256") and not os.environ.get("AUTH_PASSWORD"):
        errors.append("AUTH_PASSWORD ose AUTH_PASSWORD_SHA256 mungon")
    dbp = Path(db_path) if db_path else Path(DB_PATH)
    remote = os.environ.get('DATABASE_PROVIDER') == 'turso'
    if remote and (not os.environ.get('TURSO_DATABASE_URL','').startswith('libsql://') or not os.environ.get('TURSO_AUTH_TOKEN')):
        errors.append('Turso configuration is missing')
    if not remote and not dbp.exists():
        errors.append(f"DB nuk ekziston: {dbp} (rikthim backup ose init i kontrolluar)")
    data_dir = os.environ.get("APP_DATA_DIR", str(DATA_DIR))
    if os.environ.get('STORAGE_PROVIDER') != 'vercel-blob' and data_dir and not Path(data_dir).exists():
        errors.append(f"APP_DATA_DIR nuk ekziston: {data_dir}")
    if os.environ.get('VERCEL'):
        if not remote: errors.append('Vercel requires a durable remote database')
        if os.environ.get('STORAGE_PROVIDER') != 'vercel-blob' or not os.environ.get('BLOB_READ_WRITE_TOKEN'):
            errors.append('Vercel requires private Blob storage')
        if os.environ.get('LOCAL_PILOT_USERS') == '1': errors.append('Local pilot switching must be disabled on Vercel')
    return errors

# --- branding ---
APP_NAME = os.environ.get("APP_NAME", "Opportunity Engine")

# --- databaza (env override për teste / deploy) ---
DB_PATH = Path(os.environ.get("OPPORTUNITY_DB", str(BASE_DIR / "jobs.db")))

# --- llojet e mundësive (spec §2, §12.2) ---
KIND_JOB = "job"
KIND_FREELANCE = "freelance"
KIND_PRODUCT_LEAD = "product_lead"
KIND_LOCAL = "local_job"
KINDS = (KIND_JOB, KIND_FREELANCE, KIND_PRODUCT_LEAD, KIND_LOCAL)

# vlerë e vjetër e pranueshme në input (adapter backward-compatible)
LEGACY_KIND_MAP = {"opportunity": KIND_FREELANCE}

MODE_KIND = {"jobs": KIND_JOB, "freelance": KIND_FREELANCE, "products": KIND_PRODUCT_LEAD,
             "local": KIND_LOCAL, "all": None}

# --- kategoritë lokale (konfigurueshme/extensible — spec §5D) ---
LOCAL_CATEGORIES = (
    "dog_walking", "pet_sitting", "cat_sitting", "house_sitting", "gardening",
    "errands", "shopping_help", "delivery", "household_help", "technical_help",
    "computer_phone_help", "neighborhood_help", "tutoring", "babysitting", "senior_companion", "minijob", "other_local",
)
LOCAL_CATEGORY_LABELS = {
    "dog_walking": "Ecje qensh", "pet_sitting": "Kujdes kafshësh",
    "cat_sitting": "Kujdes macesh", "house_sitting": "Kujdes shtëpie",
    "gardening": "Kopshtari", "errands": "Erranda", "shopping_help": "Ndihmë blerjesh",
    "delivery": "Dërgesa", "household_help": "Ndihmë shtëpiake",
    "technical_help": "Ndihmë teknike", "computer_phone_help": "Ndihmë PC/telefon",
    "tutoring": "Mësime private dhe gjuhë", "babysitting": "Kujdes fëmijësh",
    "senior_companion": "Shoqërim të moshuarish",
    "neighborhood_help": "Ndihmë lagjeje", "minijob": "Minipunë", "other_local": "Tjetër",
}
# fjalë-kyçe → kategori (deterministik, për normalizim nga teksti)
LOCAL_CATEGORY_KEYWORDS = {
    "dog_walking": ("dog walking", "hund ausführen", "gassi", "ecje qen"),
    "pet_sitting": ("pet sitting", "pet sitter", "haustiersitting", "kujdes kafsh"),
    "cat_sitting": ("cat sitting", "katzensitting", "kujdes mace"),
    "house_sitting": ("house sitting", "haushüter", "kujdes shtëpi"),
    "gardening": ("garden", "gardening", "rasen", "kopsht", "gart"),
    "errands": ("errand", "besorgung", "errand"),
    "shopping_help": ("shopping help", "einkaufshilfe", "blerje"),
    "delivery": ("delivery", "liefer", "courier", "dërges"),
    "household_help": ("household", "haushaltshilfe", "haushalt", "shtëpiake"),
    "technical_help": ("technical help", "montage", "reparatur", "ndihmë teknike"),
    "computer_phone_help": ("computer help", "pc-hilfe", "computerPhone", "smartphone hilfe"),
    "tutoring": ("nachhilfe", "tutoring", "language tutor", "mësime private"),
    "babysitting": ("babysitting", "babysitter", "kinderbetreuung", "kujdes fëmij"),
    "senior_companion": ("seniorenbegleitung", "alltagsbegleitung", "senior companion", "shoqërim të moshuar"),
    "minijob": ("minijob", "mini-job", "nebenjob", "aushilfe"),
    "neighborhood_help": ("nachbarschaft", "neighborhood", "lagje"),
}

# --- gaps willingness states (Faza 6) ---
GAP_WILLINGNESS = ("learning", "willing_to_learn", "willing_to_obtain",
                   "unavailable", "not_interested")
GAP_SEVERITIES = ("info", "soft", "hard")

# etiketa shqip të llojeve (për UI/teste — spec §5)
KIND_LABELS = {
    KIND_JOB: "Punë",
    KIND_FREELANCE: "Freelance",
    KIND_PRODUCT_LEAD: "Leads",
    KIND_LOCAL: "Lokale",
}

# --- statuse kanonike sipas llojit (spec §12.3) ---
STATUS_SETS = {
    KIND_JOB: ["new", "saved", "draft_prepared", "applied", "response",
               "interview", "offer", "rejected", "archived"],
    KIND_FREELANCE: ["new", "lead_saved", "contacted", "proposal_prepared",
                     "proposal_sent", "negotiation", "won", "lost", "archived"],
    KIND_PRODUCT_LEAD: ["new", "qualified", "outreach_prepared", "contacted",
                        "interested", "proposal_sent", "won", "lost", "archived"],
    KIND_LOCAL: ["new", "saved", "contacted", "accepted", "completed",
                 "rejected", "archived"],
}
ALL_STATUSES = sorted({s for lst in STATUS_SETS.values() for s in lst})

STATUS_LABELS = {
    "new": "e re", "saved": "e ruajtur", "draft_prepared": "draft i përgatitur",
    "applied": "aplikuar", "response": "përgjigje", "interview": "intervistë",
    "offer": "ofertë", "rejected": "refuzuar", "archived": "arkivuar",
    "lead_saved": "lead i ruajtur", "contacted": "kontaktuar",
    "proposal_prepared": "propozim i përgatitur", "proposal_sent": "propozim dërguar",
    "negotiation": "negocim", "won": "fituar", "lost": "humbur",
    "qualified": "kualifikuar", "outreach_prepared": "outreach i përgatitur",
    "interested": "i interesuar", "accepted": "pranuar", "completed": "përfunduar",
}

# etiketa shumës për barin kompakt të statuseve
STRIP_LABELS = {
    "new": "Të reja", "saved": "Të ruajtura", "applied": "Aplikuara",
    "interview": "Intervista", "offer": "Ofertat", "lead_saved": "Leads",
    "contacted": "Kontaktuar", "proposal_sent": "Propozime", "won": "Fituar",
    "qualified": "Kualifikuar", "interested": "Interesuar", "accepted": "Pranuar",
    "completed": "Përfunduar", "rejected": "Refuzuara", "archived": "Arkivuar",
}

STRIP_KEYS = {
    "products": ["new", "qualified", "contacted", "proposal_sent", "won"],
    "jobs": ["new", "saved", "applied", "interview", "offer"],
    "freelance": ["new", "lead_saved", "contacted", "proposal_sent", "won"],
    "all": ["new", "saved", "applied", "interview", "offer"],
    "local": ["new", "saved", "contacted", "completed"],
}

# vlerat e ruajtura para Fazës 1 → taste kanonike
LEGACY_STATUS_MAP = {
    "e re": "new", "e ruajtur": "saved", "aplikuar": "applied",
    "intervistë": "interview", "ofertë": "offer", "refuzuar": "rejected",
    "arkivuar": "archived", "lead": "lead_saved", "kontaktuar": "contacted",
    "propozim": "proposal_sent", "fituar": "won", "humbur": "lost",
}

QUICK_ACTIONS = {
    KIND_JOB: [("saved", "Ruaj"), ("applied", "Aplikova"), ("rejected", "Refuzo")],
    KIND_FREELANCE: [("lead_saved", "Ruaj lead"), ("contacted", "Kontaktova"), ("lost", "Humbur")],
    KIND_PRODUCT_LEAD: [("qualified", "Kualifiko"), ("contacted", "Kontaktova"), ("lost", "Humbur")],
    KIND_LOCAL: [("saved", "Ruaj"), ("contacted", "Kontaktova"), ("completed", "Përfunduar")],
}

# --- burimet (lista ndërtohet nga konfigurimi, jo nga routat) ---
SOURCES = [
    {"id": "arbeitnow", "name": "Arbeitnow", "note": "Punë në Gjermani · pa celes"},
    {"id": "remotive", "name": "Remotive", "note": "Remote · pa celes"},
    {"id": "jobicy", "name": "Jobicy", "note": "Remote · pa celes"},
    {"id": "adzuna", "name": "Adzuna", "note": "DACH · kërkon celes", "key": True},
    {"id": "hackernews_freelance", "name": "HN Freelance", "note": "Projekte freelance · pa celes", "kinds": ["freelance"]},
    {"id": "remotive_contract", "name": "Remotive Kontrata", "note": "Kontrata remote · pa celes", "kinds": ["freelance"]},
    {"id": "jobicy_contract", "name": "Jobicy Kontrata", "note": "Kontrata remote · pa celes", "kinds": ["freelance"]},
    {"id": "hn_leads", "name": "HN Leads", "note": "Kërkesa software (leads) · pa celes", "kinds": ["product_lead"]},
]


def canon_status(value: str) -> str:
    v = (value or "").strip()
    if v in ALL_STATUSES:
        return v
    return LEGACY_STATUS_MAP.get(v, v)


def canon_kind(value: str) -> str:
    v = (value or "").strip()
    if v in KINDS:
        return v
    return LEGACY_KIND_MAP.get(v, v)

# Search terms describe requested work, never assert qualifications.
LOCAL_SEARCH_TERMS = {
    'dog_walking': ('Gassi gehen', 'dog walking'),
    'pet_sitting': ('Tierbetreuung', 'pet sitting'),
    'cat_sitting': ('Katzenbetreuung', 'cat sitting'),
    'house_sitting': ('Hausbetreuung', 'house sitting'),
    'gardening': ('Gartenhilfe', 'gardening'),
    'errands': ('Besorgungshilfe', 'errands'),
    'shopping_help': ('Einkaufshilfe', 'shopping help'),
    'delivery': ('Lieferdienst', 'delivery'),
    'household_help': ('Haushaltshilfe', 'household help'),
    'technical_help': ('Reparaturhilfe', 'repair assistance'),
    'computer_phone_help': ('Computerhilfe', 'computer support'),
    'neighborhood_help': ('Nachbarschaftshilfe', 'neighborhood assistance'),
    'tutoring': ('Nachhilfe', 'tutoring'),
    'babysitting': ('Kinderbetreuung', 'babysitting'),
    'senior_companion': ('Seniorenbegleitung', 'senior companion'),
    'minijob': ('Aushilfe', 'part time work'),
    'other_local': ('Nebenjob', 'local work'),
}
