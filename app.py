"""app.py: SafeGuard Streamlit demo.  Run:  streamlit run app.py"""
from __future__ import annotations

import time
from datetime import datetime
import uuid
import os

try:
    from falkor_graph import FalkorSafetyGraph
    from falkor_memory import FalkorMemory
    from crew import Crew
except ImportError:  # FalkorDB is optional; NetworkX remains the fallback
    FalkorSafetyGraph = FalkorMemory = Crew = None

try:
    from streamlit_geolocation import streamlit_geolocation
except ImportError:
    def streamlit_geolocation():
        return None
from notifier import LiveDispatcher
import json
import sos_server
from sos_server import SosServer
import tracking
from report import build_report
from whatif import run_whatif
import learning
import osm_data
import walk_ui
from walkmode import WalkWatcher

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from agent_workflow import RISK_COLORS, SafeGuardAgent, SimulatedDispatcher, audit_log_to_dot, detect_triple_tap
from graph_store import SafetyGraph, seed_demo
from llm import LLM
from memory_store import MemoryStore, seed_demo_profile

USER_ID = "u1"
LOG_KEYS = ("step", "thought", "action_taken", "retrieved_nodes", "risk_level", "confidence_score")
PRESETS = {
    "(silent panic: no message)": "",
    "Someone is following me": "someone is following me and I feel unsafe",
    "Weapon involved": "someone is following me and I think he has a knife",
    "Medical: heavy bleeding": "I fell and I am bleeding heavily",
    "Harassment": "a guy keeps harassing me and I feel uncomfortable",
}

st.set_page_config(page_title="SafeGuard Agent", page_icon="🛡️", layout="wide")


@st.cache_resource
def get_system():
    """Initialize SafeGuard once per Streamlit server process.

    GRAPH_BACKEND=falkordb selects FalkorDB. If it cannot be imported or reached,
    the app deliberately falls back to the local NetworkX/SQLite backend.
    """
    note, graph, memory = "", None, None
    if os.getenv("GRAPH_BACKEND", "networkx").lower() == "falkordb":
        if FalkorSafetyGraph is None:
            note = "FalkorDB package unavailable; install it with: python -m pip install falkordb. Using local storage."
        else:
            try:
                graph = FalkorSafetyGraph()
                memory = FalkorMemory(graph)
            except Exception as exc:
                note = f"FalkorDB unavailable ({exc}). Using local NetworkX/SQLite storage."
                graph, memory = None, None

    if graph is None:
        graph = SafetyGraph("data/safety_graph.json")
        memory = MemoryStore("data/memory.sqlite")

    seed_demo(graph)
    seed_demo_profile(memory, USER_ID)
    dispatcher = LiveDispatcher(graph)
    agent = SafeGuardAgent(graph, memory=memory, dispatcher=dispatcher, llm=LLM())

    # The task queue is specific to FalkorDB; the agent remains usable without it.
    if FalkorSafetyGraph is not None and isinstance(graph, FalkorSafetyGraph) and Crew is not None:
        agent.crew = Crew(agent, graph)
        agent.crew.start()

    watcher = WalkWatcher(agent, graph)
    watcher.start()
    sos = SosServer(agent, graph, USER_ID)
    sos.start()
    return graph, memory, agent, watcher, sos, note


graph, memory, agent, watcher, sos, backend_note = get_system()

ss = st.session_state
_DEFAULTS = {"taps": [], "pending": None, "to_run": None, "last": None, "last_logs": [],
             "last_trigger": None, "last_event_ts": None, "whatif": None, "track_pos": None}
for _k, _v in _DEFAULTS.items():
    if _k not in ss:
        ss[_k] = _v


def badge(level: str) -> str:
    return (f"<span style='background:{RISK_COLORS[level]};color:white;padding:2px 10px;"
            f"border-radius:6px;font-size:13px;font-weight:600'>{level}</span>")




def render_log(log: dict) -> None:
    st.markdown(f"{badge(log['risk_level'])} **{log['step']}** · conf {log['confidence_score']:.2f} · "
                f"{log['latency_ms']} ms", unsafe_allow_html=True)
    st.caption(log["thought"])
    with st.expander("structured JSON log"):
        st.json({k: log[k] for k in LOG_KEYS})


# ------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("⚙️ Simulation controls")

    # ---- location (real GPS or demo points)
    st.markdown("**📍 Location**")
    geo = streamlit_geolocation()  # click the icon, then press Allow in the browser prompt
    real = bool(geo) and geo.get("latitude") is not None
    zones = {d["name"]: (d["lat"], d["lng"]) for _, d in graph.g.nodes(data=True) if d["kind"] == "ZONE"}
    live = graph.get(f"loc:now:{USER_ID}") or {"lat": 37.7749, "lng": -122.4194}
    places = {}
    if real:
        places["📍 My real location"] = (geo["latitude"], geo["longitude"])
    places["Demo: current position"] = (live["lat"], live["lng"])
    places.update({f"Demo: inside {n}": c for n, c in zones.items()})
    place = st.selectbox("User location", list(places))
    lat, lng = places[place]
    hour = datetime.now().hour
    area_key = (round(lat, 2), round(lng, 2))
    if real and place.startswith("📍"):
        acc = int(geo.get("accuracy") or 20)
        st.caption(f"GPS fix: {lat:.5f}, {lng:.5f} (±{acc} m)")
    else:
        acc = st.slider("GPS accuracy (m)", 5, 100, 12)
        hour = st.slider("Local hour (0-23)", 0, 23, datetime.now().hour)
    if real and place.startswith("📍") and ss.get("osm_area") != area_key:  # real GPS: check the area by itself
        try:
            with st.spinner("Checking nearby police stations and hospitals (OpenStreetMap)..."):
                found = osm_data.ensure_area(graph, (lat, lng), (lat, lng), margin_m=3000)
            ss["osm_msg"] = (f"📡 {found['counts']['police']} police stations and {found['counts']['hospital']} "
                             f"hospitals found nearby ({found['added']['stations']} new).")
        except Exception as exc:
            ss["osm_msg"] = f"OpenStreetMap unavailable: {exc}"
        ss["osm_area"] = area_key
    if ss.get("osm_msg"):
        st.caption(ss["osm_msg"])
    if st.button("📡 Check police & hospitals around this location"):
        try:
            with st.spinner("Querying OpenStreetMap..."):
                found = osm_data.ensure_area(graph, (lat, lng), (lat, lng), margin_m=3000)
            ss["osm_msg"] = (f"📡 {found['counts']['police']} police stations and {found['counts']['hospital']} "
                             f"hospitals found nearby ({found['added']['stations']} new).")
        except Exception as exc:
            ss["osm_msg"] = f"OpenStreetMap unavailable: {exc}"
        st.rerun()

    if real and st.button("Re-center demo city on my location"):
        graph.reset()  # reset through the backend API so FalkorDB stays in sync
        seed_demo(graph, center=(geo["latitude"], geo["longitude"]))
        st.rerun()
    if backend_note:
        st.warning(backend_note)
    st.caption(f"Graph backend: **{getattr(graph, 'backend', type(graph).__name__)}**")
    if getattr(sos, "error", None):
        st.warning(f"Background SOS listener: {sos.error}")
    else:
        st.caption(f"Background SOS listener: port {getattr(sos, 'port', 8765)} · token-protected")

    # ---- alert message
    preset = st.selectbox("Message sent with the alert", list(PRESETS))
    custom = st.text_input("...or type your own message")
    message = custom.strip() or PRESETS[preset]
    grace = st.slider("Cancel window (seconds)", 0, 10, 5)
    st.caption(f"LLM provider: **{agent.llm.provider}** (the dispatch core never depends on it)")

    # ---- emergency contacts
    with st.expander("👥 Emergency contacts", expanded=True):
        for c in graph.contacts_for(USER_ID):
            cols = st.columns([4, 1, 1])
            cols[0].write(f"{c['priority']}. **{c['name']}** ({c['relation']})")
            topic = c.get("ntfy_topic")
            if topic and cols[1].button("Test", key=f"t_{c['id']}"):
                try:
                    agent.dispatcher.send_test(topic)
                    st.toast("Test alert sent")
                except Exception as exc:
                    st.error(f"Could not send: {exc}")
            if cols[2].button("🗑", key=f"d_{c['id']}"):
                graph.remove_node(c["id"])
                st.rerun()
            if topic:
                st.caption("Contact installs the free **ntfy** app and subscribes to:")
                st.code(topic, language=None)
            else:
                st.caption("demo contact: alerts are simulated")
        with st.form("add_contact", clear_on_submit=True):
            n = st.text_input("Name")
            ph = st.text_input("Phone (shown in alerts)")
            rel = st.text_input("Relation", "Friend")
            pr = st.number_input("Priority (1 = first)", 1, 10, 1)
            if st.form_submit_button("Add contact") and n.strip():
                nid = graph.add_contact(USER_ID, n.strip().lower().replace(" ", "_"), n.strip(), ph, rel, int(pr))
                graph.set_prop(nid, "ntfy_topic", "safeguard-" + uuid.uuid4().hex[:12])
                st.rerun()

    # ---- real police station / hospital
    with st.expander("🏛️ Add a real police station / hospital"):
        with st.form("add_resp", clear_on_submit=True):
            kind = st.selectbox("Type", ["Police station", "Hospital"])
            rname = st.text_input("Name")
            rphone = st.text_input("Phone")
            rlat = st.number_input("Latitude", value=float(lat), format="%.6f")
            rlng = st.number_input("Longitude", value=float(lng), format="%.6f")
            if st.form_submit_button("Add") and rname.strip():
                rid = rname.strip().lower().replace(" ", "_")
                if kind == "Police station":
                    graph.add_station(rid, rname.strip(), rlat, rlng, rphone)
                else:
                    graph.add_hospital(rid, rname.strip(), rlat, rlng, rphone, has_trauma=True)
                st.rerun()

    # ---- resilience controls
    with st.expander("🧪 Resilience demo"):
        llm_down = st.checkbox("Simulate LLM outage")
        net_down = st.checkbox("Simulate internet outage")
        agent.llm.simulate_outage = llm_down
        agent.dispatcher.simulate_offline = net_down
        tracking.OFFLINE = net_down
        st.caption(f"Alerts queued, waiting for connectivity: **{agent.dispatcher.queued_count()}**")
        st.caption("LLM outage: triage falls back to rules and explanations to a template. Network outages queue configured alerts.")

    if st.button("Reset demo data"):
        graph.reset()
        seed_demo(graph)
        if hasattr(memory, "delete_user_data"):
            memory.delete_user_data(USER_ID)
        else:
            memory.update_profile(USER_ID, {
                "full_name": None, "medical_notes": None, "preferred_contacts": None,
                "do_not_notify": None, "frequent_routes": None, "language": None,
                "trigger_stats": None, "zone_prefs": None, "dismissed_suggestions": None,
                "walk_history": None,
            })
        seed_demo_profile(memory, USER_ID)
        agent.dispatcher.outbox.clear()
        for k in ("taps", "pending", "to_run", "last", "last_logs"):
            ss.pop(k, None)
        st.rerun()

def build_trigger(taps: list[float], cancelled: bool = False) -> dict:
    return {"type": "triple_tap", "taps": taps, "lat": lat, "lng": lng, "accuracy_m": acc,
            "local_hour": hour, "message": message, "cancelled": cancelled,
            "timestamp": datetime.now().astimezone().isoformat()}


# ---------------------------------------------------------------------- main
st.title("🛡️ SafeGuard Agent")
st.caption("Demo environment: this app does not contact emergency services. Configured ntfy topics may receive real push notifications.")
left, right = st.columns([1, 1.3])

with left:
    st.subheader("📱 User phone")
    c1, c2, c3, c4 = st.columns(4)
    tap = c1.button("🆘 TAP", type="primary", use_container_width=True)
    sim = c2.button("⚡ Simulate 3 taps", use_container_width=True)
    slow = c3.button("🐢 Slow taps", use_container_width=True)
    manual_sos = c4.button("🚨 SOS now", use_container_width=True)
    now = time.time()

    if tap:
        ss.taps = [t for t in ss.taps if now - t <= 2.0] + [now]
        if detect_triple_tap(ss.taps):
            ss.pending, ss.taps = {"taps": list(ss.taps)}, []
    if sim:
        ss.pending, ss.taps = {"taps": [now - 0.8, now - 0.4, now]}, []
    if slow:  # 3 taps spread over 6 s: the agent must reject this pattern (no grace window needed)
        ss.to_run, ss.taps = build_trigger([now - 6.0, now - 3.0, now]), []
    if manual_sos:
        ss.to_run = {"type": "panic_button", "lat": lat, "lng": lng, "accuracy_m": acc,
                     "local_hour": hour, "message": message, "timestamp": datetime.now().astimezone().isoformat()}

    st.progress(min(len(ss.taps), 3) / 3, text=f"Taps in the last 2 s: {len(ss.taps)}/3")
    st.caption("Streamlit can drop very fast clicks, so use ⚡ for a reliable demo. 🐢 shows a rejected pattern.")

    if ss.pending:
        taps = ss.pending["taps"]
        zone_name = learning.zone_of(graph, lat, lng)
        eff_grace = learning.grace_for(memory, USER_ID, zone_name, int(grace))
        if st.button("❌ CANCEL: I'm safe", use_container_width=True, key="cancel"):
            ss.to_run, ss.pending = build_trigger(taps, cancelled=True), None
        else:
            ph = st.empty()
            note = " (longer window learned for this area)" if eff_grace > int(grace) else ""
            for remaining in range(eff_grace, 0, -1):
                ph.error(f"🚨 Triple-tap detected. Dispatching in {remaining}s unless cancelled...{note}")
                time.sleep(1)
            ph.empty()
            ss.to_run, ss.pending = build_trigger(taps), None

with right:
    st.subheader("🔎 Live reasoning & audit trace")
    trace = st.container()
    if ss.to_run is not None:
        trigger, ss.to_run = ss.to_run, None
        logs: list[dict] = []
        for ev in agent.stream_incident(USER_ID, trigger):
            for log in ev["logs"]:
                logs.append(log)
                with trace:
                    render_log(log)
            if ev["node"] == "trigger_intake":
                ss.last_event_ts = ev["update"].get("event_ts")
            if ev["node"] == "__final__":
                ss.last = ev
        ss.last_logs = logs
        ss.last_trigger, ss.whatif = trigger, None
        if trigger["type"] == "triple_tap" and ss.last and (ss.last["valid"] or trigger.get("cancelled")):
             learning.record_trigger(memory, USER_ID, learning.zone_of(graph, trigger["lat"], trigger["lng"]),
                                    bool(trigger.get("cancelled")))
        if ss.last and ss.last["valid"]:
            tracking.start_session(ss.last["incident_id"], graph.get(f"user:{USER_ID}")["name"],
                                   ss.last["risk_level"], trigger["lat"], trigger["lng"], trigger.get("accuracy_m"))
            ss.track_pos = (trigger["lat"], trigger["lng"])
    elif ss.last_logs:
        with trace:
            for log in ss.last_logs:
                render_log(log)
    else:
        trace.info("Press 🆘 TAP three times quickly, or use ⚡ Simulate 3 taps.")
with left:
    st.divider()
    sug = learning.suggestion(memory, USER_ID)
    if sug:
        st.info(f"🧠 You cancelled {sug['cancelled']} of your last {sug['total']} alerts in **{sug['zone']}**. "
                f"Use a longer cancel window ({learning.MAX_GRACE}s) there to avoid accidental dispatches? "
                "Protection is never reduced: a cancel window only delays dispatch slightly.")
        a1, a2 = st.columns(2)
        if a1.button("Yes, use a longer window", key="learn_yes"):
            learning.accept(memory, USER_ID, sug["zone"])
            st.rerun()
        if a2.button("No thanks", key="learn_no"):
            learning.dismiss(memory, USER_ID, sug["zone"])
            st.rerun()
    walk_ui.render_walk_panel(graph, memory, agent, lat, lng, geo, real, hour, USER_ID)

# ------------------------------------------------------------------- results
last = ss.last
if last:
    st.divider()
    if not last["valid"]:
        st.warning("No dispatch: " + ss.last_logs[0]["thought"])
    else:
        st.markdown(f"## Risk {badge(last['risk_level'])}  score {last['risk_score']:.2f} · "
                    f"confidence {last['confidence']:.2f}", unsafe_allow_html=True)
        expl = last["explanation"]
        st.info(expl.get("user_message", ""))
        st.write(expl.get("summary", ""))
        t_plan, t_reason, t_graph, t_map, t_out, t_whatif, t_track, t_report = st.tabs(
            ["Dispatch plan", "Reasoning graph", "Entity graph", "Map", "Delivery outbox",
             "🔮 What-if", "📍 Live tracking", "📄 Report"])
        with t_plan:
            st.dataframe(pd.DataFrame([{"#": a["order"], "Channel": a["channel"], "Recipient": a["recipient_name"],
                                        "Why": a["reason"]} for a in last["plan"]]), hide_index=True)
        with t_reason:
            st.graphviz_chart(audit_log_to_dot(ss.last_logs))
        with t_graph:
            sub = last["subgraph"]
            hl_nodes = set(last["highlight_nodes"]) | {n["id"] for n in sub["nodes"]}
            hl_edges = [(e["source"], e["relation"], e["target"]) for e in sub["edges"]]
            try:
                components.html(graph.to_pyvis_html(highlight_nodes=hl_nodes, highlight_edges=hl_edges),
                                height=540, scrolling=True)
            except ImportError as exc:
                st.warning(f"Entity graph visualization dependency is missing: {exc}. Install pyvis with: python -m pip install pyvis")
        with t_map:
            st.map(pd.DataFrame(graph.geo_points(highlight=last["highlight_nodes"])),
                   latitude="lat", longitude="lon", color="color", size="size")
        with t_out:
            mine = [r for r in agent.dispatcher.outbox if r["payload"].get("incident_id") == last["incident_id"]]
            st.caption("Medical notes appear only in the EMS payload.")
            st.json(mine)

        with t_whatif:
            st.caption("Replays the real risk engine with one factor changed. Nothing is sent or stored.")
            if st.button("Run what-if analysis"):
                ss.whatif = run_whatif(agent, USER_ID, ss.last_trigger, ss.last_event_ts, last["incident_id"],
                                       last["risk_score"], last["risk_level"])
            w = ss.whatif
            if w:
                if w["replay_matches_audit"] is True:
                    st.success("✅ The replay reproduces the live decision exactly, so the decision is auditable.")
                elif w["replay_matches_audit"] is False:
                    st.warning("Replay differs from the live decision (live triage also used the LLM). "
                               "What-if uses rules-only triage.")
                for line in w["insights"]:
                    st.markdown("- " + line)
                st.dataframe(pd.DataFrame(w["rows"]), hide_index=True)

        with t_track:
            name = graph.get(f"user:{USER_ID}")["name"]
            demo = agent.dispatcher.demo_mode
            topics = []
            for a in last["plan"]:
                if a["channel"] == "SMS_CONTACT":
                    tp = (graph.get(a["recipient_id"]) or {}).get("ntfy_topic")
                    if tp:
                        topics.append(tp)
            st.caption(f"Alerted contacts with a push topic: {len(topics)}. They get a position update with a map link "
                       "on each step. Open the **Live Track** page in another tab to watch the trail.")
            b1, b2, b3 = st.columns(3)
            ph = st.empty()
            if b1.button("📍 Send my real position", disabled=not real):
                tracking.add_point(last["incident_id"], geo["latitude"], geo["longitude"], geo.get("accuracy"), "gps")
                graph.set_user_location(USER_ID, geo["latitude"], geo["longitude"], geo.get("accuracy"))
                ss.track_pos = (geo["latitude"], geo["longitude"])
                tracking.push_position(topics, name, geo["latitude"], geo["longitude"],
                                       len(tracking.read_points(last["incident_id"])), demo)
                st.toast("Position shared")
            if b2.button("🚶 Simulate walking (5 updates)"):
                p_lat, p_lng = ss.track_pos or (lat, lng)
                for i in range(1, 6):
                    p_lat, p_lng = tracking.step_position(p_lat, p_lng, i)
                    tracking.add_point(last["incident_id"], p_lat, p_lng, acc, "simulated-walk")
                    graph.set_user_location(USER_ID, p_lat, p_lng, acc)
                    tracking.push_position(topics, name, p_lat, p_lng, i, demo)
                    with ph.container():
                        tracking.render_live_track(compact=True)
                    time.sleep(1)
                ss.track_pos = (p_lat, p_lng)
            if b3.button("✅ I'm safe: end tracking"):
                tracking.end_session(last["incident_id"])
                tracking.push_text(topics, "SAFEGUARD: ALL CLEAR", f"{name} marked themselves safe.", demo)
                st.toast("Tracking ended, contacts notified")
            with ph.container():
                tracking.render_live_track(compact=True)

        with t_report:
            trail = tracking.read_points(last["incident_id"])
            doc, bundle = build_report(last, ss.last_logs, ss.last_trigger, trail, ss.whatif)
            st.caption(f"Integrity SHA-256: `{bundle['integrity_sha256']}`")
            d1, d2 = st.columns(2)
            d1.download_button("⬇️ Incident report (HTML, print to PDF)", doc,
                               f"{last['incident_id']}_report.html", "text/html")
            d2.download_button("⬇️ Evidence bundle (JSON)", json.dumps(bundle, indent=2, ensure_ascii=False, default=str),
                               f"{last['incident_id']}_evidence.json", "application/json")
            st.caption("Open the HTML file and press Ctrl+P, then Save as PDF. Medical notes are not included.")