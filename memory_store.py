"""memory_store.py: persistent profile + episodic memory in SQLite. No API key, no vector DB.
Implements the MemoryLike interface used by agent_workflow.py."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_STOP = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is", "are", "was", "were", "by",
         "with", "i", "me", "my", "it", "this", "that", "near"}


def _tok(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOP and len(t) > 1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: str = "data/memory.sqlite"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, text TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_episodes_user ON episodes(user_id, id);
            """)
            self._db.commit()

    # ---------------------------------------------------------------- profile
    def get_profile(self, user_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT data FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def update_profile(self, user_id: str, updates: dict) -> dict:
        """Shallow merge. A value of None deletes that key."""
        profile = self.get_profile(user_id)
        for k, v in updates.items():
            if v is None:
                profile.pop(k, None)
            else:
                profile[k] = v
        with self._lock:
            self._db.execute(
                "INSERT INTO profiles(user_id, data, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (user_id, json.dumps(profile, ensure_ascii=False), _now()))
            self._db.commit()
        return profile

    # --------------------------------------------------------------- episodes
    def add_episode(self, user_id: str, text: str, metadata: Optional[dict] = None) -> str:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO episodes(user_id, text, metadata, created_at) VALUES(?,?,?,?)",
                (user_id, text, json.dumps(metadata or {}, ensure_ascii=False), _now()))
            self._db.commit()
            return f"ep-{cur.lastrowid}"

    def list_episodes(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text, metadata, created_at FROM episodes WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit)).fetchall()
        return [{"id": f"ep-{r[0]}", "text": r[1], "metadata": json.loads(r[2]), "created_at": r[3]} for r in rows]

    def recall(self, user_id: str, query: str, k: int = 3, zone_id=None) -> list[dict]:
        """Keyword relevance (IDF-weighted overlap) plus a small recency boost.
        If nothing matches, returns the most recent episodes so recent alerts still inform the agent."""
        episodes = self.list_episodes(user_id, limit=500)
        if not episodes:
            return []
        docs = [set(_tok(e["text"])) for e in episodes]
        q = set(_tok(query))
        n = len(docs)
        df = {t: sum(1 for d in docs if t in d) for t in q}
        now = datetime.now(timezone.utc)
        scored = []
        for e, d in zip(episodes, docs):
            overlap = q & d
            if not overlap:
                continue
            idf = sum(math.log(1 + n / (1 + df[t])) for t in overlap)
            age_days = (now - datetime.fromisoformat(e["created_at"])).total_seconds() / 86400
            scored.append({**e, "score": round(idf / (1 + 0.05 * len(d)) + 0.2 / (1 + age_days), 3),
                           "match": "keyword match"})
        scored.sort(key=lambda x: -x["score"])
        if scored:
            return scored[:k]
        return [{**e, "score": 0.0, "match": "most recent"} for e in episodes[:k]]

    # ---------------------------------------------------------------- privacy
    def delete_user_data(self, user_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM profiles WHERE user_id=?", (user_id,))
            self._db.execute("DELETE FROM episodes WHERE user_id=?", (user_id,))
            self._db.commit()


def seed_demo_profile(mem: MemoryStore, user_id: str = "u1") -> bool:
    """Fictional demo profile. No-op if a profile already exists."""
    if mem.get_profile(user_id):
        return False
    mem.update_profile(user_id, {
        "full_name": "Sam Rivera",
        "medical_notes": "Asthma (carries inhaler). Allergic to penicillin. Blood group O+.",
        "preferred_contacts": ["Jordan", "Mom"],  # overrides graph priority order: Jordan is called first
        "do_not_notify": [],
        "frequent_routes": ["Home -> University Library (weekdays 8am)", "Gym -> Home (evenings)"],
        "language": "en",
    })
    mem.add_episode(user_id, "MEDIUM HARASSMENT alert near Downtown Night Market: verbal harassment on the way home",
                    {"risk_level": "MEDIUM", "seeded": True})
    return True