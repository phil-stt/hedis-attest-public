"""The measure registry: measures as data (attest/measures/*.yaml).

A measure is active (extraction runs), planned (listed, not built), or registry_only
(no chart lever: the product says which measures it does not help with). A measure
may have indicators (TRC has four); each indicator is its own spec with its own
family, fields, window, and checks. `spec(measure, indicator)` returns the merged
spec the schema, rules, prompts, service, and UI all read from.
"""

import hashlib
from functools import cache
from pathlib import Path
from typing import Dict, Optional

import yaml

MEASURE_DIR = Path(__file__).resolve().parent / "measures"
FAMILIES = ("blood_pressure", "lab_value", "procedure_window", "paired_labs", "event_window")


@cache
def all_measures() -> Dict[str, dict]:
    out = {}
    for p in sorted(MEASURE_DIR.glob("*.yaml")):
        m = yaml.safe_load(p.read_text())
        m.setdefault("indicators", None)
        m.setdefault("exclusions", [])
        out[m["code"]] = m
    return out


def active() -> Dict[str, dict]:
    return {k: v for k, v in all_measures().items() if v["status"] == "active"}


def key(measure: str, indicator: Optional[str] = None) -> str:
    """Display/lookup key: 'CBP' or 'TRC:MRP'."""
    return f"{measure}:{indicator}" if indicator else measure


def split_key(k: str):
    measure, _, indicator = k.partition(":")
    return measure, indicator or None


@cache
def spec(measure: str, indicator: Optional[str] = None) -> dict:
    """The merged spec for a measure or one of its indicators."""
    m = all_measures()[measure]
    if m["indicators"]:
        if not indicator or indicator not in m["indicators"]:
            raise KeyError(f"{measure} needs an indicator, one of {list(m['indicators'])}")
        ind = m["indicators"][indicator]
        merged = {**{k: v for k, v in m.items() if k != "indicators"}, **ind,
                  "measure": measure, "indicator": indicator, "key": key(measure, indicator),
                  "display": f"{measure} · {ind['name']}"}
    else:
        if indicator:
            raise KeyError(f"{measure} has no indicators")
        merged = {**m, "measure": measure, "indicator": None, "key": measure, "display": m["name"]}
    if merged["status"] in ("active", "planned") and merged.get("family") not in FAMILIES:
        raise ValueError(f"{merged['key']}: unknown family {merged.get('family')!r}")
    merged.setdefault("fields", [])
    merged.setdefault("field_schema", {})
    merged.setdefault("field_labels", {})
    merged.setdefault("params", {})
    return merged


def spec_for_gap(gap: dict) -> dict:
    return spec(gap["measure"], gap.get("indicator"))


def extraction_specs() -> Dict[str, dict]:
    """Every active spec that extraction runs for, by key (indicators expanded)."""
    out = {}
    for code, m in active().items():
        if m["indicators"]:
            for ind in m["indicators"]:
                out[key(code, ind)] = spec(code, ind)
        else:
            out[code] = spec(code)
    return out


def prompt_versions() -> Dict[str, str]:
    """{measure: prompt version} for active measures (one prompt per measure, indicators share it)."""
    return {code: m["prompt"] for code, m in active().items()}


def variant_id(router: str | None = None, model: str | None = None, effort: str | None = None) -> str:
    """The extraction variant: model, effort, prompt versions, router, hashed to a short stable id.
    The pipeline registers it; every finding carries it so decisions can be compared by variant."""
    from .config import EFFORT, MODEL  # local import: config reads the environment

    model, effort = model or MODEL, effort or EFFORT
    prompts = prompt_versions()
    base = f"{model}|{effort}|" + ",".join(f"{k}={v}" for k, v in sorted(prompts.items())) + f"|router={router or 'none'}"
    return f"{model.split('-', 1)[1].replace('-', '')}-{effort}-{hashlib.sha1(base.encode()).hexdigest()[:8]}" + (
        "-router" if router else "")
