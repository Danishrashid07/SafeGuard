"""walk_ui.py: Streamlit panel for Walk Me Home mode."""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request

from graph_store import haversine_m
import uuid
from datetime import datetime, timedelta, timezone

import pydeck as pdk
import streamlit as st
import pandas as pd

import osm_data

import routing
import walkmode


def _places(graph, user_id: str) -> dict:
    out = {}
    for _, dst, k in graph.g.out_edges(f"user:{user_id}", keys=True):
        n = graph.get(dst)
        if k == "FREQUENTS" and n and "lat" in n:
            out[n["name"]] = (n["lat"], n["lng"])
    return out



    node = graph.g.nodes[f"user:{user_id}"]
    if not node.get("ntfy_topic"):
        node["ntfy_topic"] = "safeguard-me-" + uuid.uuid4().hex[:10]
        graph.save()
    return node["ntfy_topic"]
def _self_topic(graph, user_id: str) -> str:
    nid = f"user:{user_id}"
    topic = (graph.get(nid) or {}).get("ntfy_topic")
    if not topic:
        topic = "safeguard-me-" + uuid.uuid4().hex[:10]
        graph.set_prop(nid, "ntfy_topic", topic)
    return topic

def _route_map(graph, route, direct, position) -> None:
    stations = graph.nearest("STATION", route[0][0], route[0][1], k=50)
    layers = [
        pdk.Layer("PathLayer", data=[{"path": [[p[1], p[0]] for p in direct]}], get_path="path",
                  get_color=[150, 150, 150], width_min_pixels=3),
        pdk.Layer("PathLayer", data=[{"path": [[p[1], p[0]] for p in route]}], get_path="path",
                  get_color=[0, 200, 120], width_min_pixels=5)]
    pts = [{"lon": s["lng"], "lat": s["lat"], "c": [0, 82, 204], "r": 70} for s in stations]
    pts.append({"lon": route[-1][1], "lat": route[-1][0], "c": [255, 171, 0], "r": 80})
    if position:
        pts.append({"lon": position[1], "lat": position[0], "c": [255, 0, 0], "r": 90})
    layers.append(pdk.Layer("ScatterplotLayer", data=pts, get_position="[lon, lat]", get_fill_color="c", get_radius="r"))
    lats, lngs = [p[0] for p in route], [p[1] for p in route]
    span = max(max(lats) - min(lats), max(lngs) - min(lngs), 0.005)
    zoom = max(11, min(16, 14 - math.log2(span / 0.02)))
    st.pydeck_chart(pdk.Deck(layers=layers, initial_view_state=pdk.ViewState(
        latitude=sum(lats) / len(lats), longitude=sum(lngs) / len(lngs), zoom=zoom)))
    st.caption("🟢 safe route · ⚪ direct route · 🔵 police stations · 🟠 destination · 🔴 you")


def _load_incident(ss, s: dict) -> bool:
    """Copies the auto-escalated incident into the main app state so results tabs and the report work."""
    if ss.get("walk_loaded") == s.get("incident_id") or not walkmode.INCIDENT.exists():
        return False
    data = json.loads(walkmode.INCIDENT.read_text(encoding="utf-8"))
    final, logs = data["final"], data["logs"]
    ss.last = final
    ss.last_logs = [{**a, "incident_id": final["incident_id"]} for a in s["audit"]] + logs
    ss.last_trigger, ss.last_event_ts, ss.whatif = data["trigger"], data["event_ts"], None
    ss.track_pos = tuple(s["position"])
    ss.walk_loaded = final["incident_id"]
    return True


@st.fragment(run_every=1)
def _timer_panel(memory, geo, real, user_id):
    ss = st.session_state
    s = walkmode.load()
    if not s or s["status"] in ("COMPLETED", "CANCELLED"):
        return
    now, status = datetime.now(timezone.utc), s["status"]
    if status == "ACTIVE":
        left = int((walkmode.parse(s["due_at"]) - now).total_seconds())
        st.success(f"🟢 Walking to **{s['dest_name']}**. Check-in due in **{max(left, 0)} s**.")
    elif status == "WARNING":
        left = int((walkmode.parse(s["warning_at"]) + timedelta(seconds=s["grace_s"]) - now).total_seconds())
        st.error(f"🔴 MISSED CHECK-IN. Auto-escalating in **{max(left, 0)} s**. Tap ✅ I'm OK now to cancel.")
    elif status == "ESCALATING":
        st.error("🚨 Escalating now: dispatching to contacts and agencies...")
    elif status == "ESCALATED":
        st.error(f"🚨 Auto-escalated at risk {s.get('risk_level')}. Contacts and agencies were alerted with no tap.")
        if _load_incident(ss, s):
            st.rerun(scope="app")
    elif status == "ESCALATION_FAILED":
        st.error("Escalation failed (see events). Call your local emergency number now.")

    if status in ("ACTIVE", "WARNING"):
        b1, b2, b3, b4 = st.columns(4)
        if b1.button("✅ I'm OK", type="primary", key="w_ok", use_container_width=True):
            walkmode.check_in()
            st.rerun(scope="app")
        if b2.button("🚶 Advance", key="w_adv", use_container_width=True):
            walkmode.advance()
            st.rerun(scope="app")
        if b3.button("🏁 Arrived", key="w_arr", use_container_width=True):
            done = walkmode.arrive()
            if done and done["status"] == "COMPLETED":
                mins = (walkmode.parse(done["finished_at"]) - walkmode.parse(done["started_at"])).total_seconds() / 60
                walkmode.record_walk(memory, user_id, done["dest_name"], mins)
            st.rerun(scope="app")
        if b4.button("➕ 5 min", key="w_ext", use_container_width=True):
            walkmode.extend(5)
            st.rerun(scope="app")
        c1, c2 = st.columns(2)
        if c1.button("📍 Use my GPS position", key="w_gps", disabled=not real, use_container_width=True):
            walkmode.set_position(geo["latitude"], geo["longitude"])
            st.rerun(scope="app")
        if c2.button("❌ Cancel walk", key="w_cancel", use_container_width=True):
            walkmode.cancel()
            st.rerun(scope="app")
    elif st.button("Start a new walk", key="w_new"):
        walkmode.clear()
        ss.pop("walk_plan", None)
        ss.pop("walk_loaded", None)
        st.rerun(scope="app")
    for e in reversed(s["events"][-6:]):
        st.caption(f"{e['ts'][11:19]}  {e['msg']}")
def _geocode(query: str, near: tuple[float, float]) -> list[dict]:
    """Place search via OpenStreetMap Nominatim (free, no key; keep to ~1 search per second)."""
    lat, lng = near
    d = 0.5  # bias results toward ~50 km around the start point
    params = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 5,
                                     "viewbox": f"{lng - d},{lat + d},{lng + d},{lat - d}", "bounded": 0})
    req = urllib.request.Request("https://nominatim.openstreetmap.org/search?" + params,
                                 headers={"User-Agent": "SafeGuardHackathonDemo/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read().decode())
    return [{"name": x["display_name"], "lat": float(x["lat"]), "lng": float(x["lon"])} for x in data]



def _planner(graph, memory, lat, lng, hour, user_id, ss) -> None:
    saved = _places(graph, user_id)
    mode = st.radio(
        "Where are you walking to?",
        ["Saved place", "Search a place", "Enter coordinates"],
        horizontal=True,
        key="walk_mode"
    )
    dest_name, dest = None, None

    if mode == "Saved place":
        if not saved:
            st.info("No saved places yet. Use Search or Enter coordinates, then press ⭐ Save.")
        else:
            dest_name = st.selectbox("Saved place", list(saved), key="walk_dest")
            dest = saved[dest_name]
    elif mode == "Search a place":
        q = st.text_input("Type a place or address", key="walk_q")
        if st.button("🔎 Search", key="walk_search") and q.strip():
            try:
                ss["walk_results"] = _geocode(q.strip(), (lat, lng))
            except Exception as exc:
                ss["walk_results"] = []
                st.error(f"Search failed (needs internet): {exc}")
            ss["walk_searched"] = True

        results = ss.get("walk_results", [])
        if results:
            i = st.selectbox(
                "Pick the right match",
                range(len(results)),
                key="walk_pick",
                format_func=lambda k: results[k]["name"][:90]
            )
            dest = (results[i]["lat"], results[i]["lng"])
            dest_name = results[i]["name"].split(",")[0]
        elif ss.get("walk_searched"):
            st.caption("No results. Try a more specific name or use Enter coordinates.")
    else:
        c1, c2 = st.columns(2)
        dlat = c1.number_input(
            "Destination latitude",
            value=float(lat),
            format="%.6f",
            key="walk_dlat"
        )
        dlng = c2.number_input(
            "Destination longitude",
            value=float(lng),
            format="%.6f",
            key="walk_dlng"
        )
        label = st.text_input("Name for this place", "Custom destination", key="walk_label")
        dest, dest_name = (dlat, dlng), (label.strip() or "Custom destination")

    if dest is None:
        return

    dist = haversine_m(lat, lng, dest[0], dest[1])
    if dist < 50:
        st.warning("The destination is almost the same as your start location.")
        return
    if dist > 15000:
        st.warning(f"That is {dist / 1000:.0f} km away. Walk mode works best under 15 km.")
        return

    st.caption(
        f"Straight-line distance {dist / 1000:.2f} km. Starting from the sidebar location "
        f"({lat:.4f}, {lng:.4f}); change it in the sidebar to start elsewhere."
    )

    hist = walkmode.walk_history(memory, user_id).get(f"to {dest_name}")
    if hist:
        st.caption(
            f"🧠 You have walked to {dest_name} {hist['count']} time(s); "
            f"average {hist['avg_min']} min."
        )

    bias = st.slider(
        "Safety vs speed",
        0.0,
        1.0,
        0.7,
        key="walk_bias",
        help="0 = shortest, 1 = avoid risk the most"
    )
    c1, c2 = st.columns(2)
    interval = c1.slider("Check in every (s)", 15, 300, 30, key="walk_int")
    grace = c2.slider("Grace after a miss (s)", 10, 120, 15, key="walk_grace")
    st.caption("Demo timers are short. Real use: about 180-300 s and 60 s.")

    with st.expander("📲 Get check-in reminders on your own phone"):
        st.caption("Install the free ntfy app and subscribe to:")
        st.code(_self_topic(graph, user_id), language=None)

    b_plan, b_save = st.columns(2)

    if b_plan.button("🗺️ Plan safe route", key="walk_plan_btn"):
        with st.spinner("Checking real police stations, hospitals and street conditions (OpenStreetMap)..."):
            osm = None
            try:
                area = osm_data.ensure_area(graph, (lat, lng), dest)
                osm, ss["walk_osm_info"] = area["data"], area
            except Exception as exc:
                ss["walk_osm_info"] = {"error": str(exc)}

            ss["walk_plan"] = {
                **routing.plan_route(graph, (lat, lng), dest, hour, bias, osm=osm),
                "dest_name": dest_name
            }

    if mode != "Saved place" and b_save.button("⭐ Save to my places", key="walk_save"):
        graph.add_place(
            user_id,
            "p_" + uuid.uuid4().hex[:6],
            dest_name,
            dest[0],
            dest[1],
            "FREQUENT"
        )
        st.toast(f"Saved '{dest_name}' to your frequent places")

    plan = ss.get("walk_plan")
    if plan and plan["dest"] == list(dest):
        safe, direct = plan["safe"], plan["direct"]
        m1, m2, m3, m4 = st.columns(4)

        m1.metric(
            "Distance",
            f"{safe['distance_m']} m",
            f"{plan['extra_pct']:+d}% vs direct",
            delta_color="off"
        )
        m2.metric(
            "Avg risk",
            f"{safe['exposure']:.2f}",
            f"{safe['exposure'] - direct['exposure']:+.2f} vs direct",
            delta_color="inverse"
        )
        m3.metric(
            "High-risk street",
            f"{safe['high_risk_m']} m",
            f"{safe['high_risk_m'] - direct['high_risk_m']:+d} m",
            delta_color="inverse"
        )
        m4.metric("Police posts nearby", len(safe["police_posts"]))

        st.success(plan["summary"])

        info = ss.get("walk_osm_info") or {}
        if "error" in info:
            st.warning(
                f"Could not reach OpenStreetMap ({info['error']}). Using only the stations and zones already "
                "in the graph."
            )
        elif info:
            c, a = info["counts"], info["added"]
            st.caption(
                f"📡 OpenStreetMap found {c['police']} police stations, {c['hospital']} hospitals, {c['venues']} "
                f"shops/cafes, {c['unlit']} unlit and {c['lit']} lit road segments here. "
                f"{a['stations']} new stations and {a['hospitals']} hospitals were added to the graph."
            )

        def _row(label, m):
            return {
                "Route": label,
                "Distance (m)": m["distance_m"],
                "Avg risk": m["exposure"],
                "High-risk (m)": m["high_risk_m"],
                "Poorly lit (m)": m["unlit_m"],
                "Isolated (m)": m["isolated_m"],
                "Busy (m)": m["busy_m"],
                "Police posts ≤300 m": len(m["police_posts"]),
                "Hospitals ≤500 m": len(m["hospitals"])
            }

        st.dataframe(
            pd.DataFrame([
                _row("Safe route", safe),
                _row("Direct route", direct)
            ]),
            hide_index=True
        )

        if safe["police_posts"]:
            st.caption(
                "👮 Police near the route: " +
                ", ".join(
                    f"{p['name']} ({p['min_dist_m']} m)"
                    for p in safe["police_posts"][:4]
                )
            )

        if safe["hospitals"]:
            st.caption(
                "🏥 Hospitals near the route: " +
                ", ".join(
                    f"{h['name']} ({h['min_dist_m']} m)"
                    for h in safe["hospitals"][:3]
                )
            )

        if safe["zones"]:
            st.caption("Zones on route: " + " → ".join(z["name"] for z in safe["zones"]))

        _route_map(graph, plan["waypoints"], plan["direct_waypoints"], plan["waypoints"][0])

        if st.button("▶️ Start walk (arm dead-man's switch)", type="primary", key="walk_start"):
            expected, note = walkmode.expected_minutes(memory, user_id, dest_name, safe["eta_min"])
            walkmode.start_walk(
                user_id,
                graph.get(f"user:{user_id}")["name"],
                dest_name,
                plan,
                expected,
                hour,
                interval,
                grace,
                _self_topic(graph, user_id),
                note
            )
            ss.pop("walk_plan", None)
            ss.pop("walk_loaded", None)
            st.rerun()


def render_walk_panel(graph, memory, agent, lat, lng, geo, real, hour, user_id) -> None:
    ss = st.session_state
    st.subheader("🚶 Walk Me Home")
    s = walkmode.load()
    if s is None or s["status"] in ("COMPLETED", "CANCELLED"):
        if s and s["status"] == "COMPLETED":
            st.success(f"Walk to {s['dest_name']} completed. Duration saved to memory for next time.")
        elif s:
            st.info("Walk cancelled.")
        _planner(graph, memory, lat, lng, hour, user_id, ss)
        return
    _route_map(graph, s["route"], s["direct_route"], s["position"])
    _timer_panel(memory, geo, real, user_id)
    with st.expander("🧾 Walk audit trail (structured logs)"):
        for a in s["audit"]:
            st.json({k: a[k] for k in ("step", "thought", "action_taken", "retrieved_nodes", "risk_level",
                                       "confidence_score")}, expanded=False)