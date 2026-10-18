"""sos_server.py: HTTP trigger so a phone shortcut/app can raise an SOS without opening SafeGuard."""
from __future__ import annotations

import hmac
import json
import secrets
import socket
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import tracking

LAST = Path("data/sos_incident.json")


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no traffic is sent; just picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def load_last(max_age_s: int = 600) -> Optional[dict]:
    if not LAST.exists():
        return None
    try:
        data = json.loads(LAST.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["received_at"])).total_seconds()
        return data if age <= max_age_s else None
    except Exception:
        return None


class SosServer:
    def __init__(self, agent, graph, user_id: str, port: int = 8765, cooldown_s: int = 20):
        self.agent, self.graph, self.user_id = agent, graph, user_id
        self.port, self.cooldown_s = port, cooldown_s
        self.error: Optional[str] = None
        self._last = 0.0
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self.token = (graph.get(f"user:{user_id}") or {}).get("sos_token")
        if not self.token:
            self.token = secrets.token_urlsafe(16)
            graph.set_prop(f"user:{user_id}", "sos_token", self.token)

    def start(self) -> bool:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep tokens out of the console
                pass

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self) -> None:
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if self.command == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n:
                        try:
                            q.update(json.loads(self.rfile.read(n).decode()))
                        except Exception:
                            pass
                if u.path == "/health":
                    return self._send(200, {"ok": True})
                if not hmac.compare_digest(str(q.get("token", "")), outer.token):
                    return self._send(401, {"error": "bad token"})
                if u.path == "/sos":
                    return self._send(*outer.handle_sos(q))
                if u.path == "/cancel":
                    return self._send(*outer.handle_cancel(q))
                self._send(404, {"error": "not found"})

            do_GET = _route
            do_POST = _route

        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        except OSError as exc:
            self.error = f"port {self.port} unavailable ({exc}). Is another SafeGuard still running?"
            return False
        threading.Thread(target=httpd.serve_forever, daemon=True, name="sos-http").start()
        return True

    def _location(self, q: dict):
        lat, lng = q.get("lat"), q.get("lng", q.get("lon"))
        acc = q.get("acc")
        if lat is not None and lng is not None:
            lat, lng = float(lat), float(lng)
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError("coordinates out of range")
            return lat, lng, (float(acc) if acc else 25.0), "phone GPS"
        live = self.graph.get(f"loc:now:{self.user_id}")
        if not live:
            raise ValueError("no location sent and none stored")
        return live["lat"], live["lng"], None, "last known location (phone sent none)"

    def handle_sos(self, q: dict):
        now = time.time()
        with self._lock:
            if now - self._last < self.cooldown_s:
                return 429, {"status": "cooldown", "retry_after_s": int(self.cooldown_s - (now - self._last))}
            self._last = now
        try:
            grace = max(0, min(30, int(float(q.get("grace", 0)))))
            lat, lng, acc, source = self._location(q)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        trigger = {"type": "hardware_button", "lat": lat, "lng": lng, "accuracy_m": acc,
                   "local_hour": int(q.get("hour", datetime.now().hour)), "message": str(q.get("msg", "")).strip()}
        rid = uuid.uuid4().hex[:8]
        if grace:
            timer = threading.Timer(grace, self._run, args=(rid, trigger))
            self._pending[rid] = timer
            timer.start()
            return 202, {"status": "pending", "request_id": rid, "cancel_within_s": grace, "location": source}
        threading.Thread(target=self._run, args=(rid, trigger), daemon=True).start()
        return 202, {"status": "accepted", "request_id": rid, "location": source}

    def handle_cancel(self, q: dict):
        timer = self._pending.pop(str(q.get("id", "")), None)
        if timer:
            timer.cancel()
            return 200, {"status": "cancelled"}
        return 404, {"status": "nothing pending with that id"}

    def _run(self, rid: str, trigger: dict) -> None:
        self._pending.pop(rid, None)
        try:
            crew = getattr(self.agent, "crew", None)
            final = (crew.run_incident("PhoneBridgeAgent", self.user_id, trigger) if crew
                     else self.agent.handle_incident(self.user_id, trigger))
            logs = final.pop("audit_log", [])
            if not final.get("valid"):
                return
            tracking.start_session(final["incident_id"], self.graph.get(f"user:{self.user_id}")["name"],
                                   final["risk_level"], trigger["lat"], trigger["lng"], trigger.get("accuracy_m"))
            LAST.write_text(json.dumps({"received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                        "request_id": rid, "final": final, "logs": logs, "trigger": trigger,
                                        "event_ts": logs[0]["timestamp"] if logs else None}, default=str),
                            encoding="utf-8")
        except Exception:
            traceback.print_exc()