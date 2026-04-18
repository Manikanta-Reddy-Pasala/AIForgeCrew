"""Paperclip ↔ Hermes bridge.

Polls real Paperclip's API for tasks assigned to our agents, dispatches to the
right Hermes.Agent, posts results back. Heartbeat-driven per Paperclip's
bring-your-own-agent model.

Minimal implementation — assumes Paperclip API shape:
  GET  /api/agents/<agent_id>/tasks?status=assigned
  POST /api/tasks/<task_id>/heartbeat   (agent alive)
  POST /api/tasks/<task_id>/complete    (body: result)
  POST /api/tasks/<task_id>/comment     (body: text)

Adapt the endpoint paths in `PaperclipClient` if the upstream API differs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from hermes.agent import Agent
from hermes.llm import LLMClient

from .config import PaperclipConfig
from .store import Store


ROLE_TO_AGENT_ID = {
    "em": "engineering_manager",
    "tester": "tester",
    "sr-developer": "sr_developer",
    "sr-architect": "sr_architect",
}


@dataclass
class PaperclipClient:
    base_url: str = "http://localhost:3100"
    timeout_s: float = 30.0

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url.rstrip('/')}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"paperclip {method} {path} → HTTP {e.code}: {e.read()!r}") from e
        return json.loads(raw) if raw else {}

    def list_tasks(self, agent_id: str) -> list[dict]:
        return self._req("GET", f"/api/agents/{agent_id}/tasks?status=assigned").get("tasks", [])

    def heartbeat(self, task_id: str) -> None:
        self._req("POST", f"/api/tasks/{task_id}/heartbeat")

    def comment(self, task_id: str, text: str) -> None:
        self._req("POST", f"/api/tasks/{task_id}/comment", {"body": text})

    def complete(self, task_id: str, result: dict) -> None:
        self._req("POST", f"/api/tasks/{task_id}/complete", {"result": result})


def run_once(repo_root: Path, role: str, *, paperclip_url: str | None = None) -> int:
    """One poll cycle: pick up any assigned tasks for `role`, run through Hermes,
    report back. Returns number of tasks processed.
    """
    pc = PaperclipClient(base_url=paperclip_url or "http://localhost:3100")
    store = Store(repo_root / ".paperclip" / "paperclip.db")
    cfg = PaperclipConfig.load(repo_root)

    # LM Studio client for all local roles; cloud for em (set by Agent.load).
    client = LLMClient.local() if role != "em" else LLMClient.cloud()
    agent = Agent.load(repo_root, role, client=client)

    tasks = pc.list_tasks(ROLE_TO_AGENT_ID[role])
    for t in tasks:
        tid = t["id"]
        pc.heartbeat(tid)
        pc.comment(tid, f"{role} picking up via Hermes")
        try:
            reply = agent.run(ticket_id=tid,
                              user_message=t.get("prompt") or t.get("body") or "",
                              store=store, cfg=cfg)
            pc.complete(tid, {
                "ok": True,
                "role": role,
                "content": reply.content[:8000],
                "usage": reply.usage,
            })
        except Exception as e:
            pc.comment(tid, f"FAIL {role}: {type(e).__name__}: {e}")
            pc.complete(tid, {"ok": False, "error": str(e)})
    store.close()
    return len(tasks)


def run_loop(repo_root: Path, role: str, *, interval_s: float = 5.0,
             paperclip_url: str | None = None) -> None:
    """Long-running heartbeat loop. Ctrl-C to stop."""
    while True:
        try:
            n = run_once(repo_root, role, paperclip_url=paperclip_url)
            if n == 0:
                time.sleep(interval_s)
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f"[bridge {role}] error: {e}")
            time.sleep(interval_s * 2)
