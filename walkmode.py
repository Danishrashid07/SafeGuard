"""walkmode.py: Walk Me Home session state, the dead-man's-switch watcher (background thread), walk memory."""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import tracking
from graph_store import risk_level_from_score

SESSION = Path("data/walk_session.json")
INCIDENT = Path("data/walk_incident.json")
LOCK = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


_parse = parse


def load() -> Optional[dict]:
    with LOCK:
        if not SESSION.exists():
            return None
        try:
            return json.loads(SESSION.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None


def _save(s: dict) -> None:
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(SESSION)


def clear() -> None:
    with LOCK:
        for p in (SESSION, INCIDENT):
            p.unlink(missing_ok=True)


def _event(s: dict, msg: str) -> None:
    s.setdefault("events", []).append({"ts": _iso(_now()), "msg": msg})


def _audit(s: dict, step: str, thought: str, action: str, nodes, risk: str, conf: float, **details) -> None:
    s.setdefault("audit", []).append({
        "step": step, "thought": thought, "action_taken": action, "retrieved_nodes": list(nodes),
        "risk_level": risk, "confidence_score": round(float(conf), 3), "incident_id": None,
        "timestamp": _now().isoformat(timespec="milliseconds"), "latency_ms": 0, "details": details})


def _refresh_due(s: dict) -> None:
    s["due_at"] = _iso(min(_parse(s["next_checkin_due"]), _parse(s["arrival_deadline"])))


# ------------------------------------------------------------------ memory
def walk_history(memory, user_id: str) -> dict:
    return (memory.get_profile(user_id) or {}).get("walk_history", {})


def expected_minutes(memory, user_id: str, dest_name: str, route_eta_min: float) -> tuple[float, str]:
    h = walk_history(memory, user_id).get(f"to {dest_name}")
    if h and h.get("count", 0) >= 1:
        return max(route_eta_min * 0.8, h["avg_min"]), \
            f"; usual time to {dest_name} is {h['avg_min']:.0f} min over {h['count']} walk(s)"
    return route_eta_min, ""


def record_walk(memory, user_id: str, dest_name: str, minutes: float) -> None:
    hist = walk_history(memory, user_id)
    h = hist.get(f"to {dest_name}", {"count": 0, "avg_min": 0.0})
    n = h["count"]
    hist[f"to {dest_name}"] = {"count": n + 1, "avg_min": round((h["avg_min"] * n + minutes) / (n + 1), 1)}
    memory.update_profile(user_id, {"walk_history": hist})
    memory.add_episode(user_id, f"Walked to {dest_name} safely in {minutes:.0f} min on the safe route", {"walk": True})


# ----------------------------------------------------------------- session
def start_walk(user_id, user_name, dest_name, plan, expected_min, local_hour, interval_s, grace_s,
               self_topic=None, memory_note="") -> dict:
    now = _now()
    arrival_s = int(expected_min * 60 * 1.25 + 120)
    s = {"walk_id": "walk-" + uuid.uuid4().hex[:8], "user_id": user_id, "user_name": user_name,
         "dest_name": dest_name, "route": plan["waypoints"], "direct_route": plan["direct_waypoints"],
         "idx": 0, "position": plan["waypoints"][0], "status": "ACTIVE", "started_at": _iso(now),
         "interval_s": interval_s, "grace_s": grace_s, "arrival_deadline": _iso(now + timedelta(seconds=arrival_s)),
         "next_checkin_due": _iso(now + timedelta(seconds=interval_s)), "last_checkin": _iso(now),
         "local_hour": local_hour, "self_topic": self_topic, "expected_min": round(expected_min, 1),
         "events": [], "audit": []}
    _refresh_due(s)
    safe = plan["safe"]
    _audit(s, "plan_safe_route", plan["summary"], "graph risk lattice + Dijkstra route planning",
           [p["id"] for p in safe["police_posts"]] + [z["id"] for z in safe["zones"]],
           risk_level_from_score(safe["exposure"]), 0.8, safety_bias=plan["safety_bias"])
    _audit(s, "start_walk",
           f"Dead-man's switch armed: check in every {interval_s} s, grace {grace_s} s, arrival deadline in "
           f"{arrival_s // 60} min (expected {expected_min:.0f} min{memory_note}).",
           "arm timers", [], "LOW", 0.9)
    _event(s, f"Walk to {dest_name} started; first check-in due in {interval_s} s")
    with LOCK:
        INCIDENT.unlink(missing_ok=True)
        _save(s)
    return s


def _mutate(fn) -> Optional[dict]:
    with LOCK:
        s = load()
        if not s or s["status"] not in ("ACTIVE", "WARNING"):
            return s
        fn(s)
        _save(s)
        return s


def check_in() -> Optional[dict]:
    def f(s):
        was = s["status"]
        now = _now()
        s["status"], s["last_checkin"] = "ACTIVE", _iso(now)
        s["next_checkin_due"] = _iso(now + timedelta(seconds=s["interval_s"]))
        _refresh_due(s)
        _event(s, "Check-in received" + (" during grace period: escalation cancelled" if was == "WARNING" else ""))
        _audit(s, "check_in", "User confirmed they are OK" + (" before the grace period ended; escalation cancelled."
               if was == "WARNING" else "; next check-in timer reset."), "reset dead-man's switch", [], "LOW", 0.95)
    return _mutate(f)


def advance(steps: int = 1) -> Optional[dict]:
    def f(s):
        s["idx"] = min(len(s["route"]) - 1, s["idx"] + steps)
        s["position"] = s["route"][s["idx"]]
        _event(s, f"Moved along route ({s['idx']}/{len(s['route']) - 1})")
    return _mutate(f)


def set_position(lat: float, lng: float) -> Optional[dict]:
    def f(s):
        s["position"] = [lat, lng]
        _event(s, "Position updated from GPS")
    return _mutate(f)


def extend(minutes: int = 5) -> Optional[dict]:
    def f(s):
        s["arrival_deadline"] = _iso(_parse(s["arrival_deadline"]) + timedelta(minutes=minutes))
        _refresh_due(s)
        _event(s, f"Arrival deadline extended by {minutes} min")
        _audit(s, "extend_deadline", f"User extended the arrival deadline by {minutes} min.", "extend", [], "LOW", 0.9)
    return _mutate(f)


def arrive() -> Optional[dict]:
    def f(s):
        s["status"], s["finished_at"] = "COMPLETED", _iso(_now())
        _event(s, "Arrived safely: dead-man's switch disarmed")
        _audit(s, "arrived", "User marked arrival; walk completed safely.", "disarm", [], "LOW", 0.95)
    return _mutate(f)


def cancel() -> Optional[dict]:
    def f(s):
        s["status"], s["finished_at"] = "CANCELLED", _iso(_now())
        _event(s, "Walk cancelled by user")
    return _mutate(f)


# ----------------------------------------------------------------- watcher
class WalkWatcher(threading.Thread):
    """Polls the walk session every second. Runs inside the Streamlit server process, so escalation works even if
    the browser tab is closed (as long as the server keeps running)."""

    def __init__(self, agent, graph, poll_s: float = 1.0):
        super().__init__(daemon=True, name="walk-watcher")
        self.agent, self.graph, self.poll_s = agent, graph, poll_s

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            time.sleep(self.poll_s)

    def tick(self) -> None:
        now = _now()
        warn = escalate = None
        with LOCK:
            s = load()
            if not s or s["status"] not in ("ACTIVE", "WARNING"):
                return
            if s["status"] == "ACTIVE" and now >= _parse(s["due_at"]):
                s["status"], s["warning_at"] = "WARNING", _iso(now)
                _event(s, f"Missed check-in: {s['grace_s']} s grace period started")
                _audit(s, "missed_checkin", f"No check-in by the due time; warning the user and starting a "
                       f"{s['grace_s']} s grace period.", "push warning + start grace timer", [], "MEDIUM", 0.9)
                _save(s)
                warn = dict(s)
            elif s["status"] == "WARNING" and now >= _parse(s["warning_at"]) + timedelta(seconds=s["grace_s"]):
                s["status"] = "ESCALATING"
                _event(s, "Grace period expired with no check-in: escalating automatically")
                _save(s)
                escalate = dict(s)
        if warn:
            self._warn_user(warn)
        if escalate:
            self._escalate(escalate)

    @staticmethod
    def _warn_user(s: dict) -> None:
        if s.get("self_topic"):
            try:
                tracking._push(s["self_topic"], "SAFEGUARD: CHECK IN NOW",
                               f"Missed check-in on your walk to {s['dest_name']}. Open SafeGuard and tap I'm OK "
                               f"within {s['grace_s']} s or your contacts will be alerted.", None, "5", "warning")
            except Exception:
                pass

    def _escalate(self, s: dict) -> None:
        lat, lng = s["position"]
        trigger = {"type": "dead_mans_switch", "lat": lat, "lng": lng, "accuracy_m": 25, "local_hour": s["local_hour"],"walk_id": s["walk_id"],
                   "message": f"Missed check-in during Walk Me Home to {s['dest_name']} (user unresponsive)."}
        try:
            crew = getattr(self.agent, "crew", None)
            final = (crew.run_incident("GuardianAgent", s["user_id"], trigger) if crew
                     else self.agent.handle_incident(s["user_id"], trigger))
        except Exception as exc:
            traceback.print_exc()
            with LOCK:
                cur = load()
                cur["status"] = "ESCALATION_FAILED"
                _event(cur, f"Escalation FAILED: {exc}")
                _save(cur)
            return
        logs = final.pop("audit_log", [])
        if not final.get("valid"):
            with LOCK:
                cur = load()
                cur["status"] = "ESCALATION_FAILED"
                _event(cur, "Escalation rejected: " + (logs[0]["thought"] if logs else "unknown reason"))
                _save(cur)
            return
        INCIDENT.write_text(json.dumps({"walk_id": s["walk_id"], "final": final, "logs": logs, "trigger": trigger,
                                        "event_ts": logs[0]["timestamp"] if logs else None}, default=str),
                            encoding="utf-8")
        tracking.start_session(final["incident_id"], s["user_name"], final["risk_level"], lat, lng, 25)
        with LOCK:
            cur = load()
            cur.update(status="ESCALATED", incident_id=final["incident_id"], risk_level=final["risk_level"])
            _event(cur, f"AUTO-ESCALATED: risk {final['risk_level']}, {len(final.get('plan', []))} actions dispatched")
            _audit(cur, "auto_escalate",
                   f"No check-in within the grace period. Last known position {lat:.5f}, {lng:.5f}. Triggered a "
                   f"dead_mans_switch incident ({final['incident_id']}) with no user action.",
                   "agent.handle_incident(dead_mans_switch)", final.get("highlight_nodes", []),
                   final["risk_level"], final["confidence"])
            _save(cur)