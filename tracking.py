"""tracking.py: live incident tracking (position trail, active-incident state, position pushes to contacts)."""
from __future__ import annotations

import json
import math
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TRACK_DIR = Path("data/tracks")
ACTIVE = Path("data/active_incident.json")
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def start_session(incident_id: str, user_name: str, risk_level: str, lat: float, lng: float,
                  accuracy_m: Optional[float] = None) -> None:
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE.write_text(json.dumps({"incident_id": incident_id, "user_name": user_name, "risk_level": risk_level,
                                  "status": "ACTIVE", "started_at": _now()}), encoding="utf-8")
    add_point(incident_id, lat, lng, accuracy_m, "trigger")


def add_point(incident_id: str, lat: float, lng: float, accuracy_m: Optional[float] = None, source: str = "gps") -> None:
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": _now(), "lat": lat, "lng": lng, "accuracy_m": accuracy_m, "source": source}
    with _lock, (TRACK_DIR / f"{incident_id}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def read_points(incident_id: str) -> list[dict]:
    p = TRACK_DIR / f"{incident_id}.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def active_session() -> Optional[dict]:
    if not ACTIVE.exists():
        return None
    try:
        return json.loads(ACTIVE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def end_session(incident_id: str) -> None:
    s = active_session()
    if s and s["incident_id"] == incident_id:
        s.update(status="ENDED", ended_at=_now())
        ACTIVE.write_text(json.dumps(s), encoding="utf-8")


def step_position(lat: float, lng: float, i: int, step_m: float = 25.0) -> tuple[float, float]:
    """Simulated walking: ~25 m per step, heading north-east with a slight wobble."""
    h = math.radians(45 + 20 * math.sin(i))
    return (round(lat + step_m * math.cos(h) / 111_320, 6),
            round(lng + step_m * math.sin(h) / (111_320 * math.cos(math.radians(lat))), 6))


def _push(topic: str, title: str, body: str, click: Optional[str], priority: str, tags: str) -> None:
    headers = {"Title": title, "Priority": priority, "Tags": tags, "User-Agent": "safeguard/1.0"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        r.read()


def push_position(topics: list[str], user_name: str, lat: float, lng: float, n: int, demo: bool = True) -> int:
    sent = 0
    for t in topics:
        try:
            _push(t, f"SAFEGUARD LIVE UPDATE #{n}", ("[DEMO] " if demo else "") + f"{user_name} is now at {lat:.5f}, {lng:.5f}",
                  f"https://maps.google.com/?q={lat},{lng}", "4", "round_pushpin")
            sent += 1
        except Exception:
            pass
    return sent


def push_text(topics: list[str], title: str, body: str, demo: bool = True) -> int:
    sent = 0
    for t in topics:
        try:
            _push(t, title, ("[DEMO] " if demo else "") + body, None, "3", "white_check_mark")
            sent += 1
        except Exception:
            pass
    return sent


def render_live_track(compact: bool = False) -> None:
    import pandas as pd
    import streamlit as st

    sess = active_session()
    if not sess:
        st.info("No incident is being tracked right now.")
        return
    pts = read_points(sess["incident_id"])
    live = sess["status"] == "ACTIVE"
    st.markdown(f"{'🔴 LIVE' if live else '⚪ ENDED'} · **{sess['user_name']}** · risk {sess['risk_level']} · "
                f"incident `{sess['incident_id']}`")
    if not pts:
        st.caption("Waiting for the first position...")
        return
    last = pts[-1]
    age = int((datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])).total_seconds())
    rows = [{"lat": p["lat"], "lon": p["lng"], "color": "#FFAB00", "size": 25} for p in pts[:-1]]
    rows.append({"lat": last["lat"], "lon": last["lng"], "color": "#FF0000", "size": 80})
    st.map(pd.DataFrame(rows), latitude="lat", longitude="lon", color="color", size="size")
    st.markdown(f"📍 Latest: [{last['lat']:.5f}, {last['lng']:.5f}](https://maps.google.com/?q={last['lat']},{last['lng']})"
                f" · updated {age}s ago · {len(pts)} points")
    if not compact:
        st.dataframe(pd.DataFrame(pts[-8:][::-1]), hide_index=True)