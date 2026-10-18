"""falkor_graph.py: FalkorDB as SafeGuard's context layer (people, places, responders, memory, decisions, agents).
FalkorDB is the source of truth. A NetworkX mirror is kept in-process as a read cache for geometry and UI.
Every FalkorDB read/write fails soft: if the database is down, dispatch continues on the cache ('degraded')."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from falkordb import FalkorDB

from graph_store import ROAD_FACTOR, SPEED_MPS, SafetyGraph, haversine_m, parse_ts

GRAPH_NAME = os.getenv("FALKORDB_GRAPH", "safeguard")
LABELS = {"USER": "User", "LOCATION": "Location", "ZONE": "Zone", "STATION": "Station", "PATROL": "Patrol",
          "HOSPITAL": "Hospital", "CONTACT": "Contact", "ALERT": "Alert", "PLACE": "Place"}
KIND_OF = {v: k for k, v in LABELS.items()}
EXTRA_LABELS = ["Incident", "Decision", "Action", "Step", "Agent", "Task", "Episode", "Walk"]
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

AGENT_NAMES = ["GuardianAgent", "PhoneBridgeAgent", "TriageAgent", "DispatchAgent", "ScribeAgent"]
HANDOFFS = [("GuardianAgent", "TriageAgent"), ("PhoneBridgeAgent", "TriageAgent"),
            ("TriageAgent", "DispatchAgent"), ("DispatchAgent", "ScribeAgent")]
STEP_AGENT = {"trigger_intake": "GuardianAgent", "assess_context": "TriageAgent", "triage": "TriageAgent",
              "score_risk": "TriageAgent", "plan_dispatch": "DispatchAgent", "execute_dispatch": "DispatchAgent",
              "explain": "ScribeAgent", "memory_update": "ScribeAgent", "finalize": "ScribeAgent"}


def agent_for(step: str) -> str:
    return "DispatchAgent" if step.startswith("dispatch:") else STEP_AGENT.get(step, "ScribeAgent")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect():
    kw: dict[str, Any] = {}
    if os.getenv("FALKORDB_USERNAME"):
        kw["username"] = os.environ["FALKORDB_USERNAME"]
    if os.getenv("FALKORDB_PASSWORD"):
        kw["password"] = os.environ["FALKORDB_PASSWORD"]
    if os.getenv("FALKORDB_SSL") == "1":
        kw["ssl"] = True
    db = FalkorDB(host=os.getenv("FALKORDB_HOST", "localhost"), port=int(os.getenv("FALKORDB_PORT", "6379")), **kw)
    return db, db.select_graph(GRAPH_NAME)


def _clean(v: Any):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [x for x in (_clean(i) for i in v) if x is not None and not isinstance(x, (list, dict))]
    if isinstance(v, dict):
        return json.dumps(v, default=str)
    return str(v)


def _props(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        c = _clean(v)
        if _IDENT.match(k) and c is not None:
            out[k] = c
    return out


def _view(node) -> dict:
    p = dict(node.properties)
    return {"id": p.pop("id"), **p}


def plain(v):
    if hasattr(v, "labels") and hasattr(v, "properties"):
        return {"_labels": list(v.labels), **v.properties}
    if hasattr(v, "relation") and hasattr(v, "properties"):
        return {"_rel": v.relation, **v.properties}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


class FalkorSafetyGraph(SafetyGraph):
    backend = "FalkorDB"

    def __init__(self):
        
        super().__init__(path=None, autosave=False)
        self.backend = "FalkorDB"
        self.degraded, self.last_error = False, ""
        self._tl = threading.local()
        self._cache: dict[str, Any] = {}
        self.db, self.fg = connect()
        self.fg.query("RETURN 1")  # fail fast if the database is unreachable
        for label in list(LABELS.values()) + EXTRA_LABELS:
            try:
                self.fg.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
            except Exception:
                pass  # already exists
        self._load_mirror()
        self._ensure_agents()

    # ----------------------------------------------------------- plumbing
    def _q(self, cypher: str, params: Optional[dict] = None, ro: bool = False):
        t0 = time.perf_counter()
        res = (self.fg.ro_query if ro else self.fg.query)(cypher, params or {})
        log = self._tl.__dict__.setdefault("trace", [])
        log.append({"cypher": " ".join(cypher.split()),
                    "params": {k: (str(v)[:77] + "...") if len(str(v)) > 80 else v for k, v in (params or {}).items()},
                    "rows": len(res.result_set), "ms": round((time.perf_counter() - t0) * 1000, 1)})
        if len(log) > 120:
            del log[:60]
        return res

    def _w(self, cypher: str, params: Optional[dict] = None):
        try:
            return self._q(cypher, params)
        except Exception as exc:
            self._fail(exc)
            return None

    def _fail(self, exc: Exception) -> None:
        self.degraded, self.last_error = True, str(exc)

    def drain_trace(self) -> list[dict]:
        log = self._tl.__dict__.get("trace", [])
        self._tl.trace = []
        return log[-40:]

    def _touch(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cached(self, key: str, fn):
        with self._lock:
            if key not in self._cache:
                self._cache[key] = fn()
            return self._cache[key]

    def _label(self, nid: str) -> Optional[str]:
        kind = self.g.nodes[nid].get("kind") if nid in self.g else None
        return LABELS.get(kind)

    def _load_mirror(self) -> None:
        with self._lock:
            self.g.clear()
            for (n,) in self.fg.ro_query("MATCH (n) RETURN n").result_set:
                label = n.labels[0] if n.labels else ""
                if label not in KIND_OF:
                    continue
                p = dict(n.properties)
                nid = p.pop("id", None)
                if nid:
                    self.g.add_node(nid, kind=KIND_OF[label], name=p.pop("name", nid), **p)
            for a, rel, b, edge in self.fg.ro_query("MATCH (a)-[r]->(b) RETURN a.id, type(r), b.id, r").result_set:
                if a in self.g and b in self.g:
                    self.g.add_edge(a, b, key=rel, **dict(edge.properties))

    def _ensure_agents(self) -> None:
        for name in AGENT_NAMES:
            self._w("MERGE (a:Agent {id:$i}) SET a.name=$n", {"i": f"agent:{name}", "n": name})
        for a, b in HANDOFFS:
            self._w("MATCH (x:Agent {id:$a}), (y:Agent {id:$b}) MERGE (x)-[:HANDS_OFF_TO]->(y)",
                    {"a": f"agent:{a}", "b": f"agent:{b}"})

    # ------------------------------------------------- write-through (mirror + FalkorDB)
    def _put(self, nid: str, kind: str, name: str, **attrs: Any) -> str:
        super()._put(nid, kind, name, **attrs)
        self._w(f"MERGE (n:{LABELS[kind]} {{id:$id}}) SET n += $p", {"id": nid, "p": _props({"name": name, **attrs})})
        self._touch()
        return nid

    def _link(self, src: str, rel: str, dst: str, **attrs: Any) -> None:
        super()._link(src, rel, dst, **attrs)
        ls, ld = self._label(src), self._label(dst)
        if ls and ld and _IDENT.match(rel):
            p = _props(attrs)
            self._w(f"MATCH (a:{ls} {{id:$s}}), (b:{ld} {{id:$d}}) MERGE (a)-[r:{rel}]->(b)"
                    + (" SET r += $p" if p else ""), {"s": src, "d": dst, "p": p})
        self._touch()

    def set_user_location(self, user_id, lat, lng, accuracy_m=None, ts=None):
        lid = super().set_user_location(user_id, lat, lng, accuracy_m, ts)
        self._w("MATCH (:Location {id:$l})-[r:IN_ZONE]->() DELETE r", {"l": lid})
        for _, dst, k in list(self.g.out_edges(lid, keys=True)):
            if k == "IN_ZONE":
                self._w("MATCH (a:Location {id:$l}), (b:Zone {id:$z}) MERGE (a)-[:IN_ZONE]->(b)", {"l": lid, "z": dst})
        self._touch()
        return lid

    def update_patrol(self, patrol_id, lat=None, lng=None, status=None):
        super().update_patrol(patrol_id, lat, lng, status)
        self._w("MATCH (p:Patrol {id:$id}) SET p += $p",
                {"id": f"patrol:{patrol_id}", "p": _props({"lat": lat, "lng": lng, "status": status})})
        self._touch()

    def set_prop(self, nid: str, key: str, value: Any) -> None:
        super().set_prop(nid, key, value)
        label = self._label(nid)
        if label and _IDENT.match(key):
            self._w(f"MATCH (n:{label} {{id:$id}}) SET n += $p", {"id": nid, "p": _props({key: value})})

    def remove_node(self, nid: str) -> None:
        label = self._label(nid)
        super().remove_node(nid)
        if label:
            self._w(f"MATCH (n:{label} {{id:$id}}) DETACH DELETE n", {"id": nid})
        self._touch()

    def reset(self) -> None:
        super().reset()
        self._w("MATCH (n) DETACH DELETE n")
        self._touch()
        self._ensure_agents()

    # --------------------------------------------- reads that drive decisions (Cypher, cached when hot)
    def _zones(self) -> list[dict]:
        return self._cached("zones", lambda: [_view(r[0]) for r in self._q("MATCH (z:Zone) RETURN z", ro=True).result_set])

    def _alerts(self) -> list[dict]:
        return self._cached("alerts", lambda: [_view(r[0]) for r in self._q("MATCH (a:Alert) RETURN a", ro=True).result_set])

    def zone_for_point(self, lat: float, lng: float):
        try:
            best = None
            for z in self._zones():
                dist = haversine_m(lat, lng, z["lat"], z["lng"])
                if dist <= z["radius_m"] and (best is None or z["radius_m"] < best["radius_m"]):
                    best = {**z, "kind": "ZONE", "distance_m": round(dist)}
            return best
        except Exception as exc:
            self._fail(exc)
            return super().zone_for_point(lat, lng)

    def recent_alerts(self, lat=None, lng=None, radius_m=1500, within_hours=None, within_minutes=None,
                      user_id=None, now=None):
        try:
            alerts = self._alerts()
        except Exception as exc:
            self._fail(exc)
            return super().recent_alerts(lat, lng, radius_m, within_hours, within_minutes, user_id, now)
        now_dt = parse_ts(now)
        window = timedelta(minutes=within_minutes) if within_minutes is not None else (
            timedelta(hours=within_hours) if within_hours is not None else None)
        out = []
        for d in alerts:
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
            out.append({**d, "age_min": round(age.total_seconds() / 60, 1),
                        "distance_m": None if dist is None else round(dist)})
        out.sort(key=lambda a: a["age_min"])
        return out

    def contacts_for(self, user_id: str) -> list[dict]:
        try:
            rows = self._q("MATCH (u:User {id:$u})-[h:HAS_CONTACT]->(c:Contact) RETURN c, h.priority ORDER BY h.priority",
                           {"u": f"user:{user_id}"}, ro=True).result_set
            return [{**_view(c), "priority": 99 if pr is None else pr} for c, pr in rows]
        except Exception as exc:
            self._fail(exc)
            return super().contacts_for(user_id)

    @staticmethod
    def _rank(views: list[dict], lat: float, lng: float, kind: str) -> list[dict]:
        for v in views:
            d = haversine_m(lat, lng, v["lat"], v["lng"])
            v["distance_m"] = round(d)
            v["eta_min"] = round(d * ROAD_FACTOR / SPEED_MPS.get(kind, 1.4) / 60, 1)
        return sorted(views, key=lambda v: v["distance_m"])

    def _nodes(self, cypher: str, params: Optional[dict] = None) -> list[dict]:
        return [_view(r[0]) for r in self._q(cypher, params, ro=True).result_set]

    def responders_for(self, user_id: str, lat: float, lng: float, emergency_type: str = "UNKNOWN") -> dict:
        """Graph traversal in Cypher: Zone <-JURISDICTION_OVER- Station <-ASSIGNED_TO- Patrol, plus nearest hospital."""
        try:
            zone = self.zone_for_point(lat, lng)
            station, station_reason = None, "no stations in graph"
            if zone:
                owned = self._rank(self._nodes("MATCH (s:Station)-[:JURISDICTION_OVER]->(z:Zone {id:$z}) RETURN s",
                                               {"z": zone["id"]}), lat, lng, "STATION")
                if owned:
                    station = owned[0]
                    station_reason = (f"graph: (Station)-[:JURISDICTION_OVER]->(Zone '{zone['name']}'); "
                                      f"nearest of {len(owned)}")
            if station is None:
                allst = self._rank(self._nodes("MATCH (s:Station) RETURN s"), lat, lng, "STATION")
                if allst:
                    station, station_reason = allst[0], "nearest station (no jurisdiction edge for this location)"
            patrol, patrol_reason = None, "no available patrol vehicle"
            if station:
                own = self._rank(self._nodes("MATCH (p:Patrol {status:'AVAILABLE'})-[:ASSIGNED_TO]->(s:Station {id:$s}) "
                                             "RETURN p", {"s": station["id"]}), lat, lng, "PATROL")
                if own:
                    patrol = own[0]
                    patrol_reason = f"graph: nearest AVAILABLE (Patrol)-[:ASSIGNED_TO]->({station['name']})"
            if patrol is None:
                anyp = self._rank(self._nodes("MATCH (p:Patrol {status:'AVAILABLE'}) RETURN p"), lat, lng, "PATROL")
                if anyp:
                    patrol, patrol_reason = anyp[0], "no available patrol at the responsible station; nearest AVAILABLE overall"
            hospitals = self._rank(self._nodes("MATCH (h:Hospital) RETURN h"), lat, lng, "HOSPITAL")
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
        except Exception as exc:
            self._fail(exc)
            return super().responders_for(user_id, lat, lng, emergency_type)

    # ------------------------------------------------ decisions, steps, evidence (the audit trail as a graph)
    def record_incident(self, state: dict, alert_id: Optional[str] = None) -> None:
        try:
            self._record_incident(state, alert_id)
        except Exception as exc:
            self._fail(exc)

    def _record_incident(self, state: dict, alert_id: Optional[str]) -> None:
        iid, uid = state["incident_id"], f"user:{state['user_id']}"
        trig, tri, lr = state.get("trigger") or {}, state.get("triage") or {}, state.get("location_risk") or {}
        zone, valid = lr.get("zone") or {}, bool(state.get("valid"))
        status = "DISPATCHED" if valid else ("CANCELLED" if trig.get("cancelled") else "REJECTED")
        self._q("MERGE (i:Incident {id:$id}) SET i += $p", {"id": iid, "p": _props({
            "status": status, "trigger_type": trig.get("type"), "emergency_type": tri.get("emergency_type"),
            "risk_level": state.get("risk_level"), "risk_score": state.get("risk_score"),
            "confidence": state.get("confidence"), "message": trig.get("message"), "lat": trig.get("lat"),
            "lng": trig.get("lng"), "ts": state.get("event_ts") or _now()})})
        self._q("MATCH (u:User {id:$u}), (i:Incident {id:$i}) MERGE (u)-[:RAISED]->(i)", {"u": uid, "i": iid})
        if zone.get("id"):
            self._q("MATCH (i:Incident {id:$i}), (z:Zone {id:$z}) MERGE (i)-[:OCCURRED_IN]->(z)", {"i": iid, "z": zone["id"]})
        if alert_id and self.has_node(alert_id):
            self._q("MATCH (i:Incident {id:$i}), (a:Alert {id:$a}) MERGE (i)-[:CREATED_ALERT]->(a)", {"i": iid, "a": alert_id})
        if trig.get("walk_id"):
            self._q("MERGE (w:Walk {id:$w}) SET w.status='ESCALATED'", {"w": trig["walk_id"]})
            self._q("MATCH (u:User {id:$u}), (w:Walk {id:$w}), (i:Incident {id:$i}) "
                    "MERGE (u)-[:WALKED]->(w) MERGE (i)-[:ESCALATED_FROM]->(w)", {"u": uid, "w": trig["walk_id"], "i": iid})

        logs = [l for l in state.get("audit_log", []) if l.get("incident_id") == iid]
        steps, hops, nxt, rets = [], [], [], []
        for idx, l in enumerate(logs):
            sid = f"step:{iid}:{idx}"
            steps.append(_props({"id": sid, "idx": idx, "step": l["step"], "thought": str(l["thought"])[:400],
                                 "action_taken": l["action_taken"], "risk_level": l["risk_level"],
                                 "confidence": l["confidence_score"], "ts": l["timestamp"], "latency_ms": l["latency_ms"]}))
            hops.append({"s": sid, "a": f"agent:{agent_for(l['step'])}"})
            if idx:
                nxt.append({"a": f"step:{iid}:{idx - 1}", "b": sid})
            rets += [{"s": sid, "n": r} for r in l.get("retrieved_nodes", [])[:12] if self.has_node(r)]
        if steps:
            self._q("UNWIND $x AS s MERGE (n:Step {id:s.id}) SET n += s", {"x": steps})
            self._q("MATCH (i:Incident {id:$i}) UNWIND $x AS e MATCH (s:Step {id:e.s}) MERGE (i)-[:HAS_STEP]->(s)",
                    {"i": iid, "x": hops})
            self._q("UNWIND $x AS e MATCH (s:Step {id:e.s}), (a:Agent {id:e.a}) MERGE (a)-[:PERFORMED]->(s)", {"x": hops})
        if nxt:
            self._q("UNWIND $x AS e MATCH (a:Step {id:e.a}), (b:Step {id:e.b}) MERGE (a)-[:NEXT]->(b)", {"x": nxt})
        if rets:
            self._q("UNWIND $x AS e MATCH (s:Step {id:e.s}), (n {id:e.n}) MERGE (s)-[:RETRIEVED]->(n)", {"x": rets})
        if not valid:
            return

        did = f"dec:{iid}"
        notes = next((l["details"].get("notes") for l in logs if l["step"] == "score_risk"), None) or []
        self._q("MERGE (d:Decision {id:$d}) SET d += $p", {"d": did, "p": _props({
            "level": state.get("risk_level"), "score": state.get("risk_score"), "confidence": state.get("confidence"),
            "rationale": " | ".join(notes), "policy": "risk-fusion v1"})})
        self._q("MATCH (i:Incident {id:$i}), (d:Decision {id:$d}) MERGE (i)-[:DECIDED]->(d)", {"i": iid, "d": did})
        ev = []
        if zone.get("id"):
            ev.append({"e": zone["id"], "why": f"location risk {lr.get('score', 0):.2f}: " + "; ".join(lr.get("factors", [])[:2])})
        for a in lr.get("nearby_alerts", []):
            ev.append({"e": a["id"], "why": f"{a['type']} alert {a['age_min'] / 1440:.1f} days ago, {a['distance_m']} m away"})
        for l in logs:
            if l["step"] == "score_risk":
                ev += [{"e": r, "why": "repeat trigger within 10 minutes"} for r in l["retrieved_nodes"]]
        ev += [{"e": ep.get("id"), "why": f"memory: {ep.get('match', 'related')} episode"} for ep in state.get("episodes", [])]
        resp = state.get("responders") or {}
        ev += [{"e": resp[k]["id"], "why": resp[k].get("selection_reason", "")} for k in ("station", "patrol", "hospital") if resp.get(k)]
        if state.get("profile"):
            ev.append({"e": uid, "why": "profile: " + ", ".join(list(state["profile"])[:5])})
        ev = [x for x in ev if x["e"]]
        if ev:
            self._q("UNWIND $x AS e MATCH (d:Decision {id:$d}), (n {id:e.e}) MERGE (d)-[b:BASED_ON]->(n) SET b.why=e.why",
                    {"x": ev, "d": did})
        receipts, acts, sends = state.get("receipts", []), [], []
        for i, a in enumerate(state.get("plan", [])):
            aid = f"act:{iid}:{a['order']}"
            acts.append(_props({"id": aid, "channel": a["channel"], "reason": a["reason"], "seq": a["order"],
                                "recipient": a["recipient_name"],
                                "status": (receipts[i] if i < len(receipts) else {}).get("status", "PLANNED")}))
            sends.append({"a": aid, "r": a["recipient_id"]})
        if acts:
            self._q("UNWIND $x AS s MERGE (n:Action {id:s.id}) SET n += s", {"x": acts})
            self._q("MATCH (d:Decision {id:$d}) UNWIND $x AS s MATCH (n:Action {id:s.id}) MERGE (d)-[:LED_TO]->(n)",
                    {"d": did, "x": acts})
            self._q("UNWIND $x AS e MATCH (a:Action {id:e.a}), (r {id:e.r}) MERGE (a)-[:SENT_TO]->(r)", {"x": sends})

    # ---------------------------------------------------------- multi-agent task board
    def create_task(self, tid: str, creator: str, kind: str, payload: dict) -> None:
        self._q("MERGE (a:Agent {id:$a}) SET a.name=$n "
                "CREATE (t:Task {id:$t, kind:$k, status:'PENDING', payload:$p, created_at:$ts}) CREATE (a)-[:CREATED]->(t)",
                {"a": f"agent:{creator}", "n": creator, "t": tid, "k": kind,
                 "p": json.dumps(payload, default=str), "ts": _now()})

    def claim_task(self, agent_name: str, kind: str):
        pending = self._q("MATCH (t:Task {kind:$k, status:'PENDING'}) RETURN count(t)", {"k": kind}, ro=True).result_set
        if not pending or not pending[0][0]:
            return None
        rows = self._q("MATCH (t:Task {kind:$k, status:'PENDING'}) WITH t ORDER BY t.created_at LIMIT 1 "
                       "SET t.status='CLAIMED', t.claimed_at=$ts RETURN t.id, t.payload",
                       {"k": kind, "ts": _now()}).result_set
        if not rows:
            return None
        tid, payload = rows[0]
        self._q("MATCH (t:Task {id:$t}), (a:Agent {id:$a}) MERGE (a)-[:CLAIMED]->(t)", {"t": tid, "a": f"agent:{agent_name}"})
        return tid, json.loads(payload)

    def complete_task(self, tid: str, agent_name: str, incident_id: Optional[str]) -> None:
        self._q("MATCH (t:Task {id:$t}), (a:Agent {id:$a}) SET t.status='DONE', t.done_at=$ts MERGE (a)-[:COMPLETED]->(t)",
                {"t": tid, "a": f"agent:{agent_name}", "ts": _now()})
        if incident_id:
            self._q("MATCH (t:Task {id:$t}), (i:Incident {id:$i}) MERGE (t)-[:PRODUCED]->(i)", {"t": tid, "i": incident_id})

    def fail_task(self, tid: str, agent_name: str, error: str) -> None:
        self._w("MATCH (t:Task {id:$t}) SET t.status='FAILED', t.error=$e", {"t": tid, "e": error[:300]})


# --------------------------------------------------------------------- read helpers (also used by the page)
def recent_incidents(fg, n: int = 15) -> list[dict]:
    rows = fg.ro_query("MATCH (i:Incident) RETURN i.id, i.risk_level, i.emergency_type, i.status, i.trigger_type, i.ts "
                       "ORDER BY i.ts DESC LIMIT $n", {"n": n}).result_set
    return [{"id": r[0], "risk": r[1], "type": r[2], "status": r[3], "trigger": r[4], "ts": r[5]} for r in rows]


def incident_path(fg, iid: str) -> dict:
    q_dec = ("MATCH (i:Incident {id:$i})-[:DECIDED]->(d:Decision) RETURN d.level, d.score, d.confidence, d.rationale")
    q_ev = ("MATCH (i:Incident {id:$i})-[:DECIDED]->(d:Decision)-[b:BASED_ON]->(e) "
            "RETURN e.id, coalesce(e.name, e.text, e.id), labels(e), b.why")
    q_act = ("MATCH (i:Incident {id:$i})-[:DECIDED]->(d:Decision)-[:LED_TO]->(a:Action) OPTIONAL MATCH (a)-[:SENT_TO]->(r) "
             "RETURN a.id, a.channel, a.reason, a.status, a.seq, r.id, r.name, labels(r) ORDER BY a.seq")
    dec, ev, act = (fg.ro_query(q, {"i": iid}).result_set for q in (q_dec, q_ev, q_act))
    return {"decision": ({"level": dec[0][0], "score": dec[0][1], "confidence": dec[0][2], "rationale": dec[0][3]} if dec else None),
            "evidence": [{"id": r[0], "name": r[1], "label": (r[2] or [""])[0], "why": r[3]} for r in ev],
            "actions": [{"id": r[0], "channel": r[1], "reason": r[2], "status": r[3], "seq": r[4], "recipient_id": r[5],
                         "recipient": r[6], "recipient_label": (r[7] or [""])[0]} for r in act],
            "cypher": [q_dec, q_ev, q_act]}


def incident_steps(fg, iid: str) -> list[dict]:
    rows = fg.ro_query("MATCH (i:Incident {id:$i})-[:HAS_STEP]->(s:Step) OPTIONAL MATCH (a:Agent)-[:PERFORMED]->(s) "
                       "RETURN s.idx, s.step, s.thought, s.risk_level, s.confidence, a.name ORDER BY s.idx", {"i": iid}).result_set
    return [{"#": r[0], "agent": r[5], "step": r[1], "risk": r[3], "confidence": r[4], "reasoning": r[2]} for r in rows]


def agent_activity(fg, n: int = 15) -> list[dict]:
    rows = fg.ro_query("MATCH (c:Agent)-[:CREATED]->(t:Task) OPTIONAL MATCH (w:Agent)-[:CLAIMED]->(t) "
                       "OPTIONAL MATCH (x:Agent)-[:COMPLETED]->(t) OPTIONAL MATCH (t)-[:PRODUCED]->(i:Incident) "
                       "RETURN t.id, t.kind, t.status, c.name, w.name, x.name, i.id, t.created_at "
                       "ORDER BY t.created_at DESC LIMIT $n", {"n": n}).result_set
    return [{"task": r[0], "kind": r[1], "status": r[2], "created by": r[3], "claimed by": r[4],
             "completed by": r[5], "incident": r[6], "created": r[7]} for r in rows]


def agent_topology(fg) -> list[dict]:
    rows = fg.ro_query("MATCH (a:Agent)-[:HANDS_OFF_TO]->(b:Agent) RETURN a.name, b.name").result_set
    return [{"from": r[0], "hands off to": r[1]} for r in rows]