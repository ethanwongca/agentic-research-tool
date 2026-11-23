"""Loading, validating and hashing the pipeline spec (pipeline.yaml)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .banks import BANKS

SPEC_FILE = "pipeline.yaml"
ARX_DIR = ".arx"

DEFAULT_POLICY = {
    "stage_dir": "stages",
    "min_constraints": 1,
    "min_tests": 1,        # hand-written input -> expected-output cases
    "min_invariants": 1,   # properties checked on the real-data output
    "max_attempts": 3,     # failed `arx verify` runs before the stage is BLOCKED and handed back to you
    "agent_options": False,  # may the agent attach suggested answers to its questions?
    "strict_contracts": True,
    "agent": "claude",       # what the Build button runs: claude | codex | a custom command  # outputs must pin down structure (dataframe columns, array dtype+shape, list item type)
}

# Answers that mean "nobody actually decided".
PLACEHOLDER = re.compile(
    r"^\s*(|tbd|todo|tba|\?+|idk|unknown|n/?a|none yet|whatever|any|anything|default|"
    r"you (decide|choose|pick)|(agent|claude|ai|model) (decides?|chooses?|picks?)|"
    r"up to (you|claude|the agent|the ai))\s*[.!]?\s*$",
    re.I,
)


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return bool(PLACEHOLDER.match(str(value)))


def find_root(start: str | os.PathLike | None = None) -> Path | None:
    p = Path(start or os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / SPEC_FILE).exists():
            return d
    return None


def canonical_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def parse_scalar(text: Any) -> Any:
    """Turn a UI/CLI string into a number/bool/list when it clearly is one."""
    if not isinstance(text, str):
        return text
    s = text.strip()
    try:
        v = yaml.safe_load(s)
    except yaml.YAMLError:
        return text
    if isinstance(v, bool) or isinstance(v, (int, float)) or isinstance(v, list):
        return v
    if isinstance(v, dict) and s.startswith("{"):
        return v
    return text


class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_rep)


class Project:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.spec_path = self.root / SPEC_FILE
        self.arx_dir = self.root / ARX_DIR
        self.load()

    # ---------- io ----------
    def load(self) -> None:
        self.data: dict = yaml.safe_load(self.spec_path.read_text()) or {}
        self.data.setdefault("stages", [])
        self.policy = {**DEFAULT_POLICY, **(self.data.get("policy") or {})}

    def save(self) -> None:
        text = yaml.dump(self.data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
        self.spec_path.write_text(text)

    # ---------- stages ----------
    @property
    def stage_list(self) -> list[dict]:
        return self.data["stages"]

    @property
    def all_stages(self) -> dict[str, dict]:
        return {s["id"]: s for s in self.stage_list}

    @property
    def stages(self) -> dict[str, dict]:
        """Active (non-archived) stages: the pipeline."""
        return {s["id"]: s for s in self.stage_list if not s.get("archived")}

    def is_archived(self, sid: str) -> bool:
        return bool(self.all_stages.get(sid, {}).get("archived"))

    def stage(self, sid: str) -> dict:
        try:
            return self.all_stages[sid]
        except KeyError:
            raise SystemExit(f"arx: no stage '{sid}' in {SPEC_FILE} (have: {', '.join(self.stages)})")

    def stage_files(self, sid: str) -> list[str]:
        s = self.stage(sid)
        return list(s.get("files") or [f"{self.policy['stage_dir']}/{sid}.py"])

    def module_path(self, sid: str) -> Path:
        return self.root / self.stage_files(sid)[0]

    def owner_of(self, rel: str) -> str | None:
        rel = rel.replace(os.sep, "/")
        for sid in self.all_stages:
            if rel in self.stage_files(sid):
                return sid
        return None

    def deps(self, sid: str) -> list[str]:
        s = self.stage(sid)
        out = list(s.get("depends_on") or [])
        for i in s.get("inputs") or []:
            src = str(i.get("from") or "")
            if "." in src:
                d = src.split(".", 1)[0]
                if d not in out:
                    out.append(d)
        return out

    def topo_order(self) -> list[str]:
        order, seen, temp = [], set(), set()

        def visit(n):
            if n in seen:
                return
            if n in temp:
                raise ValueError(f"cycle through stage '{n}'")
            temp.add(n)
            for d in self.deps(n):
                if d in self.stages:
                    visit(d)
            temp.discard(n)
            seen.add(n)
            order.append(n)

        for sid in self.stages:
            visit(sid)
        return order

    def decisions(self, sid: str) -> list[dict]:
        """Stage's own decisions plus any required bank questions it hasn't recorded yet."""
        s = self.stage(sid)
        own = list(s.get("decisions") or [])
        keys = {d.get("key") for d in own if d.get("key")}
        for q in BANKS.get(s.get("kind") or "custom", []):
            if q["key"] not in keys:
                own.append({"key": q["key"], "question": q["question"], "answer": None, "asked_by": "arx"})
        return own

    def params(self, sid: str) -> dict:
        p = {d["key"]: parse_scalar(d.get("answer")) for d in self.decisions(sid) if d.get("key")}
        p.update(self.stage(sid).get("params") or {})
        return p

    def stage_hash(self, sid: str) -> str:
        s = {k: v for k, v in self.stage(sid).items() if k not in ("ui", "notes")}
        return canonical_hash(s)

    # ---------- validation ----------
    def issues(self, sid: str) -> list[str]:
        """Everything that is still unspecified. Empty list == ready for human approval."""
        s = self.stage(sid)
        pol = self.policy
        out: list[str] = []
        if is_placeholder(s.get("purpose")):
            out.append("purpose: what is this stage for?")
        if is_placeholder(s.get("approach")):
            out.append("approach: HOW should this be done? (the researcher decides the method, not the agent)")
        for d in self.deps(sid):
            if self.is_archived(d):
                out.append(f"reads from archived stage '{d}'; point the input at an active box")
            elif d not in self.stages:
                out.append(f"depends on unknown stage '{d}'")
        for i in s.get("inputs") or []:
            src = str(i.get("from") or "")
            if not i.get("name"):
                out.append("an input has no name")
            if "." in src:
                st, name = src.split(".", 1)
                if st in self.stages and name not in [o.get("name") for o in self.stages[st].get("outputs") or []]:
                    out.append(f"input '{i.get('name')}' reads {src}, but '{st}' declares no output '{name}'")
        outs = s.get("outputs") or []
        if not outs:
            out.append("outputs: declare at least one output (name + type)")
        for o in outs:
            if not o.get("name") or is_placeholder(o.get("type")):
                out.append(f"output '{o.get('name', '?')}' needs a name and a type")
        if pol.get("strict_contracts"):
            from .contracts import spec_issues
            out += spec_issues(s)
        if len([c for c in s.get("constraints") or [] if not is_placeholder(c)]) < pol["min_constraints"]:
            out.append(f"constraints: need at least {pol['min_constraints']}")
        tests = s.get("tests") or []
        if len(tests) < pol["min_tests"]:
            out.append(f"tests: need at least {pol['min_tests']} hand-written test case(s) (input -> expected)")
        for t in tests:
            if isinstance(t.get("input"), str) or isinstance(t.get("params"), str):
                out.append(f"test '{t.get('name', '?')}' input/params isn't valid JSON")
            if is_placeholder(t.get("expect")) and not t.get("raises"):
                out.append(f"test '{t.get('name', '?')}' has no `expect` expression or `raises`")
        invs = s.get("invariants") or []
        if len(invs) < pol["min_invariants"]:
            out.append(f"invariants: need at least {pol['min_invariants']} check(s) on the real output")
        for v in invs:
            if is_placeholder(v.get("expr")):
                out.append(f"invariant '{v.get('name', '?')}' has no `expr`")
        for d in self.decisions(sid):
            if is_placeholder(d.get("answer")):
                who = d.get("asked_by", "researcher")
                out.append(f"unanswered [{d.get('key') or '-'}] {d.get('question')}  (asked by {who})")
        return out
