"""Typed contracts for box inputs/outputs: what flows along each arrow, enforced on every run.

A port is   {name, type, description, schema}   where `type` is one of
  dataframe | series | ndarray | list[<item>] | tuple | dict | str | int | float | bool | any
and `schema` pins it down further:

  dataframe: columns: [{name, dtype, nullable, unique}], rows, min_rows, max_rows, exact_columns
  series:    dtype, rows, nullable, unique
  ndarray:   dtype, shape: [n, 384] (ints, "*" = any, or a symbol), finite, min, max
  list:      item (or list[str]), length, min_len, max_len, unique, nonempty_items
  dict:      keys: [...]
  int/float: min, max

Symbols (n, d, ...) are bound across ALL ports of one box run: if `docs` has rows: n and
`embeddings` has shape [n, "*"], the row counts must agree.
"""
from __future__ import annotations

import math
import re
from typing import Any

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

SYMBOL = re.compile(r"^[A-Za-z_]\w*$")
PORT_TYPES = ["dataframe", "series", "ndarray", "list", "tuple", "dict", "str", "int", "float", "bool", "any"]
DTYPES = ["any", "int", "float", "number", "bool", "str", "datetime", "category", "object",
          "float32", "float64", "int32", "int64"]
PY_ITEM = {"str": str, "int": int, "float": (int, float), "bool": bool, "dict": dict, "list": list}


def split_type(t: Any) -> tuple[str, str]:
    t = str(t or "").strip()
    m = re.match(r"^(\w+)\s*(?:\[(.*)\])?$", t)
    if not m:
        return t.lower(), ""
    base = m.group(1).lower()
    return {"df": "dataframe", "array": "ndarray", "np.ndarray": "ndarray"}.get(base, base), (m.group(2) or "").strip()


def parse_shape(shape: Any) -> list | None:
    if shape is None or shape == "":
        return None
    if isinstance(shape, str):
        shape = [s.strip().strip("'\"") for s in shape.strip("[]() ").split(",") if s.strip()]
    out = []
    for d in shape:
        if isinstance(d, str) and d.lstrip("-").isdigit():
            d = int(d)
        out.append(d)
    return out


class Dims:
    """Binds symbolic sizes across the ports of one run."""

    def __init__(self):
        self.vals: dict[str, int] = {}
        self.src: dict[str, str] = {}

    def bind(self, sym: Any, val: int, where: str, probs: list[str]) -> None:
        if sym is None or sym in ("", "*", "?", "any"):
            return
        if isinstance(sym, int) or (isinstance(sym, str) and sym.isdigit()):
            if int(sym) != val:
                probs.append(f"{where} is {val}, contract says {sym}")
            return
        sym = str(sym)
        if not SYMBOL.match(sym):
            return
        if sym in self.vals and self.vals[sym] != val:
            probs.append(f"{where} is {val}, but {sym} = {self.vals[sym]} from {self.src[sym]}")
        else:
            self.vals.setdefault(sym, val)
            self.src.setdefault(sym, where)


def _dtype_ok(dtype: str, series_or_arr) -> bool:
    d = str(dtype or "any").lower()
    if d in ("", "any"):
        return True
    if pd is not None and isinstance(series_or_arr, pd.Series):
        api = pd.api.types
        s = series_or_arr
        return {
            "int": api.is_integer_dtype, "float": api.is_float_dtype, "number": api.is_numeric_dtype,
            "bool": api.is_bool_dtype, "datetime": api.is_datetime64_any_dtype,
            "category": lambda x: isinstance(x.dtype, pd.CategoricalDtype),
            "str": lambda x: api.is_string_dtype(x) or (api.is_object_dtype(x) and x.dropna().map(type).eq(str).all()),
            "object": api.is_object_dtype,
        }.get(d, lambda x: str(x.dtype) == d)(s)
    kind = series_or_arr.dtype.kind
    fam = {"int": "iu", "float": "f", "number": "iuf", "bool": "b", "str": "USO", "object": "O", "datetime": "M"}
    if d in fam:
        return kind in fam[d]
    try:
        return np.dtype(d) == series_or_arr.dtype
    except TypeError:
        return True


def check_port(name: str, value: Any, typ: Any, schema: dict | None, dims: Dims, role: str = "output") -> list[str]:
    base, inner = split_type(typ)
    sc = schema or {}
    w = f"{role} '{name}'"
    probs: list[str] = []
    if base in ("", "any"):
        return probs

    if base == "dataframe":
        if pd is None or not isinstance(value, pd.DataFrame):
            return [f"{w} should be a pandas DataFrame, got {type(value).__name__}"]
        cols = sc.get("columns") or []
        for c in cols:
            cn = c.get("name")
            if cn not in value.columns:
                probs.append(f"{w} is missing column '{cn}'")
                continue
            s = value[cn]
            if not _dtype_ok(c.get("dtype"), s):
                probs.append(f"{w} column '{cn}' should be {c.get('dtype')}, got {s.dtype}")
            if c.get("nullable") is False and s.isna().any():
                probs.append(f"{w} column '{cn}' has {int(s.isna().sum())} null(s), contract says not nullable")
            if c.get("unique") and s.duplicated().any():
                probs.append(f"{w} column '{cn}' has {int(s.duplicated().sum())} duplicate(s), contract says unique")
        if sc.get("exact_columns"):
            extra = [c for c in value.columns if c not in [x.get("name") for x in cols]]
            if extra:
                probs.append(f"{w} has undeclared columns {extra}")
        dims.bind(sc.get("rows"), len(value), f"{w} rows", probs)
        if sc.get("min_rows") is not None and len(value) < int(sc["min_rows"]):
            probs.append(f"{w} has {len(value)} rows, contract says at least {sc['min_rows']}")
        if sc.get("max_rows") is not None and len(value) > int(sc["max_rows"]):
            probs.append(f"{w} has {len(value)} rows, contract says at most {sc['max_rows']}")
        return probs

    if base == "series":
        if pd is None or not isinstance(value, pd.Series):
            return [f"{w} should be a pandas Series, got {type(value).__name__}"]
        if not _dtype_ok(sc.get("dtype"), value):
            probs.append(f"{w} should have dtype {sc.get('dtype')}, got {value.dtype}")
        if sc.get("nullable") is False and value.isna().any():
            probs.append(f"{w} has nulls, contract says not nullable")
        if sc.get("unique") and value.duplicated().any():
            probs.append(f"{w} has duplicates, contract says unique")
        dims.bind(sc.get("rows"), len(value), f"{w} length", probs)
        return probs

    if base == "ndarray":
        if np is None or not isinstance(value, np.ndarray):
            return [f"{w} should be a numpy ndarray, got {type(value).__name__}"]
        if sc.get("dtype") and not _dtype_ok(sc["dtype"], value):
            probs.append(f"{w} should have dtype {sc['dtype']}, got {value.dtype}")
        shape = parse_shape(sc.get("shape"))
        if shape is not None:
            if len(shape) != value.ndim:
                probs.append(f"{w} should be {len(shape)}-D {shape}, got shape {value.shape}")
            else:
                for i, (want, got) in enumerate(zip(shape, value.shape)):
                    dims.bind(want, int(got), f"{w} dim {i}", probs)
        if value.dtype.kind in "iuf" and value.size:
            if sc.get("finite") and not np.isfinite(value).all():
                probs.append(f"{w} has {int((~np.isfinite(value)).sum())} non-finite value(s)")
            if sc.get("min") is not None and value.min() < float(sc["min"]):
                probs.append(f"{w} min is {value.min():.4g}, contract says >= {sc['min']}")
            if sc.get("max") is not None and value.max() > float(sc["max"]):
                probs.append(f"{w} max is {value.max():.4g}, contract says <= {sc['max']}")
        return probs

    if base in ("list", "tuple"):
        if not isinstance(value, (list, tuple) if base == "list" else tuple):
            return [f"{w} should be a {base}, got {type(value).__name__}"]
        item = (sc.get("item") or inner or "").lower()
        py = PY_ITEM.get(item)
        if py:
            bad = [i for i, x in enumerate(value) if not isinstance(x, py)]
            if bad:
                probs.append(f"{w} items should be {item}; item {bad[0]} is {type(value[bad[0]]).__name__}"
                             + (f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""))
        dims.bind(sc.get("length"), len(value), f"{w} length", probs)
        if sc.get("min_len") is not None and len(value) < int(sc["min_len"]):
            probs.append(f"{w} has {len(value)} items, contract says at least {sc['min_len']}")
        if sc.get("max_len") is not None and len(value) > int(sc["max_len"]):
            probs.append(f"{w} has {len(value)} items, contract says at most {sc['max_len']}")
        if sc.get("unique"):
            try:
                if len(set(value)) != len(value):
                    probs.append(f"{w} has duplicate items, contract says unique")
            except TypeError:
                pass
        if sc.get("nonempty_items") and any((x is None or (hasattr(x, "__len__") and len(x) == 0)) for x in value):
            probs.append(f"{w} has empty items")
        return probs

    if base == "dict":
        if not isinstance(value, dict):
            return [f"{w} should be a dict, got {type(value).__name__}"]
        keys = sc.get("keys") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        missing = [k for k in keys if k not in value]
        if missing:
            probs.append(f"{w} is missing key(s) {missing}")
        return probs

    if base in ("str", "int", "float", "bool"):
        py = {"str": str, "int": int, "float": (int, float), "bool": bool}[base]
        if not isinstance(value, py) or (base in ("int", "float") and isinstance(value, bool)):
            return [f"{w} should be {base}, got {type(value).__name__}"]
        if base in ("int", "float"):
            if sc.get("min") is not None and value < float(sc["min"]):
                probs.append(f"{w} is {value}, contract says >= {sc['min']}")
            if sc.get("max") is not None and value > float(sc["max"]):
                probs.append(f"{w} is {value}, contract says <= {sc['max']}")
        return probs
    return probs  # unknown/custom type: described, not machine-checked


def check_io(stage: dict, inputs: dict | None, out: Any) -> list[str]:
    """Check one run of a box: declared inputs, then declared outputs, sharing symbolic dims."""
    dims = Dims()
    probs: list[str] = []
    for i in stage.get("inputs") or []:
        if inputs is not None and i.get("name") in inputs:
            probs += check_port(i["name"], inputs[i["name"]], i.get("type"), i.get("schema"), dims, "input")
    if out is None:
        return probs
    if not isinstance(out, dict):
        return probs + [f"run() must return a dict of outputs, got {type(out).__name__}"]
    for o in stage.get("outputs") or []:
        if o.get("name") not in out:
            probs.append(f"missing declared output '{o.get('name')}'")
            continue
        probs += check_port(o["name"], out[o["name"]], o.get("type"), o.get("schema"), dims, "output")
    undeclared = [k for k in out if k not in [o.get("name") for o in stage.get("outputs") or []]]
    if undeclared:
        probs.append(f"run() returned undeclared output(s) {undeclared}")
    return probs


def coerce(value: Any, typ: Any, schema: dict | None) -> Any:
    """Turn JSON-ish test / try-it input into the declared type (records -> DataFrame, lists -> ndarray)."""
    base, _ = split_type(typ)
    sc = schema or {}
    if base == "dataframe" and pd is not None and isinstance(value, (list, dict)):
        df = pd.DataFrame(value)
        for c in sc.get("columns") or []:
            if c.get("name") in df.columns and c.get("dtype") == "str":
                df[c["name"]] = df[c["name"]].astype(object)
        return df
    if base == "series" and pd is not None and isinstance(value, list):
        return pd.Series(value)
    if base == "ndarray" and np is not None and isinstance(value, (list, tuple)):
        dt = sc.get("dtype")
        try:
            return np.asarray(value, dtype=None if not dt or dt in ("any", "number", "int", "float", "bool", "str")
                              else dt)
        except TypeError:
            return np.asarray(value)
    if base == "tuple" and isinstance(value, list):
        return tuple(value)
    return value


def coerce_inputs(stage: dict, raw: dict) -> dict:
    ports = {i.get("name"): i for i in stage.get("inputs") or []}
    return {k: coerce(v, ports[k].get("type"), ports[k].get("schema")) if k in ports else v
            for k, v in (raw or {}).items()}


def spec_issues(stage: dict) -> list[str]:
    """Strict contracts: an output's type alone isn't enough, pin down its structure."""
    out = []
    for o in stage.get("outputs") or []:
        base, inner = split_type(o.get("type"))
        sc = o.get("schema") or {}
        n = o.get("name", "?")
        if base == "dataframe" and not sc.get("columns"):
            out.append(f"output '{n}' is a dataframe: declare its columns (name, dtype)")
        elif base == "ndarray" and (not sc.get("dtype") or parse_shape(sc.get("shape")) is None):
            out.append(f"output '{n}' is an ndarray: declare its dtype and shape")
        elif base in ("list", "tuple") and not (inner or sc.get("item")):
            out.append(f"output '{n}' is a list: declare its item type")
        elif base == "series" and not sc.get("dtype"):
            out.append(f"output '{n}' is a series: declare its dtype")
    return out


# ---------------- previews for the UI ----------------
def _safe(x: Any, depth: int = 0) -> Any:
    if x is None or isinstance(x, (bool, int, str)):
        return x if not isinstance(x, str) else x[:300]
    if isinstance(x, float):
        return x if math.isfinite(x) else str(x)
    if np is not None and isinstance(x, np.generic):
        return _safe(x.item(), depth)
    if isinstance(x, (list, tuple)) and depth < 2:
        return [_safe(v, depth + 1) for v in list(x)[:12]]
    if isinstance(x, dict) and depth < 2:
        return {str(k): _safe(v, depth + 1) for k, v in list(x.items())[:12]}
    return repr(x)[:300]


def preview(value: Any) -> dict:
    if pd is not None and isinstance(value, pd.DataFrame):
        head = value.head(25)
        return {"kind": "table", "shape": list(value.shape), "columns": [str(c) for c in value.columns],
                "dtypes": {str(c): str(t) for c, t in value.dtypes.items()},
                "rows": [[_safe(v) for v in row] for row in head.itertuples(index=False)]}
    if pd is not None and isinstance(value, pd.Series):
        return {"kind": "list", "type": f"Series[{value.dtype}]", "len": len(value), "head": [_safe(v) for v in value.head(25)]}
    if np is not None and isinstance(value, np.ndarray):
        p = {"kind": "array", "shape": list(value.shape), "dtype": str(value.dtype)}
        if value.dtype.kind in "iufb" and value.size:
            v = value.astype(float)
            p["stats"] = {"min": _safe(float(np.nanmin(v))), "max": _safe(float(np.nanmax(v))),
                          "mean": _safe(round(float(np.nanmean(v)), 5)), "std": _safe(round(float(np.nanstd(v)), 5))}
            if value.ndim == 1 and value.dtype.kind in "iub" and len(np.unique(value)) <= 30:
                u, c = np.unique(value, return_counts=True)
                p["counts"] = {str(k): int(n) for k, n in zip(u, c)}
        sl = value[tuple(slice(0, 8) for _ in range(min(value.ndim, 2)))]
        p["head"] = _safe(np.round(sl, 4).tolist() if value.dtype.kind == "f" else sl.tolist())
        return p
    if isinstance(value, (list, tuple)):
        return {"kind": "list", "type": type(value).__name__, "len": len(value), "head": [_safe(v) for v in list(value)[:25]]}
    if isinstance(value, dict):
        return {"kind": "dict", "keys": [str(k) for k in value], "items": _safe(value)}
    return {"kind": "value", "repr": repr(value)[:2000], "type": type(value).__name__}


def preview_all(d: Any) -> dict:
    if not isinstance(d, dict):
        return {"(return value)": preview(d)}
    return {str(k): preview(v) for k, v in d.items()}
