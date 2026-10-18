"""learning.py: memory that learns from cancelled alerts. It may only LENGTHEN the cancel window (max 10 s):
it can delay a dispatch slightly but never reduces severity, responders, or contacts. The user must accept first."""
from __future__ import annotations

from datetime import datetime, timezone

MAX_GRACE = 10


def zone_of(graph, lat: float, lng: float) -> str:
    z = graph.zone_for_point(lat, lng)
    return z["name"] if z else "unmapped area"


def record_trigger(memory, user_id: str, zone: str, cancelled: bool) -> None:
    stats = memory.get_profile(user_id).get("trigger_stats", {})
    z = stats.get(zone, {"total": 0, "cancelled": 0})
    z["total"] += 1
    z["cancelled"] += int(cancelled)
    stats[zone] = z
    memory.update_profile(user_id, {"trigger_stats": stats})


def suggestion(memory, user_id: str):
    p = memory.get_profile(user_id)
    prefs, dismissed = p.get("zone_prefs", {}), set(p.get("dismissed_suggestions", []))
    for zone, z in p.get("trigger_stats", {}).items():
        if z["cancelled"] >= 3 and z["cancelled"] / z["total"] >= 0.6 and zone not in prefs and zone not in dismissed:
            return {"zone": zone, "cancelled": z["cancelled"], "total": z["total"]}
    return None


def accept(memory, user_id: str, zone: str) -> None:
    prefs = memory.get_profile(user_id).get("zone_prefs", {})
    prefs[zone] = {"grace_s": MAX_GRACE, "accepted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    memory.update_profile(user_id, {"zone_prefs": prefs})
    memory.add_episode(user_id, f"User accepted a longer cancel window ({MAX_GRACE} s) in {zone} after repeated cancellations")


def dismiss(memory, user_id: str, zone: str) -> None:
    d = memory.get_profile(user_id).get("dismissed_suggestions", [])
    memory.update_profile(user_id, {"dismissed_suggestions": d + [zone]})


def grace_for(memory, user_id: str, zone: str, default: int) -> int:
    pref = memory.get_profile(user_id).get("zone_prefs", {}).get(zone)
    return max(default, min(MAX_GRACE, pref["grace_s"])) if pref else default