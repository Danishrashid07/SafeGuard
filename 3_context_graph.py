"""Inspect the context graph straight from FalkorDB: why was someone alerted, reasoning steps, agents, memory."""
import pandas as pd
import streamlit as st

from falkor_graph import (agent_activity, agent_topology, connect, incident_path, incident_steps, plain,
                          recent_incidents)

st.set_page_config(page_title="Context graph", page_icon="🧠", layout="wide")
st.title("🧠 Context graph (FalkorDB)")


@st.cache_resource
def handle():
    return connect()


try:
    db, fg = handle()
    counts = fg.ro_query("MATCH (n) RETURN labels(n)[0], count(n) ORDER BY count(n) DESC").result_set
except Exception as exc:
    st.error(f"FalkorDB is not reachable: {exc}")
    st.stop()

st.caption("This page reads only from FalkorDB, not from the app's memory. "
           "FalkorDB Browser (Docker image) is at http://localhost:3000.")
cols = st.columns(max(1, min(len(counts), 8)))
for c, (label, n) in zip(cols, counts[:8]):
    c.metric(label, n)


def q(s) -> str:
    return str(s).replace('"', "'")[:70]


def path_dot(p: dict, iid: str) -> str:
    d = p["decision"]
    colors = {"CRITICAL": "#b71c1c", "HIGH": "#e65100", "MEDIUM": "#f9a825", "LOW": "#2e7d32"}
    out = ['digraph G { rankdir=LR; bgcolor="transparent"; node [style="filled", fontname="Helvetica", fontsize=10, fontcolor="white"];',
           f'  inc [shape=box, fillcolor="#455a64", label="Incident\\n{q(iid)}"];',
           f'  dec [shape=diamond, fillcolor="{colors.get(d["level"], "#444")}", label="Decision\\n{d["level"]} {d["score"]:.2f}"];',
           '  inc -> dec [label="DECIDED"];']
    for i, e in enumerate(p["evidence"]):
        out.append(f'  e{i} [shape=ellipse, fillcolor="#3949ab", label="{q(e["label"])}\\n{q(e["name"])}"];')
        out.append(f'  dec -> e{i} [label="BASED_ON: {q(e["why"])}", fontsize=8];')
    for i, a in enumerate(p["actions"]):
        out.append(f'  a{i} [shape=box, fillcolor="#00897b", label="{q(a["channel"])}"];')
        out.append(f'  dec -> a{i} [label="LED_TO"];')
        if a["recipient_id"]:
            out.append(f'  r{i} [shape=ellipse, fillcolor="#6d4c41", label="{q(a["recipient_label"])}\\n{q(a["recipient"])}"];')
            out.append(f'  a{i} -> r{i} [label="SENT_TO"];')
    return "\n".join(out + ["}"])


incidents = recent_incidents(fg)
t_why, t_steps, t_agents, t_mem, t_cy = st.tabs(["Why? (path)", "Reasoning steps", "Agents & shared state", "Memory", "Cypher console"])

with t_why:
    if not incidents:
        st.info("No incidents recorded yet. Trigger one in the main app.")
    else:
        pick = st.selectbox("Incident", range(len(incidents)), format_func=lambda i: (
            f"{incidents[i]['ts'][:19]} · {incidents[i]['risk']} {incidents[i]['type']} · {incidents[i]['status']} "
            f"({incidents[i]['trigger']}) · {incidents[i]['id']}"))
        iid = incidents[pick]["id"]
        p = incident_path(fg, iid)
        if not p["decision"]:
            st.warning(f"This incident was {incidents[pick]['status']}: no dispatch decision was made.")
        else:
            d = p["decision"]
            st.markdown(f"**Decision:** {d['level']} (score {d['score']:.2f}, confidence {d['confidence']:.2f})  \n{d['rationale']}")
            st.graphviz_chart(path_dot(p, iid))
            if p["actions"]:
                who = st.selectbox("Ask the graph: why was this recipient alerted?", range(len(p["actions"])), format_func=lambda i: (
                    f"{p['actions'][i]['channel']} → {p['actions'][i]['recipient']}"))
                a = p["actions"][who]
                st.success(f"(Incident {iid}) —DECIDED→ (Decision {d['level']}) —LED_TO→ (Action {a['channel']}) "
                           f"—SENT_TO→ ({a['recipient_label']} {a['recipient']})")
                st.write("Reason: " + a["reason"])
                st.write("The decision was based on:")
                st.dataframe(pd.DataFrame(p["evidence"]).drop(columns=["id"]), hide_index=True)
            with st.expander("Cypher behind this page"):
                for c in p["cypher"]:
                    st.code(c, language="cypher")

with t_steps:
    if incidents:
        st.dataframe(pd.DataFrame(incident_steps(fg, iid)), hide_index=True)
        st.caption("Each step is a (Step) node linked NEXT to the following step, PERFORMED by an Agent, and RETRIEVED the nodes it used.")

with t_agents:
    c1, c2 = st.columns(2)
    c1.markdown("**Agent hand-offs**")
    c1.dataframe(pd.DataFrame(agent_topology(fg)), hide_index=True)
    c2.markdown("**Task board (shared state)**")
    tasks = agent_activity(fg)
    c2.dataframe(pd.DataFrame(tasks), hide_index=True) if tasks else c2.info("No tasks yet. Trigger a walk escalation or phone SOS.")

with t_mem:
    rows = fg.ro_query("MATCH (u:User)-[:REMEMBERS]->(e:Episode) OPTIONAL MATCH (e)-[:ABOUT_ZONE]->(z:Zone) "
                       "RETURN u.name, e.created_at, e.text, z.name ORDER BY e.created_at DESC LIMIT 30").result_set
    st.dataframe(pd.DataFrame(rows, columns=["person", "when", "episode", "zone"]), hide_index=True)

with t_cy:
    st.caption("Read-only (ro_query): write queries are rejected by the database.")
    default = "MATCH (s:Station)-[:JURISDICTION_OVER]->(z:Zone) RETURN s.name, z.name"
    text = st.text_area("Cypher", default, height=100)
    if st.button("Run"):
        try:
            res = fg.ro_query(text)
            names = [h[1] if isinstance(h, (list, tuple)) else str(h) for h in res.header]
            st.dataframe(pd.DataFrame([[str(plain(v)) for v in row] for row in res.result_set[:200]], columns=names), hide_index=True)
        except Exception as exc:
            st.error(str(exc))