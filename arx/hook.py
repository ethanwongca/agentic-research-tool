"""Claude Code hooks: the enforcement block.

PreToolUse        denies edits that would bypass the researcher's spec or touch verified code.
SessionStart      injects pipeline status + the protocol into the agent's context.
UserPromptSubmit  re-injects a one-line-per-stage status every turn so the agent doesn't drift.

These are guardrails against drift, not a security boundary; `arx check` (also run by
the git pre-commit hook) detects anything that slips through via file hashes.
"""
from __future__ import annotations

import json
import os
import re
import sys

from .spec import ARX_DIR, SPEC_FILE, Project, find_root
from .state import State

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
HUMAN_ONLY = re.compile(r"\barx\b[^|;&\n]*\b(approve|unlock|retry)\b|-m\s+arx\b[^|;&\n]*\b(approve|unlock|retry)\b")
UI_API = re.compile(r"(localhost|127\.0\.0\.1|0\.0\.0\.0):\d+/api/")
WRITEY = re.compile(r"(?<![0-9&])>(?!&)|\btee\b|\bsed\b[^|;]*\s-i|\bperl\b[^|;]*\s-[a-z]*i|\bmv\b|\bcp\b|\brm\b|"
                    r"\btruncate\b|\bdd\b|\bpython3?\b|\bnode\b|\bruby\b|\bgit\s+(checkout|restore|reset|stash|apply|mv|rm)\b|"
                    r"\bopen\(|\bwrite_text\b|\bchmod\b|\bln\b|\bpatch\b")

STOP_MSG = (
    "\n\nSTOP. Do not work around this and do not invent the missing answers yourself.\n"
    "1. Ask the researcher each open item with AskUserQuestion (offer concrete options when helpful).\n"
    "2. Record their answers: arx answer <stage> <key> \"<their words>\"  (new questions: arx ask <stage> \"...\" --key k).\n"
    "3. Tell them to review and approve in `arx ui` or with `arx approve <stage>` in their own terminal.\n"
    "Only the researcher can approve or unlock."
)


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": "[arx gate] " + reason}}


def _rel(root, path: str) -> str | None:
    ap = os.path.realpath(os.path.join(str(root), path)) if not os.path.isabs(path) else os.path.realpath(path)
    try:
        rel = os.path.relpath(ap, os.path.realpath(str(root)))
    except ValueError:
        return None
    return None if rel.startswith("..") else rel.replace(os.sep, "/")


def check_path(project: Project, state: State, rel: str) -> dict | None:
    if rel == ARX_DIR or rel.startswith(ARX_DIR + "/"):
        return deny(f"{rel} is arx's internal state (approvals, locks, results). Agents may not edit it.")
    if rel == SPEC_FILE:
        return deny(f"{SPEC_FILE} is the researcher's spec. Do not edit it directly. Use `arx ask` to raise a "
                    f"question and `arx answer` to record an answer the researcher gave you; they edit and approve "
                    f"the rest in `arx ui`." + STOP_MSG)
    sid = project.owner_of(rel)
    if sid:
        ok, reasons = state.can_edit(sid)
        if ok:
            return None
        st = state.status(sid)["status"]
        if st == "blocked":
            return deny(f"stage '{sid}' is BLOCKED: verification failed {state.attempts.get(sid, 0)} times. STOP. "
                        f"Report to the researcher, factually: which checks fail, the error output, and what you "
                        f"changed in each attempt. Do NOT propose a new method, model, parameter or stage. The "
                        f"researcher decides the next box (revise it, insert a box, try an alternative, or grant "
                        f"more attempts).")
        if st in ("verified", "stale", "tampered"):
            return deny(f"{rel} belongs to stage '{sid}', which passed verification and is LOCKED. Do not modify "
                        f"verified stages. If you believe it is wrong, explain why to the researcher and ask them to "
                        f"run `arx unlock {sid}`.\n- " + "\n- ".join(reasons))
        return deny(f"{rel} belongs to stage '{sid}', which is not ready to implement:\n- "
                    + "\n- ".join(reasons) + STOP_MSG)
    stage_dir = project.policy["stage_dir"].rstrip("/") + "/"
    if rel.startswith(stage_dir) and rel.endswith(".py") and not rel.endswith("__init__.py"):
        return deny(f"{rel} is in {stage_dir} but no stage owns it. Stage code must be declared in a stage's "
                    f"`files:` list (ask the researcher), so it is covered by verification and locking.")
    return None


def check_bash(project: Project, state: State, cmd: str) -> dict | None:
    if HUMAN_ONLY.search(cmd):
        return deny("approve/unlock/retry are researcher-only actions. Ask the researcher to do it in `arx ui` or their "
                    "own terminal.")
    if UI_API.search(cmd):
        return deny("Agents may not call the arx UI's API; it is the researcher's interface.")
    if not WRITEY.search(cmd):
        return None
    protected = [SPEC_FILE, ARX_DIR + "/"]
    for sid in project.stages:
        if not state.can_edit(sid)[0]:
            protected += project.stage_files(sid)
    hits = [p for p in protected if p in cmd]
    if hits:
        return deny(f"this shell command looks like it modifies protected files: {', '.join(hits)}. Spec and "
                    f"verified/unapproved stage code can't be changed from the shell. Use `arx run`/`arx verify` "
                    f"to execute stages." + STOP_MSG)
    return None


def pre_tool_use(payload: dict) -> dict | None:
    root = find_root(payload.get("cwd"))
    if not root:
        return None
    project = Project(root)
    state = State(project)
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    if tool in EDIT_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path")
        rel = _rel(root, path) if path else None
        return check_path(project, state, rel) if rel else None
    if tool == "Bash":
        return check_bash(project, state, ti.get("command", ""))
    return None


def status_lines(project: Project, state: State) -> list[str]:
    lines = []
    for sid in project.topo_order():
        st = state.status(sid)
        n = len(st["reasons"])
        extra = f" ({n} open item{'s' * (n != 1)})" if st["status"] == "draft" else ""
        lines.append(f"  {sid}: {st['status'].upper()}{extra}")
    return lines


PROTOCOL = """You are working in an arx-governed research pipeline. pipeline.yaml is the researcher's spec and
it overrides your own judgment. Rules:
- You implement; the researcher drives. Never propose stages, methods, models, parameters or thresholds,
  and never pick ones the spec leaves open. If something is unspecified, STOP and ask a neutral question
  (AskUserQuestion, no suggested answers), then record it with `arx ask` / `arx answer`.
- The researcher creates every box on the board. You never add, split or redesign stages.
- If verification keeps failing, the stage BLOCKS. Report facts (failing checks, errors, what you tried), not ideas.
- Only implement stages that are APPROVED or FAILING, in pipeline order. Start with `arx scaffold <stage>`.
- Read every researcher choice from `params`; do not hardcode them.
- A stage is done only when `arx verify <stage>` passes; it then locks. Never edit VERIFIED stages.
- Do not weaken checks. Tests and invariants live in the spec, which you cannot edit.
- Useful: `arx status`, `arx next`, `arx check <stage>`, `arx run <stage>`."""


def session_context(project: Project, state: State, full: bool) -> str:
    head = "arx pipeline status:\n" + "\n".join(status_lines(project, state))
    return (PROTOCOL + "\n\n" + head + "\nRun `arx next` for what to do now.") if full else head


def main(event: str) -> int:
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except json.JSONDecodeError:
        payload = {}
    try:
        if event == "pre-tool-use":
            out = pre_tool_use(payload)
            if out:
                print(json.dumps(out))
            return 0
        root = find_root(payload.get("cwd"))
        if not root:
            return 0
        project = Project(root)
        state = State(project)
        name = "SessionStart" if event == "session-start" else "UserPromptSubmit"
        ctx = session_context(project, state, full=event == "session-start")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": ctx}}))
        return 0
    except Exception as e:  # fail closed for edits, open for context
        if event == "pre-tool-use":
            tool = payload.get("tool_name", "")
            cmd = (payload.get("tool_input") or {}).get("command", "")
            if tool in EDIT_TOOLS or (tool == "Bash" and WRITEY.search(cmd)):
                print(json.dumps(deny(f"arx hook error ({type(e).__name__}: {e}). Fix pipeline.yaml or ask the "
                                      f"researcher; refusing the action until the gate can evaluate it.")))
        else:
            print(f"arx hook error: {e}", file=sys.stderr)
        return 0
