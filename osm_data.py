"""osm_data.py: real police stations, hospitals and street-safety signals from OpenStreetMap (Overpass API).
Free, no API key. Results are cached in data/osm_cache so repeat lookups are instant."""
from __future__ import annotations

import hashlib
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

CACHE_DIR = Path("data/osm_cache")
ENDPOINTS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
M_PER_DEG = 111_320.0
MAX_SPAN_DEG = 0.2  # about 22 km; keeps queries small and polite

QUERY = """[out:json][timeout:25];(
nwr["amenity"="police"]({b});
nwr["amenity"~"^(hospital|clinic)$"]({b});
nwr["amenity"~"^(bar|pub|nightclub)$"]({b});
nwr["amenity"~"^(cafe|restaurant|fast_food|pharmacy|fuel|atm|bank)$"]({b});
nwr["shop"~"^(convenience|supermarket|mall|department_store|general|kiosk)$"]({b});
way["highway"]["lit"~"^(yes|no)$"]({b});
);out center tags 6000;"""


def bbox_for(origin, dest, margin_m: float = 800.0):
    (lat0, lng0), (lat1, lng1) = origin, dest
    kx = M_PER_DEG * math.cos(math.radians((lat0 + lat1) / 2))
    s, n = min(lat0, lat1) - margin_m / M_PER_DEG, max(lat0, lat1) + margin_m / M_PER_DEG
    w, e = min(lng0, lng1) - margin_m / kx, max(lng0, lng1) + margin_m / kx
    if (n - s) > MAX_SPAN_DEG or (e - w) > MAX_SPAN_DEG:
        raise ValueError("Area too large for a lookup (max about 20 km).")
    return (math.floor(s * 100) / 100, math.floor(w * 100) / 100, math.ceil(n * 100) / 100, math.ceil(e * 100) / 100)


def _parse(raw: dict, bbox) -> dict:
    out = {"bbox": list(bbox), "police": [], "hospital": [], "nightlife": [], "venues": [], "lit": [], "unlit": []}
    for el in raw.get("elements", []):
        t = el.get("tags", {})
        c = el.get("center") or el
        if "lat" not in c:
            continue
        lat, lng = c["lat"], c["lon"]
        am = t.get("amenity")
        ident = f"osm_{el['type'][0]}{el['id']}"
        phone = t.get("phone") or t.get("contact:phone") or ""
        if am == "police":
            out["police"].append({"id": ident, "name": t.get("name") or t.get("name:en") or "Police station",
                                  "lat": lat, "lng": lng, "phone": phone})
        elif am in ("hospital", "clinic"):
            out["hospital"].append({"id": ident, "name": t.get("name") or t.get("name:en") or am.title(),
                                    "lat": lat, "lng": lng, "phone": phone,
                                    "trauma": am == "hospital" and t.get("emergency") == "yes"})
        elif am in ("bar", "pub", "nightclub"):
            out["nightlife"].append([lat, lng])
        elif am or t.get("shop"):
            out["venues"].append([lat, lng, 1.0 if t.get("opening_hours") == "24/7" else 0.0])
        if "highway" in t and t.get("lit") in ("yes", "no"):
            out["lit" if t["lit"] == "yes" else "unlit"].append([lat, lng])
    return out


def fetch(bbox, force: bool = False) -> dict:
    s, w, n, e = bbox
    path = CACHE_DIR / (hashlib.md5(f"{s:.2f},{w:.2f},{n:.2f},{e:.2f}".encode()).hexdigest()[:12] + ".json")
    if path.exists() and not force:
        return json.loads(path.read_text(encoding="utf-8"))
    body = urllib.parse.urlencode({"data": QUERY.format(b=f"{s},{w},{n},{e}")}).encode()
    last = None
    for ep in ENDPOINTS:
        try:
            req = urllib.request.Request(ep, data=body, headers={"User-Agent": "SafeGuardHackathonDemo/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = _parse(json.loads(r.read().decode()), bbox)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
            return data
        except Exception as exc:
            last = exc
    raise RuntimeError(f"OpenStreetMap lookup failed: {last}")


def import_into_graph(graph, data: dict) -> dict:
    """Adds real police stations and hospitals to the SafetyGraph (skips ones already there)."""
    added = {"stations": 0, "hospitals": 0}
    old = graph.autosave
    graph.autosave = False
    try:
        for p in data["police"]:
            if f"station:{p['id']}" not in graph.g:
                nid = graph.add_station(p["id"], p["name"], p["lat"], p["lng"], p.get("phone", ""))
                graph.set_prop(nid, "source", "OpenStreetMap")
                added["stations"] += 1
        for h in data["hospital"]:
            if f"hospital:{h['id']}" not in graph.g:
                nid = graph.add_hospital(h["id"], h["name"], h["lat"], h["lng"], h.get("phone", ""),
                                         has_trauma=h.get("trauma", False))
                graph.set_prop(nid, "source", "OpenStreetMap")
                added["hospitals"] += 1
    finally:
        graph.autosave = old
    if added["stations"] or added["hospitals"]:
        graph.save()
    return added


def ensure_area(graph, origin, dest=None, margin_m: float = 800.0) -> dict:
    """Fetch (or load from cache) the area around origin..dest and import stations/hospitals into the graph."""
    data = fetch(bbox_for(origin, dest or origin, margin_m))
    added = import_into_graph(graph, data)
    counts = {k: len(data[k]) for k in ("police", "hospital", "venues", "nightlife", "lit", "unlit")}
    return {"data": data, "counts": counts, "added": added}


def _arr(x, cols: int):
    return np.array(x or [], dtype=float).reshape(-1, cols)


class OsmRisk:
    """Heuristic street-safety adjustment from OpenStreetMap: lighting, open venues, nightlife.
    Not crime statistics. Where OSM lacks data for a signal, that signal is skipped (unknown is not assumed bad)."""

    def __init__(self, data: dict, hour=None):
        h = 12 if hour is None else int(hour)
        self.night, self.late = (h >= 19 or h < 6), (h >= 21 or h < 3)
        self.unlit, self.lit, self.nightlife = _arr(data.get("unlit"), 2), _arr(data.get("lit"), 2), _arr(data.get("nightlife"), 2)
        v = _arr(data.get("venues"), 3)
        self.venues = v[:, :2]
        base_w = 1.0 if 7 <= h < 21 else 0.3  # most shops are closed at night; 24/7 places always count
        self.v_w = np.where(v[:, 2] > 0, 1.0, base_w) if len(v) else np.zeros(0)

    @staticmethod
    def _within(arr, lat, lng, r):
        if len(arr) == 0:
            return np.zeros(0, dtype=bool)
        dy = (arr[:, 0] - lat) * M_PER_DEG
        dx = (arr[:, 1] - lng) * M_PER_DEG * math.cos(math.radians(lat))
        return dx * dx + dy * dy <= r * r

    def adjust(self, lat: float, lng: float):
        delta, flags = 0.0, set()
        u = int(self._within(self.unlit, lat, lng, 120).sum())
        l = int(self._within(self.lit, lat, lng, 120).sum())
        if u + l:
            share = u / (u + l)
            delta += (0.22 if self.night else 0.04) * share
            if share > 0.5:
                flags.add("unlit")
        if len(self.venues):
            active = float(self.v_w[self._within(self.venues, lat, lng, 150)].sum())
            if active < 0.5:
                delta += 0.12 if self.night else 0.05
                flags.add("isolated")
            elif active >= 3:
                delta -= 0.08
                flags.add("busy")
        if self.late:
            n = int(self._within(self.nightlife, lat, lng, 200).sum())
            if n:
                delta += min(0.12, 0.04 * n)
                flags.add("nightlife")
        return delta, flags