"""arx: spec-gated research pipelines for coding agents."""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
from pathlib import Path

import yaml

from . import hook
from .spec import SPEC_FILE, Project, find_root, parse_scalar
from .state import State

TEMPLATES = Path(__file__).parent / "templates"
ICON = {"draft": "○", "ready": "◔", "approved": "◑", "failing": "✗", "verified": "●", "stale": "◌", "tampered": "!",
        "blocked": "■", "archived": "·"}


def load() -> tuple[Project, State]:
    root = find_root()
    if not root:
        raise SystemExit(f"arx: no {SPEC_FILE} here or in a parent directory. Run `arx init`.")
    p = Project(root)
    return p, State(p)


def require_human(action: str) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit(f"arx: `{action}` is researcher-only and needs an interactive terminal (or use `arx ui`).")


# ---------------- commands ----------------
def cmd_status(a):
    p, s = load()
    rows = []
    for sid in p.topo_order():
        st = s.status(sid)
        rows.append({"stage": sid, "title": p.stage(sid).get("title", ""), **st})
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"pipeline: {p.data.get('name', p.root.name)}\n")
    for r in rows:
        print(f"  {ICON[r['status']]} {r['stage']:<16} {r['status'].upper():<9} {r['title']}")
        for reason in r["reasons"][: (None if a.verbose else 4)]:
            print(f"      - {reason}")
        if not a.verbose and len(r["reasons"]) > 4:
            print(f"      ... {len(r['reasons']) - 4} more (arx check {r['stage']})")
    return 0


def cmd_check(a):
    p, s = load()
    sids = [a.stage] if a.stage else p.topo_order()
    report = {sid: {"status": s.status(sid)["status"], "issues": p.issues(sid),
                    "reasons": s.status(sid)["reasons"]} for sid in sids}
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        for sid, r in report.items():
            print(f"{ICON[r['status']]} {sid} [{r['status'].upper()}]")
            for i in r["reasons"]:
                print(f"    - {i}")
    if a.ci:
        bad = 0
        for sid, r in report.items():
            if r["status"] == "tampered":
                print(f"arx: FAIL {sid}: verified code was modified without unlocking", file=sys.stderr)
                bad = 1
            elif r["status"] in ("draft", "ready") and p.module_path(sid).exists():
                print(f"arx: warning: {sid} has code but its spec is not approved", file=sys.stderr)
        return bad
    return 0


def cmd_next(a):
    p, s = load()
    for sid in p.topo_order():
        st = s.status(sid)
        k = st["status"]
        if k == "verified":
            continue
        print(f"NEXT: stage '{sid}' is {k.upper()}")
        for r in st["reasons"]:
            print(f"  - {r}")
        if k == "draft":
            print("\nAGENT: stop. Ask the researcher about each open item above (AskUserQuestion), record answers "
                  f"with `arx answer {sid} <key> \"...\"`, then ask them to approve. Do not guess.")
        elif k == "ready":
            print(f"\nWaiting on the researcher: review in `arx ui` or run `arx approve {sid}` in a terminal.")
        elif k == "approved":
            if not p.module_path(sid).exists():
                print(f"\nAGENT: run `arx scaffold {sid}`, implement {p.stage_files(sid)[0]}, then `arx verify {sid}`.")
            else:
                print(f"\nAGENT: implement {', '.join(p.stage_files(sid))} per the spec, then `arx verify {sid}`.")
        elif k == "failing":
            print(f"\nAGENT: fix the implementation (not the checks), then `arx verify {sid}`.")
        elif k == "stale":
            print(f"\nRe-run `arx verify {sid}`. If it fails, the researcher decides whether to `arx unlock {sid}`.")
        elif k == "blocked":
            print("\nAGENT: stop. Report the failing checks, errors and what you tried to the researcher. Do not "
                  "propose new approaches. They decide: revise the box, insert a box, try an alternative, or "
                  f"`arx retry {sid}`.")
        elif k == "tampered":
            print(f"\nResearcher: review the diff of {', '.join(p.stage_files(sid))}; restore it or `arx unlock {sid}`.")
        return 0
    print("All stages verified. 🎉")
    return 0


def _find_decision(p: Project, sid: str, key: str) -> tuple[list, dict | None]:
    stage = p.stage(sid)
    decs = stage.setdefault("decisions", [])
    for d in decs:
        if d.get("key") == key:
            return decs, d
    if key.isdigit() and int(key) < len(decs):
        return decs, decs[int(key)]
    for d in p.decisions(sid):  # bank question not yet materialized
        if d.get("key") == key:
            d = {k: v for k, v in d.items()}
            decs.append(d)
            return decs, d
    return decs, None


def cmd_ask(a):
    p, s = load()
    stage = p.stage(a.stage)
    decs = stage.setdefault("decisions", [])
    d = {"question": a.question, "answer": None, "asked_by": "agent"}
    if a.key:
        if any(x.get("key") == a.key for x in decs):
            raise SystemExit(f"arx: {a.stage} already has a decision '{a.key}'")
        d = {"key": a.key, **d}
    if a.option:
        human = sys.stdin.isatty() and sys.stdout.isatty()
        if not human and not p.policy.get("agent_options"):
            raise SystemExit("arx: policy forbids the agent from suggesting answers (agent_options: false). Ask a "
                             "neutral question without --option; the researcher supplies the ideas.")
        d["options"] = a.option
    if a.why:
        d["why"] = a.why
    decs.append(d)
    p.save()
    print(f"Recorded open question on '{a.stage}'. The stage is blocked until the researcher answers it.")
    return 0


def cmd_answer(a):
    p, s = load()
    decs, d = _find_decision(p, a.stage, a.key)
    if d is None:
        raise SystemExit(f"arx: no decision '{a.key}' on {a.stage}. Use `arx ask` first.")
    d["answer"] = parse_scalar(a.value)
    d["answered_by"] = "researcher" if (sys.stdin.isatty() and sys.stdout.isatty()) else "agent (relayed from chat)"
    p.save()
    print(f"Recorded {a.stage}.{a.key} = {d['answer']!r} ({d['answered_by']}). Approval is still required.")
    return 0


def cmd_approve(a):
    require_human("approve")
    p, s = load()
    sid = a.stage
    issues = p.issues(sid)
    if issues:
        print(f"Cannot approve '{sid}', still unspecified:")
        for i in issues:
            print(f"  - {i}")
        return 1
    print(yaml.safe_dump(p.stage(sid), sort_keys=False, allow_unicode=True))
    relayed = [d for d in p.decisions(sid) if str(d.get("answered_by", "")).startswith("agent")]
    if relayed:
        print("NOTE: these answers were recorded by the agent from chat, check them:")
        for d in relayed:
            print(f"  - {d.get('key')}: {d.get('answer')!r}")
    if input(f"Type '{sid}' to approve this spec: ").strip() != sid:
        print("Not approved.")
        return 1
    for d in p.stage(sid).get("decisions") or []:
        if str(d.get("answered_by", "")).startswith("agent"):
            d["answered_by"] = "agent (relayed, confirmed by researcher)"
    p.save()
    s.approve(sid)
    print(f"Approved '{sid}'. The agent may now implement it.")
    return 0


def cmd_unlock(a):
    require_human("unlock")
    p, s = load()
    if a.stage not in s.locks:
        print(f"'{a.stage}' is not locked.")
        return 0
    if input(f"Unlock verified stage '{a.stage}' so it can be edited again? Type '{a.stage}': ").strip() != a.stage:
        return 1
    s.unlock(a.stage)
    print(f"Unlocked '{a.stage}'. Downstream stages will show as STALE until re-verified.")
    return 0


def cmd_retry(a):
    require_human("retry")
    p, s = load()
    s.reset_attempts(a.stage)
    print(f"Granted '{a.stage}' {p.policy['max_attempts']} more attempts.")
    return 0


def cmd_scaffold(a):
    from .scaffold import scaffold
    p, s = load()
    ok, reasons = s.can_edit(a.stage)
    if not ok:
        print("arx: cannot scaffold:\n  - " + "\n  - ".join(reasons))
        return 1
    made = scaffold(p, a.stage, force=a.force)
    print("created: " + ", ".join(made) if made else f"{p.module_path(a.stage)} exists (use --force to regenerate)")
    return 0


def cmd_run(a):
    from .runner import StageError, real_inputs, call_stage, summarize
    p, s = load()
    sids = [a.stage] if a.stage else p.topo_order()
    for sid in sids:
        st = s.status(sid)["status"]
        if st in ("draft", "ready"):
            print(f"arx: '{sid}' is {st}; not runnable until approved.")
            return 1
        try:
            out = call_stage(p, sid, real_inputs(p, s, sid, require_verified=not a.unverified), p.params(sid))
        except StageError as e:
            print(f"arx: {e} (use --unverified to run on unverified upstream for exploration)")
            return 1
        print(f"[{sid}]")
        for k, v in (out or {}).items():
            print(f"  {k}: {summarize(v)}")
    return 0


def cmd_verify(a):
    from .runner import StageError, verify
    p, s = load()
    try:
        res = verify(p, s, a.stage)
    except StageError as e:
        print(f"arx: {e}")
        return 1
    for c in res["checks"]:
        print(f"  {'PASS' if c['passed'] else 'FAIL'}  [{c['kind']}] {c['name']}  ({c['seconds']}s)")
        if not c["passed"] or a.verbose:
            for line in str(c["detail"]).splitlines()[:25]:
                print(f"        {line}")
    if res["passed"]:
        print(f"\n'{a.stage}' VERIFIED and LOCKED. Its files can no longer be edited by the agent.")
        return 0
    print(f"\n'{a.stage}' FAILING. Fix the implementation, not the checks.")
    return 1


def cmd_ui(a):
    from .server import serve
    p, _ = load()
    serve(p.root, a.port, open_browser=not a.no_browser)
    return 0


def cmd_hook(a):
    return hook.main(a.event)


# ---------------- init ----------------
def _write_launcher(target: Path) -> None:
    """Portable launcher: no machine-specific paths, safe to commit."""
    launcher = target / "arx"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "# arx launcher (generated by `arx init`). Uses the installed `arx` command\n"
        "# (pip install -e <path to agentic-research-tool>); set ARX_PYTHON to pick a specific interpreter.\n"
        'if [ -z "$ARX_PYTHON" ] && command -v arx >/dev/null 2>&1; then exec arx "$@"; fi\n'
        'exec "${ARX_PYTHON:-python3}" -m arx "$@"\n'
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _merge_settings(path: Path) -> None:
    new = json.loads((TEMPLATES / "claude" / "settings.json").read_text())
    cur = json.loads(path.read_text()) if path.exists() else {}
    hooks = cur.setdefault("hooks", {})
    for event, entries in new["hooks"].items():
        existing = hooks.setdefault(event, [])
        if not any("arx hook" in json.dumps(e) for e in existing):
            existing.extend(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=2) + "\n")


def cmd_init(a):
    target = Path(a.dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    spec = target / SPEC_FILE
    if not spec.exists():
        src = TEMPLATES / ("pipeline.example.yaml" if a.example else "pipeline.blank.yaml")
        shutil.copy(src, spec)
        print(f"  wrote {SPEC_FILE}")
    _write_launcher(target)
    _merge_settings(target / ".claude" / "settings.json")
    cmds = target / ".claude" / "commands"
    cmds.mkdir(parents=True, exist_ok=True)
    for f in (TEMPLATES / "claude" / "commands").glob("*.md"):
        shutil.copy(f, cmds / f.name)
    shutil.copy(TEMPLATES / "AGENTS.md", target / "AGENTS.md")
    cm = target / "CLAUDE.md"
    if "@AGENTS.md" not in (cm.read_text() if cm.exists() else ""):
        with cm.open("a") as fh:
            fh.write("\n@AGENTS.md\n")
    gh = target / ".githooks"
    gh.mkdir(exist_ok=True)
    shutil.copy(TEMPLATES / "githooks" / "pre-commit", gh / "pre-commit")
    (gh / "pre-commit").chmod(0o755)
    gi = target / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    for entry in [".arx/cache/", "__pycache__/"]:
        if entry not in lines:
            lines.append(entry)
    gi.write_text("\n".join(lines) + "\n")
    (target / ".arx").mkdir(exist_ok=True)
    print(f"arx initialized in {target}")
    print("  ./arx ui          fill in the boxes, approve stages")
    print("  ./arx status      see what's blocked")
    if (target / ".git").exists():
        print("  git config core.hooksPath .githooks   (enables the pre-commit tamper check)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="arx", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    x = sub.add_parser("init", help="set up arx (hooks, commands, AGENTS.md) in a project")
    x.add_argument("dir", nargs="?", default=".")
    x.add_argument("--example", action="store_true", help="start from the text-clustering example spec")
    x.set_defaults(fn=cmd_init)

    x = sub.add_parser("status", help="status of every stage")
    x.add_argument("--json", action="store_true")
    x.add_argument("-v", "--verbose", action="store_true")
    x.set_defaults(fn=cmd_status)

    x = sub.add_parser("check", help="list what's unspecified (--ci: fail on tampering)")
    x.add_argument("stage", nargs="?")
    x.add_argument("--json", action="store_true")
    x.add_argument("--ci", action="store_true")
    x.set_defaults(fn=cmd_check)

    sub.add_parser("next", help="what should happen next").set_defaults(fn=cmd_next)

    x = sub.add_parser("ask", help="record an open question on a stage (blocks it until answered)")
    x.add_argument("stage")
    x.add_argument("question")
    x.add_argument("--key", help="param name the answer becomes")
    x.add_argument("--option", action="append", help="a suggested option (repeatable)")
    x.add_argument("--why", help="why this matters")
    x.set_defaults(fn=cmd_ask)

    x = sub.add_parser("answer", help="record the researcher's answer to a decision")
    x.add_argument("stage")
    x.add_argument("key")
    x.add_argument("value")
    x.set_defaults(fn=cmd_answer)

    for name, fn, h in [("approve", cmd_approve, "researcher approves a complete stage spec"),
                        ("unlock", cmd_unlock, "researcher unlocks a verified stage"),
                        ("retry", cmd_retry, "researcher grants a blocked stage more attempts")]:
        x = sub.add_parser(name, help=h)
        x.add_argument("stage")
        x.set_defaults(fn=fn)

    x = sub.add_parser("scaffold", help="generate the stage module from its approved spec")
    x.add_argument("stage")
    x.add_argument("--force", action="store_true")
    x.set_defaults(fn=cmd_scaffold)

    x = sub.add_parser("run", help="run stage(s) on real data and summarize outputs")
    x.add_argument("stage", nargs="?")
    x.add_argument("--unverified", action="store_true")
    x.set_defaults(fn=cmd_run)

    x = sub.add_parser("verify", help="run the stage's tests + invariants; lock it if all pass")
    x.add_argument("stage")
    x.add_argument("-v", "--verbose", action="store_true")
    x.set_defaults(fn=cmd_verify)

    x = sub.add_parser("ui", help="open the boxes-and-arrows editor")
    x.add_argument("--port", type=int, default=8765)
    x.add_argument("--no-browser", action="store_true")
    x.set_defaults(fn=cmd_ui)

    x = sub.add_parser("hook", help="(internal) Claude Code hook entrypoint")
    x.add_argument("event", choices=["pre-tool-use", "session-start", "prompt"])
    x.set_defaults(fn=cmd_hook)

    a = ap.parse_args(argv)
    if not a.cmd:  # bare `arx`: where am I, what's next
        if not find_root():
            ap.print_help()
            return 0
        cmd_status(argparse.Namespace(json=False, verbose=False))
        print()
        cmd_next(a)
        print("\nOpen the board: arx ui")
        return 0
    return a.fn(a) or 0
