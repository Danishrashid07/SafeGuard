"""notifier.py: urgent push alerts to contacts (ntfy.sh), agency inbox, and an offline-tolerant retry queue."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

AGENCY_CHANNELS = ("POLICE_DISPATCH", "PATROL_ASSIGN", "EMS_NOTIFY")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ntfy_push(topic: str, title: str, body: str, click: str | None = None, priority: str = "5") -> None:
    headers = {"Title": title, "Priority": priority, "Tags": "rotating_light,sos", "User-Agent": "safeguard/1.0"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        r.read()


class LiveDispatcher:
    """Contacts with an ntfy topic get REAL urgent pushes; agency channels go to data/agency_inbox.jsonl (shown on the
    Agency Console). If the network is down, alerts are QUEUED and delivered automatically when it returns.
    Never contacts real emergency services."""

    def __init__(self, graph, inbox_path: str = "data/agency_inbox.jsonl", demo_mode: bool = True):
        self.graph, self.demo_mode = graph, demo_mode
        self.inbox = Path(inbox_path)
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        self.outbox: list[dict] = []
        self.simulate_offline = False
        self._queue: list[tuple[dict, dict]] = []
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        threading.Thread(target=self._retry_loop, daemon=True, name="dispatch-retry").start()

    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def send_test(self, topic: str) -> None:
        if self.simulate_offline:
            raise OSError("simulated internet outage")
        ntfy_push(topic, "SAFEGUARD TEST", "This is a test alert from SafeGuard. Your phone is set up correctly.", priority="4")

    def _topic(self, action: dict):
        return (self.graph.get(action["recipient_id"]) or {}).get("ntfy_topic")

    def _needs_network(self, action: dict) -> bool:
        return action["channel"] in AGENCY_CHANNELS or (action["channel"] == "SMS_CONTACT" and bool(self._topic(action)))

    def _deliver(self, action: dict) -> tuple[str, bool]:
        """Does the real delivery. Raises on network failure."""
        ch, payload = action["channel"], action["payload"]
        if ch == "SMS_CONTACT":
            topic = self._topic(action)
            if not topic:
                return "SIMULATED_NO_PUSH_TOPIC", False
            m = re.search(r"q=(-?[\d.]+),(-?[\d.]+)", payload["text"])
            click = f"https://maps.google.com/?q={m.group(1)},{m.group(2)}" if m else None
            ntfy_push(topic, "SAFEGUARD EMERGENCY ALERT" + (" (DEMO)" if self.demo_mode else ""),
                      ("[DEMO] " if self.demo_mode else "") + payload["text"], click)
            return "DELIVERED_PUSH_URGENT", True
        if ch in AGENCY_CHANNELS:
            row = {"received_at": _now(), "channel": ch, "agency": action["recipient_name"],
                   "incident_id": payload.get("incident_id"), "risk_level": payload.get("risk_level"),
                   "emergency_type": payload.get("emergency_type"), "user_name": payload.get("user_name"),
                   "user_phone": payload.get("user_phone"), "message": payload.get("user_message"),
                   "location": payload.get("location"), "eta_min": payload.get("eta_min"),
                   "medical_notes": payload.get("medical_notes"), "demo": self.demo_mode}
            with self._file_lock, self.inbox.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return "DELIVERED_TO_AGENCY_CONSOLE", True
        if ch == "CALL_CONTACT":
            return "SIMULATED_CALLS_NEED_TELEPHONY_PROVIDER", False
        return "SIMULATED_DELIVERED", False

    def send(self, action: dict) -> dict:
        receipt = {"status": "PENDING", "simulated": True, "live": False, "message_id": uuid.uuid4().hex[:10],
                   "channel": action["channel"], "recipient_id": action["recipient_id"],
                   "recipient_name": action["recipient_name"], "sent_at": _now(), "payload": action["payload"]}
        try:
            if self.simulate_offline and self._needs_network(action):
                raise OSError("simulated internet outage")
            status, live = self._deliver(action)
        except Exception as exc:
            status, live = "QUEUED_OFFLINE", False
            receipt["error"] = str(exc)
            with self._lock:
                self._queue.append((receipt, action))
        receipt.update(status=status, live=live, simulated=not live)
        with self._lock:
            self.outbox.append(receipt)
        return receipt

    def _retry_loop(self) -> None:
        while True:
            time.sleep(3)
            if self.simulate_offline:
                continue
            with self._lock:
                pending = list(self._queue)
            for receipt, action in pending:
                try:
                    status, live = self._deliver(action)
                except Exception:
                    break  # still offline: keep order, try again later
                receipt.update(status=status + "_LATE", live=live, simulated=not live, delivered_at=_now())
                with self._lock:
                    self._queue.remove((receipt, action))