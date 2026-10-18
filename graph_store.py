"""graph_store.py:  connected-entity graph.

User -> Location -> Zone <- Station <- Patrol, plus Contacts, Hospitals and past Threat Alerts.
All queries are deterministic (no LLM) so the emergency path stays fast and auditable.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import networkx as nx

EARTH_R_M = 6_371_000.0
ROAD_FACTOR = 1.3  # straight-line -> approximate road distance for ETAs
SPEED_MPS = {"PATROL": 8.3, "STATION": 8.3, "HOSPITAL": 11.1}  # ~30 km/h urban patrol, ~40 km/h ambulance
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
KIND_COLORS = {
    "USER": "#4C9AFF", "LOCATION": "#8993A4", "ZONE": "#6554C0", "STATION": "#0052CC",
    "PATROL": "#00B8D9", "HOSPITAL": "#FF5630", "CONTACT": "#36B37E", "ALERT": "#FFAB00", "PLACE": "#97A0AF",
}


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def parse_ts(ts: Any = None) -> datetime:
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc)
    dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def risk_level_from_score(score: float) -> str:
    if score >= 0.85:
        return "CRITICAL"
    if score >= 0.60:
        return "HIGH"
    if score >= 0.35:
        return "MEDIUM"
    return "LOW"


def max_risk(a: str, b: str) -> str:
    return a if RISK_LEVELS.index(a) >= RISK_LEVELS.index(b) else b


class SafetyGraph:
    """Thread-safe MultiDiGraph. Node ids are prefixed by kind: user:, zone:, station:, patrol:, hospital:,
    contact:, loc:, alert:. Edge key = relation, so re-adding a relation updates rather than duplicates."""

   
    def __init__(self, path: str | Path | None = "data/safety_graph.json", autosave: bool = True):
       self.path = Path(path) if path else None
       self.autosave = autosave and self.path is not None
       self.g = nx.MultiDiGraph()
       self._lock = threading.RLock()
       self.backend = "NetworkX"
       self.degraded = False

       if self.path and self.path.exists():
         self.load()


    def has_node(self, nid: str) -> bool:
        return nid in self.g

    def set_prop(self, nid: str, key: str, value) -> None:
        with self._lock:
            if nid in self.g:
                self.g.nodes[nid][key] = value
        self._autosave()

    def remove_node(self, nid: str) -> None:
        with self._lock:
            if nid in self.g:
                self.g.remove_node(nid)
        self._autosave()

    def reset(self) -> None:
        with self._lock:
            self.g.clear()
        self._autosave()

    def drain_trace(self) -> list:
        return []
        backend = "NetworkX (in-memory + JSON)"
    degraded = False

    def has_node(self, nid: str) -> bool:
        return nid in self.g

    def set_prop(self, nid: str, key: str, value) -> None:
        with self._lock:
            if nid in self.g:
                self.g.nodes[nid][key] = value
        self._autosave()

    def remove_node(self, nid: str) -> None:
        with self._lock:
            if nid in self.g:
                self.g.remove_node(nid)
        self._autosave()

    def reset(self) -> None:
        with self._lock:
            self.g.clear()
        self._autosave()

    def drain_trace(self) -> list:
        return []

       # ------------------------------------------------------------ primitives
    def _put(self, nid: str, kind: str, name: str, **attrs: Any) -> str:
        with self._lock:
            if nid in self.g:
                self.g.nodes[nid].update(kind=kind, name=name, **attrs)
            else:
                self.g.add_node(nid, kind=kind, name=name, **attrs)
        return nid

    def _link(self, src: str, rel: str, dst: str, **attrs: Any) -> None:
        with self._lock:
            self.g.add_edge(src, dst, key=rel, **attrs)

    def _autosave(self) -> None:
        if self.autosave:
            self.save()

    def get(self, nid: str) -> Optional[dict]:
        with self._lock:
            return {"id": nid, **self.g.nodes[nid]} if nid in self.g else None

    # --------------------------------------------------------------- builders
    def add_user(self, user_id: str, name: str, phone: str = "", **attrs: Any) -> str:
        nid = self._put(f"user:{user_id}", "USER", name, phone=phone, **attrs)
        self._autosave()
        return nid

    def add_zone(self, zone_id: str, name: str, lat: float, lng: float, radius_m: float,
                 base_risk: float, risk_factors: Optional[list[str]] = None) -> str:
        nid = self._put(f"zone:{zone_id}", "ZONE", name, lat=lat, lng=lng, radius_m=radius_m,
                        base_risk=float(base_risk), risk_factors=list(risk_factors or []))
        self._autosave()
        return nid

    def add_station(self, station_id: str, name: str, lat: float, lng: float, phone: str = "",
                    jurisdiction_zones: Iterable[str] = ()) -> str:
        nid = self._put(f"station:{station_id}", "STATION", name, lat=lat, lng=lng, phone=phone)
        for z in jurisdiction_zones:
            self._link(nid, "JURISDICTION_OVER", f"zone:{z}")
        self._autosave()
        return nid

    def add_patrol(self, patrol_id: str, callsign: str, station_id: str, lat: float, lng: float,
                   status: str = "AVAILABLE") -> str:
        nid = self._put(f"patrol:{patrol_id}", "PATROL", callsign, lat=lat, lng=lng, status=status)
        self._link(nid, "ASSIGNED_TO", f"station:{station_id}")
        self._autosave()
        return nid

    def add_hospital(self, hospital_id: str, name: str, lat: float, lng: float, phone: str = "",
                     has_trauma: bool = False) -> str:
        nid = self._put(f"hospital:{hospital_id}", "HOSPITAL", name, lat=lat, lng=lng, phone=phone,
                        has_trauma=has_trauma)
        self._autosave()
        return nid

    def add_contact(self, user_id: str, contact_id: str, name: str, phone: str, relation: str,
                    priority: int = 1, channels: Iterable[str] = ("SMS", "CALL")) -> str:
        nid = self._put(f"contact:{contact_id}", "CONTACT", name, phone=phone, relation=relation,
                        channels=list(channels))
        self._link(f"user:{user_id}", "HAS_CONTACT", nid, priority=int(priority))
        self._autosave()
        return nid

    def add_place(self, user_id: str, place_id: str, label: str, lat: float, lng: float,
                  place_kind: str = "HOME") -> str:
        """Home / frequent-route points (user FREQUENTS place; place IN_ZONE zone)."""
        nid = self._put(f"loc:{place_id}", "PLACE", label, lat=lat, lng=lng, place_kind=place_kind)
        self._link(f"user:{user_id}", "FREQUENTS", nid)
        zone = self.zone_for_point(lat, lng)
        if zone:
            self._link(nid, "IN_ZONE", zone["id"])
        self._autosave()
        return nid

    def set_user_location(self, user_id: str, lat: float, lng: float, accuracy_m: Optional[float] = None,
                          ts: Any = None) -> str:
        """Moves the user's live location node and re-derives the IN_ZONE edge."""
        uid, lid = f"user:{user_id}", f"loc:now:{user_id}"
        with self._lock:
            if uid not in self.g:
                raise KeyError(f"Unknown user '{user_id}'")
            self._put(lid, "LOCATION", "Current location", lat=lat, lng=lng, accuracy_m=accuracy_m,
                      updated_at=parse_ts(ts).isoformat())
            self._link(uid, "LOCATED_AT", lid)
            for _, dst, key in list(self.g.out_edges(lid, keys=True)):
                if key == "IN_ZONE":
                    self.g.remove_edge(lid, dst, key)
            zone = self.zone_for_point(lat, lng)
            if zone:
                self._link(lid, "IN_ZONE", zone["id"])
        self._autosave()
        return lid

    def update_patrol(self, patrol_id: str, lat: Optional[float] = None, lng: Optional[float] = None,
                      status: Optional[str] = None) -> None:
        nid = f"patrol:{patrol_id}"
        with self._lock:
            if nid not in self.g:
                raise KeyError(f"Unknown patrol '{patrol_id}'")
            if lat is not None and lng is not None:
                self.g.nodes[nid].update(lat=lat, lng=lng)
            if status:
                self.g.nodes[nid]["status"] = status
        self._autosave()

    def add_threat_alert(self, alert_id: str, lat: float, lng: float, alert_type: str, risk_level: str,
                         user_id: Optional[str] = None, ts: Any = None, summary: str = "") -> str:
        nid = self._put(f"alert:{alert_id}", "ALERT", f"{alert_type} ({risk_level})", lat=lat, lng=lng,
                        alert_type=alert_type, risk_level=risk_level, ts=parse_ts(ts).isoformat(),
                        summary=summary, user_id=user_id)
        if user_id and f"user:{user_id}" in self.g:
            self._link(nid, "RAISED_BY", f"user:{user_id}")
        zone = self.zone_for_point(lat, lng)
        if zone:
            self._link(nid, "OCCURRED_IN", zone["id"])
        self._autosave()
        return nid

    # ---------------------------------------------------------------- queries
    def zone_for_point(self, lat: float, lng: float) -> Optional[dict]:
        """Smallest zone whose circle contains the point (most specific wins)."""
        with self._lock:
            best = None
            for nid, d in self.g.nodes(data=True):
                if d.get("kind") != "ZONE":
                    continue
                dist = haversine_m(lat, lng, d["lat"], d["lng"])
                if dist <= d["radius_m"] and (best is None or d["radius_m"] < best["radius_m"]):
                    best = {"id": nid, **d, "distance_m": round(dist)}
            return best

    def recent_alerts(self, lat: Optional[float] = None, lng: Optional[float] = None, radius_m: float = 1500,
                      within_hours: Optional[float] = None, within_minutes: Optional[float] = None,
                      user_id: Optional[str] = None, now: Any = None) -> list[dict]:
        now_dt = parse_ts(now)
        window = None
        if within_minutes is not None:
            window = timedelta(minutes=within_minutes)
        elif within_hours is not None:
            window = timedelta(hours=within_hours)
        out = []
        with self._lock:
            for nid, d in self.g.nodes(data=True):
                if d.get("kind") != "ALERT":
                    continue
                if user_id is not None and d.get("user_id") != user_id:
                    continue
                age = now_dt - parse_ts(d["ts"])
                if window is not None and not (timedelta(0) <= age <= window):
                    continue
                dist = None
                if lat is not None and lng is not None:
                    dist = haversine_m(lat, lng, d["lat"], d["lng"])
                    if dist > radius_m:
                        continue
                out.append({"id": nid, **d, "age_min": round(age.total_seconds() / 60, 1),
                            "distance_m": None if dist is None else round(dist)})
        out.sort(key=lambda a: a["age_min"])
        return out

    def location_risk(self, lat: float, lng: float, local_hour: Optional[int] = None, now: Any = None) -> dict:
        """Deterministic 0-1 location risk: zone base risk x time-of-day factor + nearby recent alerts."""
        zone = self.zone_for_point(lat, lng)
        factors: list[str] = []
        base = zone["base_risk"] if zone else 0.30
        factors.append(f"zone '{zone['name']}' base risk {base:.2f}" if zone else "no mapped zone: default base risk 0.30")
        if zone and zone.get("risk_factors"):
            factors.append("zone factors: " + ", ".join(zone["risk_factors"]))
        hour = local_hour if local_hour is not None else parse_ts(now).hour
        tf = 1.3 if (hour >= 22 or hour < 5) else 1.15 if hour >= 18 else 1.0
        if tf > 1.0:
            factors.append(f"time-of-day multiplier x{tf} (hour {hour:02d})")
        nearby = self.recent_alerts(lat, lng, radius_m=1500, within_hours=24 * 30, now=now)
        bump = min(0.25, 0.08 * len(nearby))
        if nearby:
            factors.append(f"{len(nearby)} alert(s) within 1.5 km in the last 30 days (+{bump:.2f})")
        score = max(0.0, min(1.0, base * tf + bump))
        return {"score": round(score, 3), "zone": zone, "factors": factors,
                "nearby_alerts": [{"id": a["id"], "type": a["alert_type"], "age_min": a["age_min"],
                                   "distance_m": a["distance_m"]} for a in nearby],
                "zone_found": zone is not None}

    def nearest(self, kind: str, lat: float, lng: float, k: int = 3,
                predicate: Optional[Callable[[dict], bool]] = None) -> list[dict]:
        rows = []
        with self._lock:
            for nid, d in self.g.nodes(data=True):
                if d.get("kind") != kind or "lat" not in d:
                    continue
                view = {"id": nid, **d}
                if predicate and not predicate(view):
                    continue
                dist = haversine_m(lat, lng, d["lat"], d["lng"])
                view["distance_m"] = round(dist)
                view["eta_min"] = round(dist * ROAD_FACTOR / SPEED_MPS.get(kind, 1.4) / 60, 1)
                rows.append(view)
        rows.sort(key=lambda r: r["distance_m"])
        return rows[:k]

    def contacts_for(self, user_id: str) -> list[dict]:
        uid = f"user:{user_id}"
        with self._lock:
            if uid not in self.g:
                return []
            out = []
            for _, cid, key, data in self.g.out_edges(uid, keys=True, data=True):
                if key == "HAS_CONTACT":
                    out.append({"id": cid, **self.g.nodes[cid], "priority": data.get("priority", 99)})
        out.sort(key=lambda c: c["priority"])
        return out

    def responders_for(self, user_id: str, lat: float, lng: float, emergency_type: str = "UNKNOWN") -> dict:
        """Graph traversal: location -> zone -> jurisdiction station -> assigned available patrol; + hospital."""
        zone = self.zone_for_point(lat, lng)
        stations = self.nearest("STATION", lat, lng, k=20)
        station, station_reason = None, "no stations in graph"
        if zone:
            with self._lock:
                juris = {u for u, _, k in self.g.in_edges(zone["id"], keys=True) if k == "JURISDICTION_OVER"}
            owned = [s for s in stations if s["id"] in juris]
            if owned:
                station, station_reason = owned[0], f"has jurisdiction over zone '{zone['name']}' (nearest of {len(owned)})"
        if station is None and stations:
            station, station_reason = stations[0], "nearest station (no jurisdiction match for this location)"

        def assigned_to(view: dict, sid: str) -> bool:
            return self.g.has_edge(view["id"], sid, key="ASSIGNED_TO")

        patrol, patrol_reason = None, "no available patrol vehicle"
        available = lambda v: v.get("status") == "AVAILABLE"  # noqa: E731
        if station:
            own = self.nearest("PATROL", lat, lng, k=1, predicate=lambda v: available(v) and assigned_to(v, station["id"]))
            if own:
                patrol, patrol_reason = own[0], f"nearest AVAILABLE patrol assigned to {station['name']}"
        if patrol is None:
            anyp = self.nearest("PATROL", lat, lng, k=1, predicate=available)
            if anyp:
                patrol, patrol_reason = anyp[0], "no available patrol at responsible station; nearest AVAILABLE patrol overall"

        hospitals = self.nearest("HOSPITAL", lat, lng, k=10)
        hospital, hospital_reason = None, "no hospitals in graph"
        if emergency_type in ("ASSAULT", "MEDICAL", "ACCIDENT"):
            trauma = [h for h in hospitals if h.get("has_trauma")]
            if trauma and (not hospitals or trauma[0]["distance_m"] <= hospitals[0]["distance_m"] * 2.5):
                hospital, hospital_reason = trauma[0], "nearest trauma-capable hospital"
        if hospital is None and hospitals:
            hospital, hospital_reason = hospitals[0], "nearest hospital"

        if station and station["distance_m"] > 30_000:
            station = None
        if patrol and patrol["distance_m"] > 25_000:
            patrol = None
        if hospital and hospital["distance_m"] > 30_000:
            hospital = None
        for item, reason in ((station, station_reason), (patrol, patrol_reason), (hospital, hospital_reason)):
            if item:
                item["selection_reason"] = reason
        return {"zone": zone, "station": station, "patrol": patrol, "hospital": hospital,
                "contacts": self.contacts_for(user_id)}

    # --------------------------------------------------------- viz / export
    def dispatch_subgraph(self, user_id: str, extra_ids: Iterable[str] = ()) -> dict:
        """Nodes/edges relevant to one incident (user, live location, zone, chosen responders, contacts)."""
        keep = {f"user:{user_id}", f"loc:now:{user_id}", *extra_ids}
        with self._lock:
            lid = f"loc:now:{user_id}"
            if lid in self.g:
                keep |= {d for _, d, k in self.g.out_edges(lid, keys=True) if k == "IN_ZONE"}
            keep = {n for n in keep if n in self.g}
            nodes = [{"id": n, "name": self.g.nodes[n]["name"], "kind": self.g.nodes[n]["kind"]} for n in keep]
            edges = [{"source": u, "relation": k, "target": v} for u, v, k in self.g.subgraph(keep).edges(keys=True)]
        return {"nodes": nodes, "edges": edges}

    def geo_points(self, highlight: Iterable[str] = ()) -> list[dict]:
        """Rows for st.map: lat, lon, color, size, name, kind. Highlighted ids are drawn larger."""
        hl = set(highlight)
        rows = []
        with self._lock:
            for nid, d in self.g.nodes(data=True):
                if "lat" in d and d.get("kind") in ("USER", "LOCATION", "STATION", "PATROL", "HOSPITAL", "ALERT"):
                    if d["kind"] == "LOCATION" and nid.startswith("loc:now:"):
                        rows.append({"id": nid, "name": "User (live)", "kind": "USER", "lat": d["lat"], "lon": d["lng"],
                                     "color": KIND_COLORS["USER"], "size": 90})
                    elif d["kind"] != "LOCATION":
                        rows.append({"id": nid, "name": d["name"], "kind": d["kind"], "lat": d["lat"], "lon": d["lng"],
                                     "color": KIND_COLORS[d["kind"]], "size": 90 if nid in hl else 40})
        return rows

    def to_pyvis_html(self, highlight_nodes: Iterable[str] = (), highlight_edges: Iterable[tuple] = (),
                      only_nodes: Optional[Iterable[str]] = None, height: str = "520px") -> str:
        """Interactive graph; the dispatch path glows. highlight_edges: (source, relation, target)."""
        from pyvis.network import Network

        hl_n, hl_e = set(highlight_nodes), {tuple(e) for e in highlight_edges}
        keep = set(only_nodes) if only_nodes is not None else None
        try:
            net = Network(height=height, width="100%", directed=True, bgcolor="#0e1117",
                          font_color="#e6e6e6", cdn_resources="in_line")
        except TypeError:
            net = Network(height=height, width="100%", directed=True, bgcolor="#0e1117", font_color="#e6e6e6")
        dim = bool(hl_n or hl_e)
        with self._lock:
            for nid, d in self.g.nodes(data=True):
                if keep is not None and nid not in keep:
                    continue
                active = nid in hl_n
                color = KIND_COLORS.get(d["kind"], "#8993A4")
                net.add_node(nid, label=d["name"], title=f"{d['kind']}", size=30 if active else 16,
                             color={"background": color, "border": "#FFD700" if active else color},
                             borderWidth=4 if active else 1, opacity=1.0 if (active or not dim) else 0.35)
            for u, v, k in self.g.edges(keys=True):
                if keep is not None and (u not in keep or v not in keep):
                    continue
                active = (u, k, v) in hl_e
                net.add_edge(u, v, label=k, width=4 if active else 1,
                             color="#FF6B35" if active else ("#444b57" if dim else "#8993A4"),
                             font={"size": 10, "color": "#e6e6e6" if active else "#8993A4"})
        net.set_options('{"physics":{"barnesHut":{"gravitationalConstant":-7000,"springLength":150}},'
                        '"edges":{"arrows":{"to":{"enabled":true}},"smooth":{"type":"dynamic"}}}')
        return net.generate_html()

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            payload = {
                "nodes": [{"id": n, **d} for n, d in self.g.nodes(data=True)],
                "edges": [{"source": u, "target": v, "relation": k, **d} for u, v, k, d in self.g.edges(keys=True, data=True)],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def load(self) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        with self._lock:
            self.g.clear()
            for n in payload.get("nodes", []):
                nid = n.pop("id")
                self.g.add_node(nid, **n)
            for e in payload.get("edges", []):
                u, v, k = e.pop("source"), e.pop("target"), e.pop("relation")
                self.g.add_edge(u, v, key=k, **e)

    def stats(self) -> dict:
        with self._lock:
            kinds: dict[str, int] = {}
            for _, d in self.g.nodes(data=True):
                kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
            return {"nodes": self.g.number_of_nodes(), "edges": self.g.number_of_edges(), "kinds": kinds}


def _offset(center: tuple[float, float], north_m: float, east_m: float) -> tuple[float, float]:
    lat = center[0] + north_m / 111_320
    lng = center[1] + east_m / (111_320 * math.cos(math.radians(center[0])))
    return round(lat, 6), round(lng, 6)


def seed_demo(store: SafetyGraph, center: tuple[float, float] = (37.7749, -122.4194)) -> bool:
    """Fictional demo city laid out around `center` (change it to relocate everything). No-op if non-empty."""
    if store.g.number_of_nodes() > 0:
        return False
    P = lambda n, e: _offset(center, n, e)  # noqa: E731
    store.add_zone("downtown", "Downtown Night Market", *P(0, 0), 900, 0.72, ["crowded nightlife", "poor street lighting"])
    store.add_zone("univ", "University Quarter", *P(1500, -800), 1000, 0.35, ["student area"])
    store.add_zone("riverside", "Riverside Industrial", *P(-1800, 1600), 1100, 0.80, ["isolated after dark", "warehouses"])
    store.add_zone("harbor", "Harbor Residential", *P(-300, -2200), 1000, 0.25, ["residential"])
    store.add_station("central", "Central Police Station", *P(150, 100), "+00-555-0111", ["downtown", "univ"])
    store.add_station("riverside", "Riverside Police Post", *P(-1700, 1300), "+00-555-0122", ["riverside"])
    store.add_station("harbor", "Harbor Police Station", *P(-400, -2000), "+00-555-0133", ["harbor"])
    store.add_patrol("p101", "Patrol 101", "central", *P(400, -300), "AVAILABLE")
    store.add_patrol("p102", "Patrol 102", "central", *P(-200, 500), "BUSY")
    store.add_patrol("p201", "Patrol 201", "riverside", *P(-1600, 1700), "AVAILABLE")
    store.add_patrol("p301", "Patrol 301", "harbor", *P(-500, -1800), "AVAILABLE")
    store.add_hospital("mercy", "St. Mercy General (Trauma)", *P(600, 400), "+00-555-0199", has_trauma=True)
    store.add_hospital("harbor_clinic", "Harbor Clinic", *P(-350, -2300), "+00-555-0188", has_trauma=False)
    store.add_user("u1", "Sam Rivera", "+00-555-0100")
    store.add_contact("u1", "mom", "Mom", "+00-555-0101", "Mother", priority=1)
    store.add_contact("u1", "jordan", "Jordan", "+00-555-0102", "Friend", priority=2)
    store.add_contact("u1", "casey", "Casey", "+00-555-0103", "Roommate", priority=3)
    store.add_place("u1", "home", "Home", *P(-300, -2300), "HOME")
    store.add_place("u1", "library", "University Library", *P(1400, -900), "FREQUENT")
    store.add_place("u1", "gym", "Gym", *P(200, -500), "FREQUENT")
    store.set_user_location("u1", *P(1500, -800), accuracy_m=12)
    now = datetime.now(timezone.utc)
    store.add_threat_alert("a1", *P(50, 40), "HARASSMENT", "HIGH", ts=now - timedelta(days=2), summary="Reported near market entrance")
    store.add_threat_alert("a2", *P(-100, 120), "ROBBERY", "HIGH", ts=now - timedelta(days=9), summary="Phone snatching")
    store.add_threat_alert("a3", *P(200, -150), "HARASSMENT", "MEDIUM", ts=now - timedelta(days=15), summary="Verbal harassment")
    return True