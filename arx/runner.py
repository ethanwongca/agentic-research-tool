"""Running stages and verifying them against the researcher's checks.

A stage module exposes:   def run(inputs: dict, params: dict[, ctx]) -> dict
`params` are the researcher's answers to the stage's decisions (plus `params:`).
`ctx` (optional third arg) gives `ctx.artifact_dir` for plots/reports the UI shows.
"""
from __future__ import annotations

import importlib.util
import inspect
import math
import os
import pickle
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .contracts import check_io, coerce_inputs, preview_all
from .spec import Project, parse_scalar
from .state import State, now

TYPE_MAP: dict[str, Any] = {"list": list, "dict": dict, "str": str, "int": int, "float": (int, float),
                            "bool": bool, "tuple": tuple}
try:
    import numpy as np  # noqa
    TYPE_MAP.update({"ndarray": np.ndarray, "array": np.ndarray})
except ImportError:
    np = None
try:
    import pandas as pd  # noqa
    TYPE_MAP.update({"dataframe": pd.DataFrame, "series": pd.Series})
except ImportError:
    pd = None


class StageError(Exception):
    pass


def load_module(path: Path):
    if not path.exists():
        raise StageError(f"{path} does not exist (run `arx scaffold`)")
    name = f"arx_stage_{path.stem}_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
    if not hasattr(mod, "run"):
        raise StageError(f"{path} has no run(inputs, params) function")
    return mod


def call_stage(project: Project, sid: str, inputs: dict, params: dict) -> Any:
    mod = load_module(project.module_path(sid))
    art = project.root / "artifacts" / sid
    art.mkdir(parents=True, exist_ok=True)
    ctx = SimpleNamespace(artifact_dir=art, stage=sid, root=project.root)
    n = len(inspect.signature(mod.run).parameters)
    prev = os.getcwd()
    os.chdir(project.root)  # relative paths in params resolve against the project root
    try:
        return mod.run(inputs, params, ctx) if n >= 3 else mod.run(inputs, params)
    finally:
        os.chdir(prev)


def contract_problems(project: Project, sid: str, out: Any, inputs: dict | None = None) -> list[str]:
    return check_io(project.stage(sid), inputs, out)


def _env(out=None, inp=None, params=None) -> dict:
    return {"out": out, "inp": inp, "params": params, "np": np, "pd": pd, "math": math}


def summarize(v: Any) -> str:
    if np is not None and isinstance(v, np.ndarray):
        return f"ndarray shape={v.shape} dtype={v.dtype}"
    if pd is not None and isinstance(v, pd.DataFrame):
        return f"DataFrame shape={v.shape} cols={list(v.columns)[:8]}"
    if isinstance(v, (list, tuple, dict, set)):
        head = repr(list(v)[:3] if not isinstance(v, dict) else dict(list(v.items())[:3]))
        return f"{type(v).__name__} len={len(v)} head={head[:160]}"
    return repr(v)[:160]


# ---------- real-data execution with cache ----------
def _cache_path(project: Project, sid: str) -> Path:
    return project.arx_dir / "cache" / f"{sid}.pkl"


def real_inputs(project: Project, state: State, sid: str, require_verified: bool = True) -> dict:
    inputs = {}
    for i in project.stage(sid).get("inputs") or []:
        src = str(i.get("from") or "")
        if "." not in src:
            continue
        up, name = src.split(".", 1)
        if require_verified and state.status(up)["status"] != "verified":
            raise StageError(f"upstream '{up}' is not verified")
        inputs[i["name"]] = real_output(project, state, up, require_verified)[name]
    return inputs


def real_output(project: Project, state: State, sid: str, require_verified: bool = True) -> dict:
    token = state.lock_token(sid)
    cp = _cache_path(project, sid)
    if token and cp.exists():
        blob = pickle.loads(cp.read_bytes())
        if blob.get("token") == token:
            return blob["out"]
    out = call_stage(project, sid, real_inputs(project, state, sid, require_verified), project.params(sid))
    if token:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(pickle.dumps({"token": token, "out": out}))
    return out


# ---------- verification ----------
def _check(name: str, kind: str, fn) -> dict:
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as e:  # noqa
        ok, detail = False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=-3)}"
    return {"name": name, "kind": kind, "passed": bool(ok), "detail": detail, "seconds": round(time.time() - t0, 3)}


def verify(project: Project, state: State, sid: str) -> dict:
    st = state.status(sid)["status"]
    if st in ("draft", "ready"):
        raise StageError(f"stage '{sid}' is {st}; the researcher must complete and approve the spec first")
    if st == "blocked":
        raise StageError(f"stage '{sid}' is BLOCKED after {state.attempts.get(sid, 0)} failed attempts. Report the "
                         f"failures to the researcher; they decide whether to revise the box, add a box, try an "
                         f"alternative, or grant more attempts.")
    if st == "archived":
        raise StageError(f"stage '{sid}' is archived")
    if st == "tampered":
        raise StageError(f"stage '{sid}' has locked files that were modified; the researcher must review and "
                         f"`arx unlock {sid}` (or restore the files) before re-verifying")
    appr = state.approvals.get(sid)
    if not appr or appr["spec_hash"] != project.stage_hash(sid):
        raise StageError(f"spec for '{sid}' changed since the researcher approved it; it must be re-approved")
    for d in project.deps(sid):
        if state.status(d)["status"] != "verified":
            raise StageError(f"upstream '{d}' must be verified before '{sid}'")

    stage = project.stage(sid)
    base_params = project.params(sid)
    checks: list[dict] = []

    # 1. hand-written test cases
    for t in stage.get("tests") or []:
        def run_test(t=t):
            inp = t.get("input") or {}
            if isinstance(inp, str):
                inp = parse_scalar(inp)
            inp = coerce_inputs(stage, inp)
            params = {**base_params, **(t.get("params") or {})}
            if t.get("raises"):
                try:
                    call_stage(project, sid, inp, params)
                except Exception as e:  # noqa
                    ok = type(e).__name__ == t["raises"] or t["raises"] in [c.__name__ for c in type(e).__mro__]
                    return ok, f"raised {type(e).__name__}: {e}"
                return False, f"expected {t['raises']} but nothing was raised"
            out = call_stage(project, sid, inp, params)
            probs = contract_problems(project, sid, out, inp)
            if probs:
                return False, "contract violated:\n- " + "\n- ".join(probs)
            ok = eval(str(t["expect"]), _env(out, inp, params))
            return ok, f"expect: {t['expect']}\n" + "\n".join(f"  out.{k}: {summarize(v)}" for k, v in out.items())
        checks.append(_check(t.get("name", "test"), "test", run_test))

    # 2. real-data run: contract + invariants
    real: dict = {}

    def run_real():
        inp = real_inputs(project, state, sid)
        real["inp"] = inp
        real["out"] = call_stage(project, sid, inp, base_params)
        probs = contract_problems(project, sid, real["out"], inp)
        return (not probs), ("contract violated:\n- " + "\n- ".join(probs) if probs else
                             "\n".join(f"out.{k}: {summarize(v)}" for k, v in real["out"].items()))
    checks.append(_check("contract (real data)", "contract", run_real))

    for v in stage.get("invariants") or []:
        def run_inv(v=v):
            if "out" not in real or contract_problems(project, sid, real["out"], real["inp"]):
                return False, "skipped: real-data run failed"
            val = eval(str(v["expr"]), _env(real["out"], real["inp"], base_params))
            return bool(val), f"{v['expr']}  ->  {summarize(val)}"
        checks.append(_check(v.get("name", "invariant"), "invariant", run_inv))

    passed = all(c["passed"] for c in checks)
    result = {"stage": sid, "at": now(), "spec_hash": project.stage_hash(sid), "passed": passed, "checks": checks}
    if "out" in real:
        try:
            result["preview"] = preview_all(real["out"])
        except Exception:  # noqa
            pass
    if not passed:
        state.attempts[sid] = state.attempts.get(sid, 0) + 1
        state.save()
        result["attempt"] = state.attempts[sid]
    state.save_result(sid, result)
    if passed:
        state.lock(sid)
        cp = _cache_path(project, sid)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(pickle.dumps({"token": state.lock_token(sid), "out": real["out"]}))
    return result


# ---------- try it (researcher sandbox; never locks, never counts as an attempt) ----------
def try_stage(project: Project, state: State, sid: str, mode: str = "custom", raw_input: Any = None,
              params_override: dict | None = None) -> dict:
    stage = project.stage(sid)
    if not project.module_path(sid).exists():
        raise StageError(f"'{sid}' has no code yet ({project.stage_files(sid)[0]}); the agent implements it after approval")
    params = {**project.params(sid), **(params_override or {})}
    t0 = time.time()
    try:
        if mode == "real":
            inp = real_inputs(project, state, sid, require_verified=False)
        else:
            if isinstance(raw_input, str):
                raw_input = parse_scalar(raw_input) if raw_input.strip() else {}
            inp = coerce_inputs(stage, raw_input or {})
        out = call_stage(project, sid, inp, params)
    except Exception as e:  # noqa
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=-4),
                "seconds": round(time.time() - t0, 3)}
    probs = check_io(stage, inp, out)
    return {"ok": not probs, "problems": probs, "seconds": round(time.time() - t0, 3),
            "inputs": preview_all(inp), "outputs": preview_all(out)}
