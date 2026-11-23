"""Local web UI: boxes-and-arrows view + spec forms + gate actions. Binds to 127.0.0.1 only."""
from __future__ import annotations

import copy
import json
import mimetypes
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml

from .banks import BANKS
from .spec import Project, parse_scalar
from .state import State

UI = Path(__file__).parent / "ui" / "index.html"


def code_sig() -> str:
    """Fingerprint of arx's Python code, so the page can tell when this server is out of date."""
    files = sorted(Path(__file__).parent.glob("*.py"))
    return str(sum(int(f.stat().st_mtime) for f in files))


BOOT_SIG = code_sig()
LOCK = threading.Lock()
IMG = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".html", ".txt", ".csv", ".json", ".md"}


def snapshot(root: Path) -> dict:
    p = Project(root)
    s = State(p)
    stages = []
    try:
        order = p.topo_order()
    except ValueError:
        order = list(p.stages)
    order += [sid for sid in p.all_stages if p.is_archived(sid)]
    for sid in order:
        st = s.status(sid)
        art = p.root / "artifacts" / sid
        stages.append({
            "id": sid,
            "spec": p.stage(sid),
            "decisions": p.decisions(sid),
            "deps": p.deps(sid),
            "files": p.stage_files(sid),
            "files_exist": [f for f in p.stage_files(sid) if (p.root / f).exists()],
            "status": st["status"],
            "reasons": st["reasons"],
            "issues": p.issues(sid),
            "result": s.result(sid),
            "approved_at": (s.approvals.get(sid) or {}).get("at"),
            "attempts": s.attempts.get(sid, 0),
            "variants": [x for x, v in p.all_stages.items() if v.get("variant_of") == sid and not v.get("archived")],
            "locked_at": (s.locks.get(sid) or {}).get("at"),
            "artifacts": sorted(str(f.relative_to(p.root)) for f in art.rglob("*")
                                if f.is_file() and f.suffix.lower() in IMG) if art.exists() else [],
        })
    meta = {k: v for k, v in p.data.items() if k != "stages"}
    from .contracts import DTYPES, PORT_TYPES
    from .agent import RUNNER
    meta.pop("notes", None)
    return {"pipeline": meta, "policy": p.policy, "kinds": {k: v for k, v in BANKS.items()}, "stages": stages,
            "port_types": PORT_TYPES, "dtypes": DTYPES, "notes": p.data.get("notes") or [],
            "stale_server": code_sig() != BOOT_SIG,
            "agents": {a: bool(__import__("shutil").which(a)) for a in ("claude", "codex")},
            "agent": {k: v for k, v in RUNNER.status().items() if k != "events"}}


def clean_stage(body: dict) -> dict:
    """Normalize a stage posted from the UI before writing pipeline.yaml."""
    s = {k: v for k, v in body.items() if v not in (None, "", [], {}) or k in
         ("purpose", "approach", "inputs", "outputs", "decisions", "constraints", "tests", "invariants")}
    for port in (s.get("inputs") or []) + (s.get("outputs") or []):
        sc = port.get("schema")
        if isinstance(sc, dict):
            sc = {k: v for k, v in sc.items() if v not in (None, "", [], False) or k == "nullable"}
            for c in sc.get("columns") or []:
                for k in [k for k, v in c.items() if v in (None, "")]:
                    c.pop(k)
            if sc.get("shape") and isinstance(sc["shape"], str):
                from .contracts import parse_shape
                sc["shape"] = parse_shape(sc["shape"])
            for k in ("min_rows", "max_rows", "min_len", "max_len", "min", "max"):
                if isinstance(sc.get(k), str):
                    sc[k] = parse_scalar(sc[k])
            for k in ("rows", "length"):
                if isinstance(sc.get(k), str) and sc[k].isdigit():
                    sc[k] = int(sc[k])
            if sc:
                port["schema"] = sc
            else:
                port.pop("schema", None)
        if port.get("from") == "":
            port.pop("from")
    for d in s.get("decisions") or []:
        if isinstance(d.get("answer"), str):
            d["answer"] = parse_scalar(d["answer"]) if d["answer"].strip() else None
            if d["answer"] is not None and not d.get("answered_by"):
                d["answered_by"] = "researcher"
        for k in [k for k, v in d.items() if v in ("", None, []) and k not in ("answer", "question")]:
            d.pop(k)
    for t in s.get("tests") or []:
        for f in ("input", "params"):
            if isinstance(t.get(f), str):
                raw = t[f]
                try:
                    v = yaml.safe_load(raw) if raw.strip() else {}
                except yaml.YAMLError:
                    v = raw  # half-typed: keep the text, `arx check` flags it
                t[f] = v if v not in (None, "") else {}
        if not t.get("params"):
            t.pop("params", None)
        if not t.get("raises"):
            t.pop("raises", None)
    s["constraints"] = [c for c in s.get("constraints") or [] if str(c).strip()]
    if isinstance(s.get("depends_on"), list):
        s["depends_on"] = [d for d in s["depends_on"] if d]
        if not s["depends_on"]:
            s.pop("depends_on")
    return s


class Handler(BaseHTTPRequestHandler):
    root: Path

    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlparse(origin).netloc == host

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = UI.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/state":
            try:
                self._json(snapshot(self.root))
            except Exception as e:  # noqa
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif path == "/api/agent":
            from .agent import RUNNER
            self._json(RUNNER.status())
        elif path == "/favicon.svg":
            data = (UI.parent / "favicon.svg").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(data)
        elif path.startswith("/files/"):
            rel = unquote(path[len("/files/"):])
            f = (self.root / rel).resolve()
            if not str(f).startswith(str((self.root / "artifacts").resolve())) or not f.is_file():
                return self._json({"error": "not found"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
            self.end_headers()
            self.wfile.write(f.read_bytes())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "cross-origin request refused"}, 403)
        parts = urlparse(self.path).path.strip("/").split("/")
        try:
            with LOCK:
                out = self.route(parts, self._body())
            self._json(out or {"ok": True})
        except SystemExit as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa
            self._json({"error": f"{type(e).__name__}: {e}"}, 400)

    def route(self, parts, body):
        p = Project(self.root)
        s = State(p)
        if parts[:2] == ["api", "pipeline"]:
            for k in ("name", "description"):
                if k in body:
                    p.data[k] = body[k]
            if "policy" in body:
                p.data["policy"] = {**(p.data.get("policy") or {}), **body["policy"]}
            p.save()
            return None
        if parts[:2] == ["api", "stages"]:
            sid = new_stage(p, body.get("title") or "New box", body.get("kind") or "custom")
            if "x" in body and "y" in body:
                p.stage(sid)["ui"] = {"x": round(float(body["x"])), "y": round(float(body["y"]))}
            p.save()
            return {"id": sid}
        if parts[:2] == ["api", "connect"]:
            src, to = str(body.get("from", "")), str(body.get("to", ""))
            if "." not in src:
                raise ValueError("connect needs from=<box>.<output>")
            up, oname = src.split(".", 1)
            if up == to:
                raise ValueError("a box can't feed itself")
            outp = next((o for o in p.stage(up).get("outputs") or [] if o.get("name") == oname), None)
            if outp is None:
                raise ValueError(f"'{up}' has no output '{oname}'")
            tgt = p.stage(to)
            ins = tgt.setdefault("inputs", [])
            if any(i.get("from") == src for i in ins):
                raise ValueError(f"{src} is already connected to '{to}'")
            name, n = oname, 2
            while any(i.get("name") == name for i in ins):
                name, n = f"{oname}_{n}", n + 1
            port = {"name": name, "from": src, "type": outp.get("type", "")}
            if outp.get("schema"):
                port["schema"] = copy.deepcopy(outp["schema"])
            ins.append(port)
            try:
                p.topo_order()
            except ValueError:
                raise ValueError("that arrow would create a cycle")
            p.save()
            return {"name": name}
        if parts[:2] == ["api", "depend"]:
            src, to = str(body.get("from", "")), str(body.get("to", ""))
            if src == to:
                raise ValueError("a box can't depend on itself")
            p.stage(src)
            tgt = p.stage(to)
            deps = tgt.setdefault("depends_on", [])
            if src not in deps:
                deps.append(src)
            try:
                p.topo_order()
            except ValueError:
                raise ValueError("that arrow would create a cycle")
            p.save()
            return None
        if parts[:2] == ["api", "disconnect"]:
            tgt = p.stage(str(body.get("to")))
            if body.get("input"):
                tgt["inputs"] = [i for i in tgt.get("inputs") or [] if i.get("name") != body["input"]]
            if body.get("dep"):
                tgt["depends_on"] = [d for d in tgt.get("depends_on") or [] if d != body["dep"]]
                if not tgt["depends_on"]:
                    tgt.pop("depends_on")
            p.save()
            return None
        if parts[:2] == ["api", "note"]:
            notes = p.data.setdefault("notes", [])
            nid = body.get("id")
            n = next((x for x in notes if x.get("id") == nid), None)
            if n is None:
                nid = f"n{int(__import__('time').time() * 1000) % 10**9}"
                n = {"id": nid, "text": "", "x": 0, "y": 0, "color": "yellow"}
                notes.append(n)
            for k in ("text", "color", "links"):
                if k in body:
                    n[k] = body[k]
            for k in ("x", "y", "w"):
                if k in body:
                    n[k] = round(float(body[k]))
            p.save()
            return {"id": nid}
        if parts[:2] == ["api", "note_delete"]:
            nid = body.get("id")
            p.data["notes"] = [x for x in p.data.get("notes") or [] if x.get("id") != nid]
            for x in p.data["notes"]:
                if nid in (x.get("links") or []):
                    x["links"] = [l for l in x["links"] if l != nid]
            p.save()
            return None
        if parts[:2] == ["api", "agent_stop"]:
            from .agent import RUNNER
            RUNNER.stop()
            return None
        if len(parts) != 3 or parts[0] != "api":
            raise ValueError(f"unknown endpoint /{'/'.join(parts)}")
        action, sid = parts[1], parts[2]
        stage = p.stage(sid)

        if action == "stage":
            if sid in s.locks and body.get("_confirm_locked") is not True:
                raise ValueError("stage is verified/locked; changing its spec marks it STALE")
            body.pop("_confirm_locked", None)
            body["id"] = sid
            for k in ("archived", "variant_of", "ui"):
                if stage.get(k) and k not in body:
                    body[k] = stage[k]
            p.stage_list[index_of(p, sid)] = clean_stage(body)
            p.save()
            return None
        if action == "agent":
            from .agent import RUNNER
            st = s.status(sid)["status"]
            if st not in ("approved", "failing"):
                raise ValueError(f"'{sid}' is {st}; the agent can only build approved or failing boxes")
            bad = [d for d in p.deps(sid) if s.status(d)["status"] != "verified"]
            if bad:
                raise ValueError(f"build the upstream box first: {', '.join(bad)}")
            RUNNER.start(p.root, sid, p.stage_files(sid)[0], body.get("agent") or p.policy.get("agent", "claude"))
            return None
        if action == "move":
            stage["ui"] = {"x": round(float(body.get("x", 0))), "y": round(float(body.get("y", 0)))}
            p.save()  # layout only: excluded from the spec hash, never invalidates approval
            return None
        if action == "try":
            from .runner import StageError, try_stage
            try:
                return try_stage(p, s, sid, body.get("mode", "custom"), body.get("input"), body.get("params") or {})
            except StageError as e:
                raise ValueError(str(e))
        if action == "add_test":
            t = {"name": body.get("name") or "case", "input": body.get("input") or {}, "expect": body.get("expect")}
            if body.get("params"):
                t["params"] = body["params"]
            for f in ("input", "params"):
                if isinstance(t.get(f), str):
                    t[f] = yaml.safe_load(t[f]) if t[f].strip() else {}
            stage.setdefault("tests", []).append(t)
            p.save()
            return None
        if action == "approve":
            issues = p.issues(sid)
            if issues:
                raise ValueError("cannot approve, still unspecified:\n- " + "\n- ".join(issues))
            for d in stage.get("decisions") or []:
                if str(d.get("answered_by", "")).startswith("agent") and "confirmed" not in d["answered_by"]:
                    d["answered_by"] = "agent (relayed, confirmed by researcher)"
            p.save()
            s.approve(sid)
            return None
        if action == "unlock":
            s.unlock(sid)
            return None
        if action == "retry":
            s.reset_attempts(sid)
            return None
        if action == "verify":
            from .runner import StageError, verify
            try:
                return verify(p, s, sid)
            except StageError as e:
                raise ValueError(str(e))
        if action == "archive":
            stage["archived"] = True
            p.save()
            return None
        if action == "restore":
            stage.pop("archived", None)
            p.save()
            return None
        if action == "delete":
            if sid in s.locks:
                raise ValueError("unlock the stage before deleting it")
            readers = [x for x in p.stages if x != sid and sid in p.deps(x)]
            if readers:
                raise ValueError(f"other boxes read from '{sid}': {', '.join(readers)}")
            p.data["stages"] = [x for x in p.stage_list if x["id"] != sid]
            p.save()
            s.approvals.pop(sid, None)
            s.save()
            return None
        if action == "insert_before":
            # new box N sits between this box and its upstream: upstream -> N -> this
            nid = new_stage(p, body.get("title") or f"Before {stage.get('title', sid)}", body.get("kind") or "custom",
                            at=index_of(p, sid))
            n = p.stage(nid)
            ins = stage.get("inputs") or []
            n["inputs"] = copy.deepcopy(ins)
            n["outputs"] = [{"name": i["name"], "type": i.get("type", ""), "description": f"prepared for {sid}"}
                            for i in ins if i.get("name")]
            n["depends_on"] = [d for d in stage.get("depends_on") or []]
            if ins:
                stage["inputs"] = [{**i, "from": f"{nid}.{i['name']}"} for i in ins]
            stage["depends_on"] = [nid]
            p.save()
            return {"id": nid}
        if action == "insert_after":
            nid = new_stage(p, body.get("title") or f"After {stage.get('title', sid)}", body.get("kind") or "custom",
                            at=index_of(p, sid) + 1)
            n = p.stage(nid)
            n["inputs"] = [{"name": o["name"], "from": f"{sid}.{o['name']}", "type": o.get("type", "")}
                           for o in stage.get("outputs") or [] if o.get("name")]
            if not n["inputs"]:
                n["depends_on"] = [sid]
            p.save()
            return {"id": nid}
        if action == "variant":
            # an alternative approach for the same box: same contract and checks, approach left for you
            nid = unique_id(p, f"{sid}_alt")
            v = copy.deepcopy({k: val for k, val in stage.items() if k not in ("files", "archived", "ui")})
            v.update({"id": nid, "title": f"{stage.get('title', sid)} (alternative)", "variant_of": sid,
                      "approach": ""})
            p.stage_list.insert(index_of(p, sid) + 1, v)
            p.save()
            return {"id": nid}
        if action == "promote":
            orig = stage.get("variant_of")
            if not orig:
                raise ValueError(f"'{sid}' is not an alternative of another box")
            for x in p.stage_list:
                if x["id"] in (sid, orig):
                    continue
                for i in x.get("inputs") or []:
                    if str(i.get("from", "")).startswith(orig + "."):
                        i["from"] = sid + i["from"][len(orig):]
                if x.get("depends_on"):
                    x["depends_on"] = [sid if d == orig else d for d in x["depends_on"]]
            p.stage(orig)["archived"] = True
            stage.pop("variant_of", None)
            p.save()
            return None
        raise ValueError(f"unknown action '{action}'")


def index_of(p: Project, sid: str) -> int:
    return [x["id"] for x in p.stage_list].index(sid)


def unique_id(p: Project, base: str) -> str:
    base = re.sub(r"\W+", "_", base.lower()).strip("_") or "box"
    if base[0].isdigit():
        base = "s_" + base
    sid, n = base, 2
    while sid in p.all_stages:
        sid, n = f"{base}{n}", n + 1
    return sid


def new_stage(p: Project, title: str, kind: str = "custom", at: int | None = None) -> str:
    sid = unique_id(p, title)
    st = {"id": sid, "title": title, "kind": kind, "purpose": "", "approach": "", "inputs": [], "outputs": [],
          "decisions": [], "constraints": [], "tests": [], "invariants": []}
    p.stage_list.insert(len(p.stage_list) if at is None else at, st)
    return sid


def serve(root: Path, port: int = 8765, open_browser: bool = True) -> None:
    Handler.root = Path(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"arx ui: {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
