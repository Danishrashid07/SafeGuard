"""Simulated agency control room: shows incoming alerts live with an alarm sound."""
import io
import json
from tracking import render_live_track
import math
import struct
import wave
from pathlib import Path

import pandas as pd
import streamlit as st

INBOX = Path("data/agency_inbox.jsonl")
COLORS = {"CRITICAL": "🟥", "HIGH": "🟧", "MEDIUM": "🟨", "LOW": "🟩"}
st.set_page_config(page_title="Agency Console", page_icon="🚓", layout="wide")
st.title("🚓 Agency control room (simulated)")
st.caption("Receives alerts from the SafeGuard agent. Click once anywhere on this page so the browser allows alarm sound.")


def alarm_wav() -> bytes:
    rate, frames = 22050, bytearray()
    for i in range(int(rate * 1.2)):
        f = 880 if (i // (rate // 4)) % 2 == 0 else 660
        frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * f * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


@st.fragment(run_every=3)
def board():
    render_live_track(compact=True)
    rows = [json.loads(l) for l in INBOX.read_text(encoding="utf-8").splitlines() if l.strip()] if INBOX.exists() else []
    seen = st.session_state.setdefault("seen", len(rows))  # don't alarm for old alerts on first load
    if len(rows) > seen:
        st.session_state["seen"] = len(rows)
        try:
            st.audio(alarm_wav(), format="audio/wav", autoplay=True)
        except TypeError:
            st.warning("New alert! (run `pip install -U streamlit` for alarm sound)")
    if not rows:
        st.info("No alerts yet. Trigger one from the main SafeGuard page.")
        return
    pts = [r["location"] for r in rows if r.get("location")]
    if pts:
        st.map(pd.DataFrame([{"lat": p["lat"], "lon": p["lng"]} for p in pts]))
    for r in reversed(rows[-10:]):
        with st.container(border=True):
            st.markdown(f"### {COLORS.get(r['risk_level'], '')} {r['risk_level']} · {r['emergency_type']} → {r['agency']}"
                        + ("  `DEMO`" if r.get("demo") else ""))
            loc = r.get("location") or {}
            st.write(f"**{r['user_name']}** ({r['user_phone']}) · {r['received_at']} · channel {r['channel']}"
                     + (f" · ETA ~{r['eta_min']} min" if r.get("eta_min") else ""))
            st.write(f"Message: {r['message']}")
            if loc:
                st.markdown(f"📍 [{loc['lat']:.5f}, {loc['lng']:.5f}]({loc['map_link']}) (±{loc.get('accuracy_m')} m)")
            if r.get("medical_notes"):
                st.warning("Medical notes (EMS only): " + r["medical_notes"])


board()