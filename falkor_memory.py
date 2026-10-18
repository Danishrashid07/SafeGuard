"""falkor_memory.py: long-term memory stored IN the graph: (User)-[:REMEMBERS]->(Episode)-[:ABOUT_ZONE]->(Zone),
(Episode)-[:FROM_INCIDENT]->(Incident). Profile lives on the User node. Recall uses graph proximity (same zone) first."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from falkor_graph import FalkorSafetyGraph

_STOP = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is", "are", "was", "were", "by", "with",
         "i", "me", "my", "it", "this", "that", "near"}


def _tok(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOP and len(t) > 1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FalkorMemory:
    def __init__(self, graph: FalkorSafetyGraph):
        self.graph = graph

    @staticmethod
    def _uid(user_id: str) -> str:
        return f"user:{user_id}"

    def get_profile(self, user_id: str) -> dict:
        try:
            rows = self.graph._q("MATCH (u:User {id:$u}) RETURN u.profile", {"u": self._uid(user_id)}, ro=True).result_set
            return json.loads(rows[0][0]) if rows and rows[0][0] else {}
        except Exception as exc:
            self.graph._fail(exc)
            return {}

    def update_profile(self, user_id: str, updates: dict) -> dict:
        profile = self.get_profile(user_id)
        for k, v in updates.items():
            if v is None:
                profile.pop(k, None)
            else:
                profile[k] = v
        self.graph._w("MATCH (u:User {id:$u}) SET u.profile=$p, u.profile_updated=$t",
                      {"u": self._uid(user_id), "p": json.dumps(profile, ensure_ascii=False), "t": _now()})
        return profile

    def add_episode(self, user_id: str, text: str, metadata: Optional[dict] = None) -> str:
        eid, meta = f"ep:{uuid.uuid4().hex[:10]}", metadata or {}
        g = self.graph
        g._w("MATCH (u:User {id:$u}) CREATE (u)-[:REMEMBERS]->(e:Episode {id:$e, text:$t, metadata:$m, created_at:$c})",
             {"u": self._uid(user_id), "e": eid, "t": text, "m": json.dumps(meta, default=str), "c": _now()})
        if meta.get("zone_id"):
            g._w("MATCH (e:Episode {id:$e}), (z:Zone {id:$z}) MERGE (e)-[:ABOUT_ZONE]->(z)", {"e": eid, "z": meta["zone_id"]})
        if meta.get("incident_id"):
            g._w("MATCH (e:Episode {id:$e}), (i:Incident {id:$i}) MERGE (e)-[:FROM_INCIDENT]->(i)",
                 {"e": eid, "i": meta["incident_id"]})
        return eid

    def list_episodes(self, user_id: str, limit: int = 20) -> list[dict]:
        try:
            rows = self.graph._q("MATCH (u:User {id:$u})-[:REMEMBERS]->(e:Episode) RETURN e ORDER BY e.created_at DESC LIMIT $n",
                                 {"u": self._uid(user_id), "n": limit}, ro=True).result_set
        except Exception as exc:
            self.graph._fail(exc)
            return []
        return [{"id": e.properties["id"], "text": e.properties.get("text", ""),
                 "metadata": json.loads(e.properties.get("metadata", "{}")), "created_at": e.properties.get("created_at", "")}
                for (e,) in rows]

    def recall(self, user_id: str, query: str, k: int = 3, zone_id: Optional[str] = None) -> list[dict]:
        try:
            rows = self.graph._q("MATCH (u:User {id:$u})-[:REMEMBERS]->(e:Episode) OPTIONAL MATCH (e)-[:ABOUT_ZONE]->(z:Zone) "
                                 "RETURN e, z.id ORDER BY e.created_at DESC LIMIT 200",
                                 {"u": self._uid(user_id)}, ro=True).result_set
        except Exception as exc:
            self.graph._fail(exc)
            return []
        q, now, scored = set(_tok(query)), datetime.now(timezone.utc), []

        def item(e, score, why):
            p = e.properties
            return {"id": p["id"], "text": p.get("text", ""), "metadata": json.loads(p.get("metadata", "{}")),
                    "created_at": p.get("created_at", ""), "score": round(score, 3), "match": why}

        for e, zid in rows:
            toks = set(_tok(e.properties.get("text", "")))
            overlap, same_zone = len(q & toks), bool(zone_id and zid == zone_id)
            if not overlap and not same_zone:
                continue
            age = (now - datetime.fromisoformat(e.properties["created_at"])).total_seconds() / 86400
            score = overlap / (1 + 0.1 * len(toks)) + (1.0 if same_zone else 0.0) + 0.2 / (1 + age)
            why = " + ".join(x for x in ("same zone" if same_zone else "", "keyword match" if overlap else "") if x)
            scored.append(item(e, score, why))
        scored.sort(key=lambda x: -x["score"])
        return scored[:k] if scored else [item(e, 0.0, "most recent") for e, _ in rows[:k]]

    def delete_user_data(self, user_id: str) -> None:
        self.graph._w("MATCH (u:User {id:$u})-[:REMEMBERS]->(e:Episode) DETACH DELETE e", {"u": self._uid(user_id)})
        self.graph._w("MATCH (u:User {id:$u}) REMOVE u.profile", {"u": self._uid(user_id)})