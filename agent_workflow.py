"""agent_workflow.py: SafeGuard incident pipeline (LangGraph). Deterministic dispatch core; Claude assists only
with free-text triage and the human-readable explanation, and the pipeline works if the API is unavailable."""
from __future__ import annotations

import json
import operator
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Iterator, Optional, Protocol, TypedDict

from llm import LLM
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from graph_store import RISK_LEVELS, SafetyGraph, max_risk, parse_ts, risk_level_from_score

DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
DEFAULT_FAST_MODEL = os.getenv("ANTHROPIC_FAST_MODEL", "claude-haiku-5-5")
TRIGGER_TYPES = ("triple_tap", "panic_button", "voice_keyword", "manual", "dead_mans_switch")
EMERGENCY_TYPES = ("HARASSMENT", "ASSAULT", "ROBBERY", "MEDICAL", "ACCIDENT", "STALKING", "UNKNOWN")
SEVERITY_SCORE = {"LOW": 0.20, "MEDIUM": 0.45, "HIGH": 0.70, "CRITICAL": 0.95}
TRIGGER_FLOOR = 0.60  # a deliberate trigger is never below HIGH


# ------------------------------------------------------------------ interfaces
class MemoryLike(Protocol):
    """Implemented by memory_store.py (Day 2)."""
    def get_profile(self, user_id: str) -> dict: ...  # medical_notes, preferred_contacts, do_not_notify, ...
    def recall(self, user_id: str, query: str, k: int = 3) -> list[dict]: ...  # [{"id","text"}]
    def update_profile(self, user_id: str, updates: dict) -> dict: ...
    def add_episode(self, user_id: str, text: str, metadata: Optional[dict] = None) -> str: ...


class Dispatcher(Protocol):
    def send(self, action: dict) -> dict: ...


class SimulatedDispatcher:
    """Demo dispatcher. Records every action in an outbox and NEVER contacts real people or services."""

    def __init__(self) -> None:
        self.outbox: list[dict] = []
        self._lock = threading.Lock()

    def send(self, action: dict) -> dict:
        receipt = {
            "status": "SIMULATED_DELIVERED", "simulated": True, "message_id": uuid.uuid4().hex[:10],
            "channel": action["channel"], "recipient_id": action["recipient_id"],
            "recipient_name": action["recipient_name"], "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "payload": action["payload"],
        }
        with self._lock:
            self.outbox.append(receipt)
        return receipt


# ------------------------------------------------------------- pure helpers
def detect_triple_tap(taps: list[float], window_s: float = 2.0) -> bool:
    """True if any 3 consecutive taps (epoch seconds) fall within `window_s`. Shared with app.py."""
    t = sorted(float(x) for x in taps)
    return any(t[i + 2] - t[i] <= window_s for i in range(len(t) - 2))


_KEYWORDS: list[tuple[str, str, list[str]]] = [  # (severity, type, regex fragments) -- evaluated worst-first
    ("CRITICAL", "ASSAULT", [r"\bgun\b", r"\bknife\b", r"\bweapon", r"\barmed\b", r"dragg", r"kidnap", r"strangl"]),
    ("CRITICAL", "MEDICAL", [r"unconscious", r"not breathing", r"can'?t breathe", r"bleeding (a lot|heavily)", r"seizure", r"overdos"]),
    ("HIGH", "ASSAULT", [r"attack", r"assault", r"hit me", r"punch", r"grabbed"]),
    ("HIGH", "ROBBERY", [r"robbed", r"mugg", r"snatch", r"stole"]),
    ("HIGH", "STALKING", [r"follow(ing|ed) me", r"stalk", r"someone is behind"]),
    ("HIGH", "MEDICAL", [r"chest pain", r"heart", r"fainted", r"collapsed", r"allergic"]),
    ("HIGH", "ACCIDENT", [r"accident", r"crash", r"hit by"]),
    ("MEDIUM", "HARASSMENT", [r"harass", r"catcall", r"uncomfortable", r"threaten", r"groped", r"touch(ed|ing) me"]),
    ("MEDIUM", "UNKNOWN", [r"scared", r"lost", r"unsafe", r"help"]),
]


def keyword_triage(message: str) -> dict:
    text = message.lower()
    for sev, etype, patterns in _KEYWORDS:
        hits = [p for p in patterns if re.search(p, text)]
        if hits:
            return {"severity": sev, "emergency_type": etype, "signals": hits[:3]}
    return {"severity": "LOW", "emergency_type": "UNKNOWN", "signals": []}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _extract_json(text: str) -> Optional[dict]:
    text = text.strip()
    candidates = [text]
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def make_checkpointer(db_path: str):
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return SqliteSaver(sqlite3.connect(db_path, check_same_thread=False)), "sqlite"
    except ImportError:
        return MemorySaver(), "memory"


# --------------------------------------------------------------------- state
class SafeGuardState(TypedDict, total=False):
    user_id: str
    thread_id: str
    incident_id: str
    trigger: dict
    audit_log: Annotated[list, operator.add]  # persisted across incidents; filter by incident_id
    # per-incident (reset in trigger_intake)
    valid: bool
    event_ts: str
    profile: dict
    episodes: list
    location_risk: dict
    triage: dict
    risk_score: float
    risk_level: str
    risk_confidence: float
    responders: dict
    plan: list
    dispatch_confidence: float
    receipts: list
    explanation: dict
    confidence: float


TRIAGE_SYSTEM = """You triage a personal-safety emergency message. Return ONLY a JSON object:
{"emergency_type": "HARASSMENT"|"ASSAULT"|"ROBBERY"|"MEDICAL"|"ACCIDENT"|"STALKING"|"UNKNOWN",
 "severity": "LOW"|"MEDIUM"|"HIGH"|"CRITICAL",
 "signals": [str],      // short cues from the message that justify the severity
 "confidence": float}   // 0-1
When unsure, choose the HIGHER severity."""

EXPLAIN_SYSTEM = """You write the explanation for a personal-safety dispatch decision. Use ONLY the facts provided; never invent
responders, times or details. Return ONLY a JSON object:
{"summary": str,                  // 2-3 sentences for an auditor: what happened, risk level and why
 "responder_rationale": [str],    // one line per action: who was alerted and why
 "user_message": str}             // calm, short message to the user (max 2 sentences) saying help is being notified"""


class SafeGuardAgent:
    
    def __init__(self, graph: SafetyGraph, memory: Optional[MemoryLike] = None,
                 dispatcher: Optional[Dispatcher] = None, db_path: str = "data/safeguard_checkpoints.sqlite",
                 model: str = DEFAULT_MODEL, fast_model: str = DEFAULT_FAST_MODEL,
                 llm: Optional[LLM] = None):
        self.graph = graph
        self.memory = memory
        self.dispatcher: Dispatcher = dispatcher or SimulatedDispatcher()
        self.model, self.fast_model = model, fast_model
        self.llm = llm or LLM()
        self.checkpointer, self.checkpointer_kind = make_checkpointer(db_path)
        self.app = self._build().compile(checkpointer=self.checkpointer)

    # ---------------------------------------------------------------- helpers
    def _llm_json(self, system: str, user: str, model: str = "", max_tokens: int = 700) -> Optional[dict]:
        raw = self.llm.complete(system, user, max_tokens)
        return _extract_json(raw) if raw else None

    @staticmethod
    
    def _log(state: SafeGuardState, step: str, thought: str, action: str, nodes: list, risk: str,
             confidence: float, t0: float, **details: Any) -> dict:
        return {"step": step, "thought": thought, "action_taken": action, "retrieved_nodes": list(nodes),
                "risk_level": risk, "confidence_score": round(_clamp(confidence), 3),
                "incident_id": state.get("incident_id"), "timestamp": _now(),
                "latency_ms": int((time.perf_counter() - t0) * 1000), "details": details}

    # ------------------------------------------------------------------ nodes
    def trigger_intake(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        trig = state.get("trigger", {})
        reset = {"profile": {}, "episodes": [], "location_risk": {}, "triage": {}, "risk_score": 0.0,
                 "responders": {}, "plan": [], "receipts": [], "explanation": {}, "confidence": 0.0,
                 "dispatch_confidence": 0.0, "risk_confidence": 0.0,
                 "event_ts": parse_ts(trig.get("timestamp")).isoformat()}
        problems = []
        ttype = trig.get("type")
        if ttype not in TRIGGER_TYPES:
            problems.append(f"unknown trigger type '{ttype}'")
        if not isinstance(trig.get("lat"), (int, float)) or not isinstance(trig.get("lng"), (int, float)) \
                or not (-90 <= trig["lat"] <= 90 and -180 <= trig["lng"] <= 180):
            problems.append("missing or invalid coordinates")
        if f"user:{state['user_id']}" not in self.graph.g:
            problems.append(f"unknown user '{state['user_id']}'")
        if trig.get("cancelled"):
            problems.append("cancelled by user during the grace window (false-alarm protection)")
        if ttype == "triple_tap" and not trig.get("cancelled") and not detect_triple_tap(trig.get("taps", [])):
            problems.append("fewer than 3 taps within 2.0 s: pattern not confirmed")
        valid = not problems
        if valid:
            thought = (f"Trigger '{ttype}' verified" +
                       (f" ({len(trig.get('taps', []))} taps, 3 within 2.0 s)" if ttype == "triple_tap" else "") +
                       f"; provisional risk HIGH until scored.")
        else:
            thought = "Trigger rejected: " + "; ".join(problems)
        log = self._log(state, "trigger_intake", thought, "validate trigger" if valid else "reject trigger / no dispatch",
                        [f"user:{state['user_id']}"], "HIGH" if valid else "LOW", 0.98 if valid else 0.9, t0,
                        trigger={k: v for k, v in trig.items() if k != "taps"}, problems=problems)
        return {**reset, "valid": valid, "risk_level": "HIGH" if valid else "LOW", "audit_log": [log]}

    @staticmethod
    def route_after_intake(state: SafeGuardState) -> str:
        return "assess_context" if state.get("valid") else "finalize"

   
    def assess_context(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        uid, trig = state["user_id"], state["trigger"]

        loc_id = self.graph.set_user_location(
            uid,
            trig["lat"],
            trig["lng"],
            trig.get("accuracy_m"),
            state["event_ts"],
        )

        lr = self.graph.location_risk(
            trig["lat"],
            trig["lng"],
            trig.get("local_hour"),
            state["event_ts"],
        )

        profile, episodes = {}, []

        if self.memory is not None:
            profile = self.memory.get_profile(uid) or {}
            episodes = self.memory.recall(
                uid,
                trig.get("message") or "emergency near current location",
                k=3,
                zone_id=(lr["zone"] or {}).get("id"),
            ) or []

        # Preserve the provisional risk level from trigger_intake.
        # The final risk level is calculated later by score_risk().
        level = state.get("risk_level", "HIGH")

        log = self._log(
            state,
            "assess_context",
            f"Located user in "
            + (
                f"zone '{lr['zone']['name']}'"
                if lr["zone"]
                else "an unmapped area"
            )
            + f"; location risk {lr['score']:.2f}. "
            + "; ".join(lr["factors"])
            + f". Memory: {len(profile)} profile fields, "
            f"{len(episodes)} related episodes.",
            "graph.set_user_location() + graph.location_risk() "
            "+ memory.get_profile()/recall()",
            [loc_id]
            + ([lr["zone"]["id"]] if lr["zone"] else [])
            + [a["id"] for a in lr["nearby_alerts"]],
            level,
            0.9 if lr["zone_found"] else 0.6,
            t0,
            location_risk=lr,
            profile_keys=list(profile),
            episodes=[e.get("text", "")[:120] for e in episodes],
            cypher=self.graph.drain_trace(),
        )

        return {
            "profile": profile,
            "episodes": episodes,
            "location_risk": lr,
            "risk_level": level,
            "audit_log": [log],
        }


    def triage(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        message = (state["trigger"].get("message") or "").strip()
        if not message:
            result = {"emergency_type": "UNKNOWN", "severity": "HIGH", "signals": ["silent panic: no message"],
                      "confidence": 0.55, "source": "default (silent trigger)"}
        else:
            kw = keyword_triage(message)
            llm = self._llm_json(TRIAGE_SYSTEM, f"Message: {message}", self.fast_model, 300)
            llm_ok = bool(llm) and llm.get("severity") in RISK_LEVELS and llm.get("emergency_type") in EMERGENCY_TYPES
            severity, etype, signals = kw["severity"], kw["emergency_type"], list(kw["signals"])
            conf = 0.7 if kw["signals"] else 0.5
            source = "keyword rules"
            if llm_ok:
                severity = max_risk(kw["severity"], llm["severity"])  # the LLM can raise severity, never lower it
                etype = llm["emergency_type"] if llm["emergency_type"] != "UNKNOWN" else etype
                signals += [str(s) for s in llm.get("signals", [])][:3]
                try:
                    conf = _clamp(0.5 * conf + 0.5 * float(llm.get("confidence", 0.7)))
                except (TypeError, ValueError):
                    pass
                source = "keyword rules + LLM"
            result = {"emergency_type": etype, "severity": severity, "signals": signals, "confidence": conf, "source": source}
        level = max_risk(state["risk_level"], result["severity"])
        log = self._log(state, "triage",
                        f"Classified as {result['emergency_type']} / severity {result['severity']} via {result['source']}. "
                        f"Signals: {result['signals'] or 'none'}.",
                        "keyword rules + LLM triage" if message else "default silent-panic triage", [], level,
                        result["confidence"], t0, triage=result)
        return {"triage": result, "risk_level": level, "audit_log": [log]}

    def score_risk(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        tri, lr, uid = state["triage"], state["location_risk"], state["user_id"]
        s_tri, s_loc = SEVERITY_SCORE[tri["severity"]], lr["score"]
        score = 0.65 * s_tri + 0.35 * s_loc
        notes = [f"0.65 x triage {s_tri:.2f} + 0.35 x location {s_loc:.2f} = {score:.2f}"]
        repeats = self.graph.recent_alerts(user_id=uid, within_minutes=10, now=state["event_ts"])
        if repeats:
            score += 0.20
            notes.append(f"+0.20 repeat trigger ({len(repeats)} alert(s) from this user in the last 10 min)")
        if score < TRIGGER_FLOOR:
            score = TRIGGER_FLOOR
            notes.append(f"raised to trigger floor {TRIGGER_FLOOR:.2f}")
        if tri["severity"] == "CRITICAL" and score < 0.85:
            score = 0.85
            notes.append("raised to 0.85: explicit CRITICAL signal")
        score = _clamp(score)
        level = risk_level_from_score(score)
        conf = _clamp(0.45 + 0.20 * (state["trigger"].get("accuracy_m") is not None)
                      + 0.15 * lr["zone_found"] + 0.20 * tri["confidence"])
        log = self._log(state, "score_risk", f"Risk {level} (score {score:.2f}). " + " | ".join(notes),
                        "deterministic risk fusion (triage + location + repeat-trigger + floor)",
                        [a["id"] for a in repeats], level, conf, t0, score=round(score, 3), notes=notes)
        return {"risk_score": round(score, 3), "risk_level": level, "risk_confidence": conf, "audit_log": [log]}

    def plan_dispatch(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        uid, trig, tri, level = state["user_id"], state["trigger"], state["triage"], state["risk_level"]
        etype, profile = tri["emergency_type"], state.get("profile", {})
        user = self.graph.get(f"user:{uid}")
        resp = self.graph.responders_for(uid, trig["lat"], trig["lng"], etype)
        contacts = self._order_contacts(resp["contacts"], profile)
        link = f"https://maps.google.com/?q={trig['lat']},{trig['lng']}"
        base = {"incident_id": state["incident_id"], "user_name": user["name"], "user_phone": user.get("phone", ""),
                "location": {"lat": trig["lat"], "lng": trig["lng"], "accuracy_m": trig.get("accuracy_m"), "map_link": link},
                "emergency_type": etype, "risk_level": level, "user_message": trig.get("message") or "(silent panic)",
                "triggered_at": state["event_ts"]}
        actions: list[dict] = []

        def add(channel, rid, rname, payload, reason, **extra):
            actions.append({"order": len(actions) + 1, "channel": channel, "recipient_id": rid, "recipient_name": rname,
                            "payload": payload, "reason": reason, **extra})

        if level in ("CRITICAL", "HIGH"):
            st, pa = resp["station"], resp["patrol"]
            if st:
                add("POLICE_DISPATCH", st["id"], st["name"], {**base, "zone": (resp["zone"] or {}).get("name")},
                    f"{level} risk; {st['name']} {st['selection_reason']}; {st['distance_m']} m away")
            if pa:
                add("PATROL_ASSIGN", pa["id"], pa["name"], {**base, "eta_min": pa["eta_min"]},
                    f"{pa['selection_reason']}; {pa['distance_m']} m away, ETA ~{pa['eta_min']} min")
        medical_need = etype in ("MEDICAL", "ACCIDENT") or (level == "CRITICAL" and etype in ("ASSAULT", "UNKNOWN"))
        if medical_need and resp["hospital"]:
            h = resp["hospital"]
            add("EMS_NOTIFY", h["id"], h["name"],
                {**base, "medical_notes": profile.get("medical_notes", "none on file"), "eta_min": h["eta_min"]},
                f"medical need for {etype} at {level}; {h['selection_reason']}; {h['distance_m']} m away "
                f"(medical notes shared with EMS only)")
        n_contacts = {"CRITICAL": len(contacts), "HIGH": 2, "MEDIUM": 1, "LOW": 0}[level]
        for i, c in enumerate(contacts[:n_contacts]):
            msg = (f"{user['name']} triggered a SafeGuard emergency alert ({etype.lower()}, risk {level}). "
                   f"Live location: {link}")
            add("SMS_CONTACT", c["id"], c["name"], {"to": c["phone"], "text": msg, "incident_id": state["incident_id"]},
                f"priority-{c['priority']} contact ({c['relation']}); {n_contacts} contact(s) notified at {level}")
            if i == 0 and level in ("CRITICAL", "HIGH") and "CALL" in c.get("channels", []):
                add("CALL_CONTACT", c["id"], c["name"], {"to": c["phone"], "incident_id": state["incident_id"]},
                    "first contact is also called at HIGH/CRITICAL")
        escalate = {"MEDIUM": 120, "LOW": 300}.get(level)
        add("PUSH_USER_CHECKIN", f"user:{uid}", user["name"],
            {"text": "SafeGuard: are you OK? Reply to cancel escalation.", "incident_id": state["incident_id"]},
            "confirm with the user" + (f"; auto-escalate if no reply in {escalate}s" if escalate else ""),
            escalate_after_s=escalate)

        found = sum(1 for k in ("station", "patrol") if resp[k]) + (1 if contacts else 0)
        dconf = {3: 0.95, 2: 0.8, 1: 0.55}.get(found, 0.3) if level in ("CRITICAL", "HIGH") else 0.85
        nodes = [x["id"] for x in (resp["station"], resp["patrol"], resp["hospital"]) if x] + [c["id"] for c in contacts[:n_contacts]]
        log = self._log(
            state, "plan_dispatch",
            f"Risk {level}: planned {len(actions)} action(s). " +
            " ".join(f"{k}: {resp[k]['name']} ({resp[k]['selection_reason']})." for k in ("station", "patrol", "hospital") if resp[k]),
            "graph.responders_for() + dispatch policy table", nodes, level, dconf, t0,
            actions=[{k: a[k] for k in ("order", "channel", "recipient_name", "reason")} for a in actions],
            cypher=self.graph.drain_trace())
        return {"responders": resp, "plan": actions, "dispatch_confidence": dconf, "audit_log": [log]}

    @staticmethod
    def _order_contacts(contacts: list[dict], profile: dict) -> list[dict]:
        skip = {str(x).lower() for x in profile.get("do_not_notify", [])}
        pref = [str(x).lower() for x in profile.get("preferred_contacts", [])]
        kept = [c for c in contacts if c["name"].lower() not in skip and c["id"].lower() not in skip]
        return sorted(kept, key=lambda c: (pref.index(c["name"].lower()) if c["name"].lower() in pref else len(pref), c["priority"]))

    def execute_dispatch(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        receipts, logs = [], []
        for a in sorted(state["plan"], key=lambda x: x["order"]):
            ta = time.perf_counter()
            try:
                r = self.dispatcher.send(a)
                ok, thought = True, f"{a['channel']} -> {a['recipient_name']}: {r['status']} (id {r['message_id']})."
            except Exception as exc:  # one failed channel must not block the others
                r, ok, thought = {"status": "FAILED", "error": str(exc), "channel": a["channel"]}, False, \
                    f"{a['channel']} -> {a['recipient_name']} FAILED: {exc}"
            receipts.append(r)
            logs.append(self._log(state, f"dispatch:{a['channel']}", thought + " Reason: " + a["reason"],
                                  f"dispatcher.send({a['channel']})", [a["recipient_id"]], state["risk_level"],
                                  0.95 if ok else 0.1, ta, receipt={k: v for k, v in r.items() if k != "payload"}))
        delivered = sum(1 for r in receipts if r.get("status") != "FAILED")
        logs.append(self._log(state, "execute_dispatch", f"{delivered}/{len(receipts)} actions delivered.",
                              "dispatch summary", [], state["risk_level"], delivered / len(receipts) if receipts else 1.0, t0))
        return {"receipts": receipts, "audit_log": logs}

    def explain(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        facts = {"user": self.graph.get(f"user:{state['user_id']}")["name"], "risk_level": state["risk_level"],
                 "risk_score": state["risk_score"], "triage": state["triage"],
                 "location_factors": state["location_risk"]["factors"],
                 "actions": [{"channel": a["channel"], "recipient": a["recipient_name"], "reason": a["reason"]} for a in state["plan"]]}
        data = self._llm_json(EXPLAIN_SYSTEM, json.dumps(facts, ensure_ascii=False), self.fast_model, 700)
        fallback = not (data and data.get("summary") and isinstance(data.get("responder_rationale"), list))
        if fallback:
            data = {"summary": f"{facts['user']} triggered an alert classified {state['triage']['emergency_type']} at risk "
                               f"{state['risk_level']} (score {state['risk_score']:.2f}). " + "; ".join(state["location_risk"]["factors"]) + ".",
                    "responder_rationale": [f"{x['channel']} -> {x['recipient']}: {x['reason']}" for x in facts["actions"]],
                    "user_message": "Your emergency alert was received and help is being notified. Stay where it is safe if you can."}
        log = self._log(state, "explain", "Generated audit explanation" + (" from template (LLM unavailable)." if fallback else " with Claude."),
                        "template fallback" if fallback else "LLM explanation from structured facts", [],
                        state["risk_level"], 0.7 if fallback else 0.9, t0)
        return {"explanation": data, "audit_log": [log]}

    def memory_update(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        uid, trig, tri = state["user_id"], state["trigger"], state["triage"]
        alert_id = self.graph.add_threat_alert(state["incident_id"], trig["lat"], trig["lng"], tri["emergency_type"],
                                               state["risk_level"], user_id=uid, ts=state["event_ts"],
                                               summary=(trig.get("message") or "silent panic")[:160])
        notes = ["alert stored in graph"]
        rec = getattr(self.graph, "record_incident", None)
        if rec:
            rec(state, alert_id)  # incident, decision, evidence, actions, steps -> graph
            notes.append("decision path stored in graph")
        if self.memory is not None:
            zone = state["location_risk"].get("zone") or {}
            ep = self.memory.add_episode(uid, f"{state['risk_level']} {tri['emergency_type']} alert in "
                                              f"{zone.get('name', 'unmapped area')}: {trig.get('message') or 'silent panic'}",
                                         {"incident_id": state["incident_id"], "risk_level": state["risk_level"],
                                          "zone_id": zone.get("id"), "emergency_type": tri["emergency_type"]})
            self.memory.update_profile(uid, {"last_alert_at": state["event_ts"], "last_alert_type": tri["emergency_type"],
                                             "last_alert_risk": state["risk_level"]})
            notes.append(f"episode {ep} + profile updated")
        log = self._log(state, "memory_update", "Persisted incident: " + "; ".join(notes) + ".",
                        "graph.add_threat_alert() + graph.record_incident() + memory.add_episode()", [alert_id],
                        state["risk_level"], 0.9, t0, cypher=self.graph.drain_trace())
        return {"audit_log": [log]}

    def finalize(self, state: SafeGuardState) -> dict:
        t0 = time.perf_counter()
        rec = getattr(self.graph, "record_incident", None)
        if rec:
                rec(state, None)  # cancelled / rejected attempts are remembered too
        if not state.get("valid"):
            log = self._log(state, "finalize", "No dispatch performed (trigger invalid or cancelled).", "close incident",
                            [], "LOW", 0.9, t0)
            return {"confidence": 0.9, "audit_log": [log]}
        conf = _clamp(0.5 * state["risk_confidence"] + 0.5 * state["dispatch_confidence"])
        log = self._log(state, "finalize", f"Incident closed: risk {state['risk_level']}, {len(state['receipts'])} actions, "
                                           f"overall confidence {conf:.2f}.", "close incident", [], state["risk_level"], conf, t0)
        return {"confidence": conf, "audit_log": [log]}

    # ------------------------------------------------------------------ graph
    def _build(self) -> StateGraph:
        g = StateGraph(SafeGuardState)
        for n in ("trigger_intake", "assess_context", "triage", "score_risk", "plan_dispatch", "execute_dispatch",
                  "explain", "memory_update", "finalize"):
            g.add_node(n, getattr(self, n))
        g.add_edge(START, "trigger_intake")
        g.add_conditional_edges("trigger_intake", self.route_after_intake,
                                {"assess_context": "assess_context", "finalize": "finalize"})
        for a, b in (("assess_context", "triage"), ("triage", "score_risk"), ("score_risk", "plan_dispatch"),
                     ("plan_dispatch", "execute_dispatch"), ("execute_dispatch", "explain"),
                     ("explain", "memory_update"), ("memory_update", "finalize"), ("finalize", END)):
            g.add_edge(a, b)
        return g

    # -------------------------------------------------------------- public API
    def stream_incident(self, user_id: str, trigger: dict, thread_id: Optional[str] = None) -> Iterator[dict]:
        """One event per finished node (for the live trace panel), then a '__final__' event."""
        incident_id = "inc-" + uuid.uuid4().hex[:8]
        thread_id = thread_id or user_id
        config = {"configurable": {"thread_id": thread_id}}
        inputs = {"user_id": user_id, "thread_id": thread_id, "incident_id": incident_id, "trigger": trigger}
        for chunk in self.app.stream(inputs, config, stream_mode="updates"):
            for node, update in chunk.items():
                update = update or {}
                yield {"node": node, "incident_id": incident_id, "logs": update.get("audit_log", []), "update": update}
        v = self.app.get_state(config).values
        resp = v.get("responders", {})
        highlight = [x["id"] for x in (resp.get("station"), resp.get("patrol"), resp.get("hospital")) if x]
        highlight += [a["recipient_id"] for a in v.get("plan", []) if a["recipient_id"].startswith("contact:")]
        yield {"node": "__final__", "incident_id": incident_id, "logs": [], "valid": v.get("valid", False),
               "risk_level": v.get("risk_level", "LOW"), "risk_score": v.get("risk_score", 0.0),
               "confidence": v.get("confidence", 0.0), "triage": v.get("triage", {}), "plan": v.get("plan", []),
               "receipts": v.get("receipts", []), "explanation": v.get("explanation", {}),
               "highlight_nodes": highlight, "subgraph": self.graph.dispatch_subgraph(user_id, highlight) if v.get("valid") else {}}

    def handle_incident(self, user_id: str, trigger: dict, thread_id: Optional[str] = None) -> dict:
        final, logs = {}, []
        for ev in self.stream_incident(user_id, trigger, thread_id):
            logs.extend(ev.get("logs", []))
            if ev["node"] == "__final__":
                final = ev
        final["audit_log"] = logs
        return final

    def get_audit_log(self, thread_id: str, incident_id: Optional[str] = None) -> list[dict]:
        logs = self.app.get_state({"configurable": {"thread_id": thread_id}}).values.get("audit_log", [])
        if incident_id is None and logs:
            incident_id = logs[-1]["incident_id"]
        return [l for l in logs if l["incident_id"] == incident_id]


RISK_COLORS = {"CRITICAL": "#b71c1c", "HIGH": "#e65100", "MEDIUM": "#f9a825", "LOW": "#2e7d32"}


def audit_log_to_dot(logs: list[dict]) -> str:
    """Reasoning graph as Graphviz DOT (st.graphviz_chart). Node colour = risk level at that step."""
    lines = ['digraph G { rankdir=LR; bgcolor="transparent"; node [shape=box, style="rounded,filled", '
             'fontname="Helvetica", fontsize=10, fontcolor="white"];']
    for i, l in enumerate(logs):
        label = f"{l['step']}\\n{l['risk_level']} | conf {l['confidence_score']:.2f}\\n{l['latency_ms']} ms"
        lines.append(f'  s{i} [label="{label}", fillcolor="{RISK_COLORS[l["risk_level"]]}"];')
        if i:
            lines.append(f"  s{i-1} -> s{i};")
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":  # smoke test: python agent_workflow.py  (works even without an API key)
    from graph_store import seed_demo

    graph = SafetyGraph("data/safety_graph.json")
    seed_demo(graph)
    agent = SafeGuardAgent(graph)
    u = graph.get("loc:now:u1")
    now = time.time()
    result = agent.handle_incident("u1", {
        "type": "triple_tap", "taps": [now - 0.9, now - 0.5, now], "lat": u["lat"], "lng": u["lng"],
        "accuracy_m": 12, "local_hour": 23, "message": "someone is following me and I think he has a knife"})
    for e in result["audit_log"]:
        print(json.dumps({k: e[k] for k in ("step", "thought", "action_taken", "retrieved_nodes", "risk_level",
                                            "confidence_score")}, indent=2))
    print("\nRISK:", result["risk_level"], f"(confidence {result['confidence']:.2f})")
    print("USER MESSAGE:", result["explanation"].get("user_message"))