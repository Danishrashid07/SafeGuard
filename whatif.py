"""whatif.py: counterfactual replay of an incident decision. Uses the real scoring and planning code;
sends nothing and changes nothing."""
from __future__ import annotations

from agent_workflow import keyword_triage


def _triage(message: str) -> dict:
    msg = (message or "").strip()
    if not msg:
        return {"emergency_type": "UNKNOWN", "severity": "HIGH", "signals": ["silent panic: no message"],
                "confidence": 0.55, "source": "default (silent trigger)"}
    kw = keyword_triage(msg)
    return {**kw, "confidence": 0.7 if kw["signals"] else 0.5, "source": "keyword rules"}


def replay(agent, user_id: str, trigger: dict, event_ts, incident_id: str, **overrides) -> dict:
    trig = {**trigger, **overrides}
    tri = _triage(trig.get("message"))
    lr = agent.graph.location_risk(trig["lat"], trig["lng"], trig.get("local_hour"), event_ts)
    state = {"user_id": user_id, "trigger": trig, "event_ts": event_ts, "incident_id": incident_id,
             "triage": tri, "location_risk": lr, "risk_level": "HIGH",
             "profile": agent.memory.get_profile(user_id) if agent.memory else {}}
    scored = agent.score_risk(state)
    scored.pop("audit_log", None)
    state.update(scored)
    planned = agent.plan_dispatch(state)
    actions = sorted({(a["channel"], a["recipient_name"]) for a in planned["plan"]})
    return {"score": state["risk_score"], "level": state["risk_level"], "actions": actions, "triage": tri}


def _scenarios(agent) -> list[tuple[str, dict]]:
    zones = [d for _, d in agent.graph.g.nodes(data=True) if d.get("kind") == "ZONE"]
    out = [("it happened at 14:00 (daytime)", {"local_hour": 14}),
           ("it happened at 02:00", {"local_hour": 2})]
    if zones:
        safe, risky = min(zones, key=lambda z: z["base_risk"]), max(zones, key=lambda z: z["base_risk"])
        out.append((f"it happened in '{safe['name']}' (lowest-risk zone)", {"lat": safe["lat"], "lng": safe["lng"]}))
        out.append((f"it happened in '{risky['name']}' (highest-risk zone)", {"lat": risky["lat"], "lng": risky["lng"]}))
    out += [("the alert was a silent panic (no message)", {"message": ""}),
            ("the message only said 'I feel uncomfortable'", {"message": "I feel uncomfortable"}),
            ("the message mentioned a knife", {"message": "he has a knife"})]
    return out


def _fmt(actions) -> str:
    return ", ".join(f"{c} to {n}" for c, n in actions) or "-"


def run_whatif(agent, user_id: str, trigger: dict, event_ts, incident_id: str,
               actual_score: float | None = None, actual_level: str | None = None) -> dict:
    base = replay(agent, user_id, trigger, event_ts, incident_id)
    rows, insights = [], []
    for name, ov in _scenarios(agent):
        r = replay(agent, user_id, trigger, event_ts, incident_id, **ov)
        gained = sorted(set(r["actions"]) - set(base["actions"]))
        lost = sorted(set(base["actions"]) - set(r["actions"]))
        rows.append({"If...": name, "Score": r["score"], "Risk": r["level"],
                     "Change": round(r["score"] - base["score"], 3),
                     "Actions added": _fmt(gained), "Actions dropped": _fmt(lost)})
        if r["level"] != base["level"]:
            text = f"If {name}: risk would be **{r['level']}** instead of **{base['level']}**"
            if lost:
                text += f"; no longer: {_fmt(lost)}"
            if gained:
                text += f"; newly: {_fmt(gained)}"
            insights.append(text + ".")
    if not insights:
        insights.append("No single change tried here would move the risk level: the decision is robust.")
    matches = None
    if actual_score is not None:
        matches = abs(base["score"] - actual_score) < 0.001 and base["level"] == actual_level
    return {"baseline": base, "rows": rows, "insights": insights, "replay_matches_audit": matches}