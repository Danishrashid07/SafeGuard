"""crew.py: agents coordinate through Task nodes in FalkorDB. Guardian/PhoneBridge agents CREATE an incident task;
the Dispatch worker CLAIMS it, runs the incident pipeline and marks it COMPLETED. If the graph is down, falls back
to running the incident directly: an emergency never waits for the database."""
from __future__ import annotations

import threading
import time
import traceback
import uuid


class Crew:
    def __init__(self, agent, graph, poll_s: float = 0.4):
        self.agent, self.graph, self.poll_s = agent, graph, poll_s
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, object] = {}

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="dispatch-worker").start()

    def run_incident(self, creator: str, user_id: str, trigger: dict, timeout: float = 120.0) -> dict:
        tid = "task-" + uuid.uuid4().hex[:8]
        ev = threading.Event()
        self._events[tid] = ev
        try:
            self.graph.create_task(tid, creator, "INCIDENT", {"user_id": user_id, "trigger": trigger})
        except Exception as exc:  # graph unreachable: do not delay the emergency
            self._events.pop(tid, None)
            self.graph._fail(exc)
            return self.agent.handle_incident(user_id, trigger)
        if not ev.wait(timeout):
            self._events.pop(tid, None)
            raise TimeoutError("dispatch worker did not finish in time")
        res = self._results.pop(tid)
        if isinstance(res, Exception):
            raise res
        return res

    def _loop(self) -> None:
        while True:
            try:
                claimed = self.graph.claim_task("DispatchAgent", "INCIDENT")
                if claimed:
                    self._work(*claimed)
                else:
                    time.sleep(self.poll_s)
            except Exception:
                traceback.print_exc()
                time.sleep(2)

    def _work(self, tid: str, payload: dict) -> None:
        ev = self._events.get(tid)
        try:
            final = self.agent.handle_incident(payload["user_id"], payload["trigger"])
            self.graph.complete_task(tid, "DispatchAgent", final.get("incident_id"))
            self._results[tid] = final
        except Exception as exc:
            self.graph.fail_task(tid, "DispatchAgent", str(exc))
            self._results[tid] = exc
        finally:
            if ev:
                ev.set()