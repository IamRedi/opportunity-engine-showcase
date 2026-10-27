"""Presentation-only SQ/EN/DE localization; stored data and API values stay canonical."""
from contextvars import ContextVar
import json
from pathlib import Path
import re

LANGUAGES = {"sq": "Shqip", "en": "English", "de": "Deutsch"}
_locale = ContextVar("ui_locale", default="sq")
CATALOG = json.loads((Path(__file__).parent / "static" / "translations.json").read_text(encoding="utf-8"))


def get_locale():
    return _locale.get()


def translate(source, **values):
    source = str(source or "")
    text = CATALOG.get(source, {}).get(get_locale(), source)
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m[1], m[0])), text)


def client_catalog():
    return {key: value[get_locale()] for key, value in CATALOG.items()
            if get_locale() in value}

SYSTEM_PATTERNS = [
    "{n} terma përkatës ({total} në titull)",
    "{n} min më parë", "{n} orë më parë", "{n} ditë më parë",
    "{n}/{total} terma", "{n}/{total} gjuhë", "stile problem: {n}/{total}",
    "gjuhë: {value}", "lloji: {value}", "produkte: {value}", "+{n} të reja",
    "Profili u ruajt — pikët u rillogaritën për {n} zëra",
    "pagë {n}€/h nën minimumin {total}€/h", "pagë {n}€/h",
    "distanca {n} km", "udhëtimi ~{n} min", "kode postare: {n} km",
    "koordinata: {n} km", "titulli i profilit {n}/{total}",
    "niveli përputhet ({value})", "po mësohet: {value}",
    "kategoria {value}", "{value} jashtë preferencave", " kufizim: {value}",
    "burimi i papërdorshëm: {value}",
    "{n}/{total} faqe punëdhënësi dështuan: {value}",
    "Ruajtja e burimit dështoi ({value}). Provo përsëri.",
    "tipare për rolin {value}", "certifikatë e zakonshme për {value}",
    "format i pambështetur: {value}", "PDF i korruptuar: {value}",
    "DOCX i korruptuar: {value}", "seksion {value} pa merge-target",
    "fetch dështoi: {value}", "SMTP: {value}", "e papritur: {value}",
]


def ui_text(source):
    """Localize known system output; interpolated evidence retains its original text."""
    source = str(source or "")
    if source in CATALOG:
        return translate(source)
    for pattern in SYSTEM_PATTERNS:
        regex = re.escape(pattern)
        for name in re.findall(r"\{(\w+)\}", pattern):
            regex = regex.replace(re.escape("{" + name + "}"),
                                  r"(?P<" + name + r">[\d.,]+)" if name in ("n", "total") else r"(?P<" + name + r">.+)")
        match = re.fullmatch(regex, source)
        if match:
            values = match.groupdict()
            if pattern == "{n}/{total} faqe punëdhënësi dështuan: {value}":
                values['value'] = ui_text(values['value'])
            return translate(pattern, **values)
    # Joining system messages is presentation-only, never used on descriptions or CV evidence.
    if " · " in source:
        return " · ".join(ui_text(part) for part in source.split(" · "))
    return source
