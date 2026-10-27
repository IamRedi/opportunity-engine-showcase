"""Mbështetje gjeografike lokale (Faza 5D) — deterministic, pa API me pagesë.

- Distanca e drejtë (haversine) kur ekzistojnë koordinatat.
- Fallback kodi postar: kartela offline `data/postcodes.json`
  {"postcode": [lat, lon]} — e zgjerueshme nga përdoruesi; kod i panjohur → UNKNOWN.
- Koha e udhëtimit: UNKNOWN përveç nëse vjen si të dhënë e mundësisë
  (asnjë shpikje; routing provider i ardhshëm përmes abstraksionit më poshtë).
"""
import json
import math
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "postcodes.json"
EARTH_R = 6371.0


def _postcodes() -> dict:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def coords_for(postcode: str) -> tuple | None:
    """(lat, lon) për kod postar, ose None (UNKNOWN — s'shpiket)."""
    return _postcodes().get(str(postcode).strip()) if postcode else None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = math.radians
    dlat = r(lat2 - lat1)
    dlon = r(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(dlon / 2) ** 2)
    return round(EARTH_R * 2 * math.asin(math.sqrt(a)), 2)


def resolve_op_location(op: dict, local_prefs: dict) -> dict:
    """Zbulon distancën me dëshmi. Kthen:
    {status: known|unknown, distance_km|None, inside_radius|None, evidence}"""
    radius = local_prefs.get("radius_km")
    op_lat, op_lon = op.get("latitude"), op.get("longitude")
    base = local_prefs.get("base_coords")  # (lat, lon) i vendosur nga preferencat
    if op_lat is not None and op_lon is not None and base:
        km = haversine_km(base[0], base[1], float(op_lat), float(op_lon))
        return {"status": "known", "distance_km": km,
                "inside_radius": (km <= float(radius)) if radius else None,
                "evidence": f"koordinata: {km} km"}
    kd = op.get("kind_data") if isinstance(op.get("kind_data"), dict) else {}
    pc_op = op.get("postcode") or kd.get("postcode")
    pc_base = local_prefs.get("postcode")
    if pc_op and pc_base and pc_op == pc_base:
        return {"status": "known", "distance_km": 0.0, "inside_radius": True,
                "evidence": "kod postar i njëjtë"}
    if pc_op and pc_base:
        c1, c2 = coords_for(pc_base), coords_for(pc_op)
        if c1 and c2:
            km = haversine_km(c1[0], c1[1], c2[0], c2[1])
            return {"status": "known", "distance_km": km,
                    "inside_radius": (km <= float(radius)) if radius else None,
                    "evidence": f"kode postare: {km} km"}
    return {"status": "unknown", "distance_km": None, "inside_radius": None,
            "evidence": "pa koordinata/kod postar të njohur"}


def transport_compatible(op_transport: str, prefs: dict) -> str:
    """'match' | 'mismatch' | 'unknown' — kufizimet e transportit."""
    prefs_mode = (prefs.get("transport_mode") or "").lower()
    req = (op_transport or "").lower()
    if not req or not prefs_mode:
        return "unknown"
    if req == prefs_mode:
        return "match"
    # no-car i shprehur + punë që kërkon makinë → përplasje e fortë
    if prefs.get("no_car") and req in ("car", "auto"):
        return "mismatch"
    return "unknown"


def travel_time_min(op: dict):
    """Koha e udhëtimit VETËM si e dhënë ekzistente — kurrë e shpikur."""
    kd = op.get("kind_data") if isinstance(op.get("kind_data"), dict) else {}
    v = kd.get("travel_min") or op.get("travel_time_min")
    return float(v) if v else None


def worth_the_trip(op: dict, local_prefs: dict, location_info: dict) -> dict:
    """Metrikë 'worth the trip' 0–100 nga dëshmi të njohura; UNKNOWN përndryshe.

    Formula (deterministik): 60% paga orare (e krahasuar me min. konfiguruar,
    saturohet në 2× prag) + 25% distanca (≤2 km 100 → >rrezja 0) +
    15% koha e udhëtimit (e njohur: ≤15min 100 → >45min 0).
    Mjafton 1 faktor i njohur për skor; faktorët e panjohur hiqen nga peshat.
    """
    hourly = op.get("hourly_rate")
    if not hourly:
        kd = op.get("kind_data") if isinstance(op.get("kind_data"), dict) else {}
        hourly = kd.get("hourly_rate")
    km = location_info.get("distance_km")
    travel = travel_time_min(op)
    evidence, reasons = [], []

    pay_factor = None
    floor = float(local_prefs.get("min_hourly_rate") or 0)
    if hourly:
        h = float(hourly)
        if floor and h < floor:
            pay_factor, _ = 0, None
            reasons.append(f"pagë {h}€/h nën minimumin {floor}€/h")
        else:
            pay_factor = min(100, round(h / max(floor, 1) * 60)) if floor else min(100, round(h / 12 * 60))
            reasons.append(f"pagë {h}€/h")
        evidence.append({"factor": "pay", "value": h})
    elif floor:
        reasons.append("pagë e panjohur — nuk gjykohet")

    dist_factor = None
    if km is not None:
        dist_factor = max(0, 100 - int(km * 10))
        reasons.append(f"distanca {km} km")
        evidence.append({"factor": "distance", "value": km})
    elif location_info.get("status") == "unknown":
        reasons.append("distanca e panjohur — nuk gjykohet")

    travel_factor = None
    if travel is not None:
        travel_factor = max(0, 100 - int(travel * 2))
        reasons.append(f"udhëtimi ~{int(travel)} min")
        evidence.append({"factor": "travel", "value": travel})

    parts = [(pay_factor, 0.6), (dist_factor, 0.25), (travel_factor, 0.15)]
    known = [(v, w) for v, w in parts if v is not None]
    if not known:
        return {"worth_score": None, "status": "unknown",
                "evidence": evidence, "reasons": reasons + ["dëshmi e pamjaftueshme"]}
    score = round(sum(v * w for v, w in known) / sum(w for _, w in known))
    return {"worth_score": max(0, min(100, score)), "status": "known",
            "evidence": evidence, "reasons": reasons}
