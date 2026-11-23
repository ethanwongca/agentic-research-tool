"""Run the coding agent (Claude Code or Codex) headless from the UI, so 'Build' is one click.

The agent runs inside the project, so the project's hooks and AGENTS.md still gate everything it does.
Its progress is parsed into short events the UI shows live.
"""
from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

PROMPT = """Implement the arx box `{sid}` exactly as the researcher specified. Follow AGENTS.md strictly.
1. Run `arx check {sid}`. If the box is not APPROVED or FAILING, stop and say why in one sentence.
2. If {file} does not exist, run `arx scaffold {sid}`. Implement it literally: the approach, the decisions
   (read them from params), the constraints, and the input/output contract in the docstring.
3. Run `arx verify {sid}`. If it fails, fix implementation bugs only. Never change the method, the
   contract or the checks. Repeat until it passes or arx says the box is BLOCKED.
4. If the spec leaves a choice open, do not guess: run `arx ask {sid} "<neutral question>"` and stop.
5. End with a short factual report: what you implemented and the check results."""

AGENTS = {
    "claude": ["claude", "-p", "{prompt}", "--output-format", "stream-json", "--verbose",
               "--permission-mode", "acceptEdits", "--allowedTools", "Bash(arx:*)"],
    "codex": ["codex", "exec", "--full-auto", "{prompt}"],
}


class AgentRunner:
    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.state: dict = {"running": False, "sid": None, "events": [], "exit": None}

    def status(self) -> dict:
        with self.lock:
            s = dict(self.state)
            s["events"] = list(self.state["events"][-300:])
            return s

    def _event(self, kind: str, msg: str) -> None:
        msg = (msg or "").strip()
        if not msg:
            return
        with self.lock:
            self.state["events"].append({"t": round(time.time() - self.state["started"], 1), "kind": kind,
                                         "msg": msg[:1500]})

    def start(self, root: Path, sid: str, file: str, agent: str = "claude") -> None:
        with self.lock:
            if self.state["running"]:
                raise ValueError(f"the agent is already working on '{self.state['sid']}'")
        tmpl = AGENTS.get(agent) or shlex.split(agent) + ["{prompt}"]
        exe = shutil.which(tmpl[0])
        if not exe:
            raise ValueError(f"'{tmpl[0]}' isn't installed or on PATH. Install it, or change the agent in Rules.")
        prompt = PROMPT.format(sid=sid, file=file)
        cmd = [exe] + [a.replace("{prompt}", prompt) for a in tmpl[1:]]
        log = root / ".arx" / "runs" / f"{sid}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.state = {"running": True, "sid": sid, "agent": tmpl[0], "events": [], "exit": None,
                          "started": time.time(), "log": str(log.relative_to(root))}
        self.proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, bufsize=1)
        self._event("info", f"{tmpl[0]} started on '{sid}'")
        threading.Thread(target=self._pump, args=(log,), daemon=True).start()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._event("info", "stopped by researcher")

    def _pump(self, log: Path) -> None:
        with log.open("w") as fh:
            for line in self.proc.stdout:
                fh.write(line)
                self._parse(line)
        code = self.proc.wait()
        with self.lock:
            self.state["running"] = False
            self.state["exit"] = code
        self._event("done" if code == 0 else "error", "finished" if code == 0 else f"exited with code {code}")

    def _parse(self, line: str) -> None:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            self._event("text", line)
            return
        t = ev.get("type")
        if t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "text":
                    self._event("text", c.get("text", ""))
                elif c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    what = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    self._event("tool", f"{c.get('name')}: {what}")
        elif t == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                    body = c.get("content")
                    if isinstance(body, list):
                        body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                    self._event("blocked" if "arx gate" in str(body) else "error", str(body)[:600])
        elif t == "result":
            self._event("result", ev.get("result") or "")


RUNNER = AgentRunner()
