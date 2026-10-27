"""Run the extractor over a labeled split and score it.

    python -m attest.eval                    # dev split (use this for prompt work)
    python -m attest.eval --split heldout    # the eval of record; never tune against it
    python -m attest.eval --split heldout --gate-min-numerator-precision 1.0 --gate-max-wrong-values 0
    python -m attest.eval diff <run_id_a> <run_id_b>   # two runs on one label set, gap by gap
    python -m attest.eval show [<run_id>]              # print a saved run's report (no model call)

Writes evals/runs/<run_id>.json and appends a summary to evals/history.json.

Definitions
- precision / recall per finding type (numerator, exclusion, abstain, none).
- wrong-value count: findings a nurse could accept (numerator or exclusion) that
  are wrong, either because the expected finding is something else or because
  any expected field differs. This is the number that must be zero.
- field accuracy: among findings with the right actionable type, the share whose
  expected fields all match exactly (value and date).
- correct abstains: abstains where the label is abstain (the trap cases).
"""

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from math import comb

from . import data
from .config import EFFORT, EVAL_DIR, MODEL, PRICE_PER_MTOK, PROMPT_VERSIONS
from .extractor import extract
from .schema import FINDING_TYPES

ACTIONABLE = ("numerator", "exclusion")

# The CI gate (.github/workflows/ci.yml passes these as flags; a test keeps them in sync).
# Set from the first clean held-out run, 20261005T032227Z.
GATE_MIN_NUMERATOR_PRECISION = 1.0
GATE_MAX_WRONG_VALUES = 0
GATE_MAX_WRONG_VALUES_PER_STRATUM = 0   # Phase 6: a wrong value in any tranche fails, even if a looser global cap allowed it

# Stratified metrics: a stratum below this n shows a bound, never a claim.
MIN_STRATUM_N = 10
# Row keys a run is stratified over (filled by score_row from the label and the dataset).
STRATA = {"tranche": "Tranche", "lob": "Line of business", "measure": "Measure", "evidence_source": "Evidence source",
          "fragmentation": "Fragmentation", "doc_types": "Document types in chart"}


def gate_passes(metrics: dict) -> bool:
    p = metrics["per_type"]["numerator"]["precision"]
    return p is not None and p >= GATE_MIN_NUMERATOR_PRECISION and metrics["wrong_value_count"] <= GATE_MAX_WRONG_VALUES


def _norm(v):
    return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def lower_bound_95(k: int, n: int):
    """Exact (Clopper-Pearson) two-sided 95% lower bound for a proportion k/n.
    For k == n this is 0.025 ** (1 / n): about 0.59 for 7/7 and 0.81 for 18/18."""
    if not n:
        return None
    if k == 0:
        return 0.0

    def p_at_least_k(p):  # P(X >= k | n, p)
        return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))

    lo, hi = 0.0, 1.0
    for _ in range(60):  # bisection: find p where P(X >= k) = 0.025
        mid = (lo + hi) / 2
        if p_at_least_k(mid) < 0.025:
            lo = mid
        else:
            hi = mid
    return round(hi, 3)


def _strata_of(label: dict) -> dict:
    """Stratum keys for a label: tranche and its line of business, evidence source, fragmentation kinds, and the
    document types in the chart. Multi-valued keys (fragmentation, doc_types) are lists; a row counts in each."""
    tranches = data.tranches()
    t = tranches.get(label.get("tranche_id") or "")
    lob = tranches.get(t["parent_id"]) if t and t.get("parent_id") else t
    gap = data.gaps().get(label["gap_id"]) or {}
    docs = data.documents()
    return {
        "tranche_id": label.get("tranche_id"),
        "tranche": (t or {}).get("name") or label.get("tranche_id") or "unknown",
        "lob": (lob or {}).get("name") or "unknown",
        "evidence_source": label.get("evidence_source") or "unknown",
        "fragmentation": sorted(label.get("fragmentation") or []) or ["none"],
        "doc_types": sorted({docs[d]["doc_type"] for d in gap.get("document_ids", []) if d in docs}) or ["none"],
    }


def score_row(label: dict, result: dict) -> dict:
    exp, pred = label["expected_finding"], result["finding_type"]
    fields = result.get("fields") or {}
    mismatched = [k for k, v in label["expected_fields"].items() if _norm(fields.get(k)) != _norm(v)]
    wrong_value = pred in ACTIONABLE and (exp != pred or bool(mismatched))
    span_hit = None
    expected = label.get("evidence")
    expected = expected if isinstance(expected, list) else ([expected] if expected else [])
    got = result.get("evidence_spans") or ([result["evidence_span"]] if result.get("evidence_span") else [])
    if expected and got:
        # Every labeled passage is covered by some predicted span (overlap on the same document).
        span_hit = all(any(a["doc_id"] == b["doc_id"] and b["start"] < a["end"] and a["start"] < b["end"] for b in got)
                       for a in expected)
    return {
        "gap_id": label["gap_id"], "measure": label["measure"], "indicator": label.get("indicator"),
        "scenario": label["scenario"],
        "category": label["category"],
        "expected": exp, "predicted": pred,
        "expected_reason": label["abstain_reason"], "predicted_reason": result.get("abstain_reason"),
        **_strata_of(label),
        "router_skipped": (result.get("router") or {}).get("decision") == "skip",
        "expected_fields": label["expected_fields"],
        "predicted_fields": {k: fields.get(k) for k in label["expected_fields"]} if label["expected_fields"] else fields,
        "field_mismatches": mismatched if exp == pred and pred in ACTIONABLE else [],
        "wrong_value": wrong_value,
        "span_overlaps_label": span_hit,
        "downgraded_by_rules": result.get("model_finding_type"),
        "failed_checks": [c for c in result.get("rule_checks", []) if not c["passed"]],
        "confidence": result.get("confidence"),
        "rationale": result.get("rationale"),
        "evidence": result.get("evidence"),
        "call": result.get("call"),
    }


def metrics(rows: list) -> dict:
    n = len(rows)
    per_type = {}
    for t in FINDING_TYPES:
        tp = sum(r["expected"] == t and r["predicted"] == t for r in rows)
        pred_n = sum(r["predicted"] == t for r in rows)
        exp_n = sum(r["expected"] == t for r in rows)
        per_type[t] = {"precision": round(tp / pred_n, 3) if pred_n else None,
                       "recall": round(tp / exp_n, 3) if exp_n else None,
                       "predicted": pred_n, "expected": exp_n, "correct": tp}
    actionable_right = [r for r in rows if r["predicted"] in ACTIONABLE and r["expected"] == r["predicted"]]
    fields_ok = sum(not r["field_mismatches"] for r in actionable_right)
    abstains = [r for r in rows if r["predicted"] == "abstain"]
    traps = [r for r in rows if r["category"] == "trap"]
    spans = [r["span_overlaps_label"] for r in actionable_right if r["span_overlaps_label"] is not None]
    return {
        "gaps": n,
        "accuracy": round(sum(r["expected"] == r["predicted"] for r in rows) / n, 3) if n else None,
        "per_type": per_type,
        "wrong_value_count": sum(r["wrong_value"] for r in rows),
        "field_accuracy": round(fields_ok / len(actionable_right), 3) if actionable_right else None,
        "field_checked": len(actionable_right),
        "abstain_rate": round(len(abstains) / n, 3) if n else None,
        "abstains": len(abstains),
        "correct_abstains": sum(r["expected"] == "abstain" for r in abstains),
        "abstain_reason_match": sum(r["expected"] == "abstain" and r["expected_reason"] == r["predicted_reason"]
                                    for r in abstains),
        "traps": len(traps),
        "traps_not_accepted": sum(r["predicted"] not in ACTIONABLE for r in traps),
        "evidence_span_match": round(sum(spans) / len(spans), 3) if spans else None,
        "downgraded_by_rules": sum(bool(r["downgraded_by_rules"]) for r in rows),
        "strata": strata(rows),
        "max_wrong_values_in_a_tranche": max((s["wrong_values"] for s in stratified(rows, "tranche")), default=0),
    }


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "value": round(k / n, 3) if n else None, "lb": lower_bound_95(k, n)}


def stratified(rows: list, key: str) -> list:
    """Per stratum of `key`: n, correct, wrong values, abstains, numerator precision and actionable recall with
    lower bounds, router losses. Below MIN_STRATUM_N the entry is flagged: the bound is shown, the rate is not a claim."""
    groups: dict = {}
    for r in rows:
        vals = r.get(key)
        if vals is None:
            vals = ["unknown"]
        for v in (vals if isinstance(vals, list) else [vals]):
            groups.setdefault(v, []).append(r)
    out = []
    for v, rs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        n = len(rs)
        num_pred = [r for r in rs if r["predicted"] == "numerator"]
        act_exp = [r for r in rs if r["expected"] in ACTIONABLE]
        skipped = [r for r in rs if r.get("router_skipped")]
        out.append({
            "key": v, "n": n, "below_min_n": n < MIN_STRATUM_N,
            "correct": sum(r["expected"] == r["predicted"] for r in rs),
            "accuracy": _rate(sum(r["expected"] == r["predicted"] for r in rs), n),
            "wrong_values": sum(r["wrong_value"] for r in rs),
            "abstains": sum(r["predicted"] == "abstain" for r in rs),
            "abstain_rate": _rate(sum(r["predicted"] == "abstain" for r in rs), n),
            "numerator_precision": _rate(sum(r["expected"] == "numerator" for r in num_pred), len(num_pred)),
            "actionable_recall": _rate(sum(r["predicted"] == r["expected"] for r in act_exp), len(act_exp)),
            # Router variant: gaps the cheap pass skipped, and how many of those held a closable finding.
            "router_skipped": len(skipped),
            "router_lost": sum(r["expected"] in ACTIONABLE for r in skipped),
        })
    return out


def strata(rows: list) -> dict:
    return {k: stratified(rows, k) for k in STRATA}


def usage_totals(rows: list) -> dict:
    tot = Counter()
    served = Counter()
    for r in rows:
        call = r.get("call") or {}
        tot.update(call.get("usage") or {})
        if call.get("model_served"):
            served[call["model_served"]] += 1
    price = PRICE_PER_MTOK.get(MODEL)
    cost = None
    if price:
        cost = round((tot["input_tokens"] * price["input"] + tot["output_tokens"] * price["output"]
                      + tot["cache_read_input_tokens"] * price["cache_read"]
                      + tot["cache_creation_input_tokens"] * price["cache_write"]) / 1e6, 2)
    return {"tokens": dict(tot), "models_served": dict(served), "estimated_cost_usd": cost}


def _routed_extract(router: str):
    """The router variant: a cheap first pass decides whether the full extractor reads the chart.
    A skip is scored as a `none` finding, so the router's recall cost shows in the metrics."""
    from .extractor import route

    def go(gap_id):
        decision = route(gap_id, router)
        if decision["worth_reading"]:
            out = extract(gap_id)
            out["router"] = {"model": router, "decision": "read", "reason": decision["reason"], "error": decision.get("error")}
            out["call"]["router_usage"] = decision["call"].get("usage")
            return out
        return {"finding_type": "none", "abstain_reason": None, "fields": {}, "evidence": [], "evidence_spans": [],
                "confidence": None, "rationale": f"Router skipped: {decision['reason']}", "rule_checks": [],
                "candidates_considered": [], "router": {"model": router, "decision": "skip", "reason": decision["reason"]},
                "call": {**decision["call"], "model_served": decision["call"].get("model_served")}}
    return go


def run(split: str, workers: int = 8, only: list | None = None, router: str | None = None) -> dict:
    labels = data.labels(split)
    if only:
        labels = [lb for lb in labels if lb["measure"] in only]
    fn = _routed_extract(router) if router else extract
    data.gaps()  # warm the store once before the pool; first use imports the dataset
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda lb: fn(lb["gap_id"]), labels))
    rows = [score_row(lb, res) for lb, res in zip(labels, results)]
    now = datetime.now(UTC)
    return {
        "run_id": f"{now:%Y%m%dT%H%M%SZ}-{split}" + (f"-{'+'.join(only)}" if only else "") + ("-router" if router else ""),
        "timestamp": now.isoformat(timespec="seconds"),
        "split": split,
        "model": MODEL,
        "effort": EFFORT,
        "router": router,
        "router_skipped": sum(1 for r in results if (r.get("router") or {}).get("decision") == "skip"),
        "router_errors": sum(1 for r in results if (r.get("router") or {}).get("error")),
        "prompt_versions": PROMPT_VERSIONS,
        "metrics": metrics(rows),
        "usage": usage_totals(rows),
        "rows": rows,
    }


def save(run_out: dict) -> None:
    runs = EVAL_DIR / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_out['run_id']}.json").write_text(json.dumps(run_out, indent=2) + "\n")
    hist_path = EVAL_DIR / "history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else []
    hist.append({k: run_out[k] for k in ("run_id", "timestamp", "split", "model", "effort", "prompt_versions",
                                         "metrics", "usage")})
    hist_path.write_text(json.dumps(hist, indent=2) + "\n")


def report(r: dict) -> str:
    m = r["metrics"]
    lines = [f"run {r['run_id']}  model={r['model']} effort={r['effort']}  prompts={r['prompt_versions']}"
             + (f"  router={r['router']} skipped={r.get('router_skipped')} router_errors={r.get('router_errors')}" if r.get("router") else ""),
             f"gaps={m['gaps']}  accuracy={m['accuracy']}  WRONG VALUES={m['wrong_value_count']}",
             f"field accuracy={m['field_accuracy']} (n={m['field_checked']})  evidence span match={m['evidence_span_match']}",
             f"abstain rate={m['abstain_rate']}  correct abstains={m['correct_abstains']}/{m['abstains']}  "
             f"reason match={m['abstain_reason_match']}  traps not accepted={m['traps_not_accepted']}/{m['traps']}",
             f"downgraded by rule checks={m['downgraded_by_rules']}",
             "type        precision  recall  pred/exp"]
    for t, v in m["per_type"].items():
        lines.append(f"{t:<11} {str(v['precision']):<10} {str(v['recall']):<7} {v['predicted']}/{v['expected']}")
    u = r["usage"]
    lines.append(f"tokens={u['tokens']}  served={u['models_served']}  est cost=${u['estimated_cost_usd']}")
    for key in ("tranche", "evidence_source"):
        lines.append(f"by {key:<36} n    correct  wrong  abstain  num.prec(LB)   note")
        for s in m.get("strata", {}).get(key, []):
            np_ = s["numerator_precision"]
            lines.append(f"  {str(s['key']):<36} {s['n']:<4} {s['correct']:<8} {s['wrong_values']:<6} {s['abstains']:<8} "
                         f"{str(np_['value']):<5}({np_['lb']})   {'n<' + str(MIN_STRATUM_N) + ': bound only' if s['below_min_n'] else ''}")
    misses = [x for x in r["rows"] if x["expected"] != x["predicted"] or x["wrong_value"] or x["field_mismatches"]]
    if misses:
        lines.append("misses:")
        for x in misses:
            flag = " WRONG-VALUE" if x["wrong_value"] else ""
            lines.append(f"  {x['gap_id']} {x['measure']:<5} {x['scenario']:<28} exp={x['expected']}"
                         f"({x['expected_reason'] or ''}) got={x['predicted']}({x['predicted_reason'] or ''})"
                         f"{' fields!=' + ','.join(x['field_mismatches']) if x['field_mismatches'] else ''}{flag}")
    return "\n".join(lines)


def diff_runs(run_a: dict, run_b: dict) -> dict:
    """Two saved runs on the same label set, gap by gap: where they agree, where they differ, and which was right."""
    a = {r["gap_id"]: r for r in run_a["rows"]}
    b = {r["gap_id"]: r for r in run_b["rows"]}
    common = sorted(set(a) & set(b))
    diffs = []
    for g in common:
        ra, rb = a[g], b[g]
        if (ra["predicted"], ra["predicted_reason"], ra["wrong_value"]) != (rb["predicted"], rb["predicted_reason"], rb["wrong_value"]):
            diffs.append({"gap_id": g, "measure": ra["measure"], "scenario": ra["scenario"], "expected": ra["expected"],
                          "a": ra["predicted"], "a_reason": ra["predicted_reason"], "a_correct": ra["expected"] == ra["predicted"],
                          "b": rb["predicted"], "b_reason": rb["predicted_reason"], "b_correct": rb["expected"] == rb["predicted"],
                          "a_wrong_value": ra["wrong_value"], "b_wrong_value": rb["wrong_value"]})
    return {"run_a": run_a["run_id"], "run_b": run_b["run_id"], "common": len(common), "agree": len(common) - len(diffs),
            "a_only_right": sum(d["a_correct"] and not d["b_correct"] for d in diffs),
            "b_only_right": sum(d["b_correct"] and not d["a_correct"] for d in diffs),
            "metrics_a": run_a["metrics"], "metrics_b": run_b["metrics"], "diffs": diffs}


def format_diff(d: dict) -> str:
    ma, mb = d["metrics_a"], d["metrics_b"]
    lines = [f"A = {d['run_a']}   B = {d['run_b']}   common gaps: {d['common']}  agree: {d['agree']}",
             f"A: accuracy {ma['accuracy']} wrong values {ma['wrong_value_count']} abstain rate {ma['abstain_rate']}  |  "
             f"B: accuracy {mb['accuracy']} wrong values {mb['wrong_value_count']} abstain rate {mb['abstain_rate']}",
             f"A right where B isn't: {d['a_only_right']}   B right where A isn't: {d['b_only_right']}"]
    for x in d["diffs"]:
        lines.append(f"  {x['gap_id']} {x['measure']:<5} {x['scenario']:<28} exp={x['expected']:<9} "
                     f"A={x['a']}({x['a_reason'] or ''}){' WRONG' if x['a_wrong_value'] else ''}  "
                     f"B={x['b']}({x['b_reason'] or ''}){' WRONG' if x['b_wrong_value'] else ''}")
    return "\n".join(lines)


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["show"]:
        sp = argparse.ArgumentParser(prog="attest.eval show")
        sp.add_argument("cmd")
        sp.add_argument("run_id", nargs="?", help="a saved run id; default: the latest held-out run")
        d = sp.parse_args(argv)
        run_id = d.run_id
        if not run_id:
            hist = json.loads((EVAL_DIR / "history.json").read_text())
            run_id = next(h["run_id"] for h in reversed(hist) if h["split"].endswith("heldout"))
        print(report(json.loads((EVAL_DIR / "runs" / f"{run_id}.json").read_text())))
        return 0
    if argv[:1] == ["diff"]:
        dp = argparse.ArgumentParser(prog="attest.eval diff")
        dp.add_argument("cmd")
        dp.add_argument("run_a")
        dp.add_argument("run_b")
        d = dp.parse_args(argv)
        runs = EVAL_DIR / "runs"
        print(format_diff(diff_runs(json.loads((runs / f"{d.run_a}.json").read_text()),
                                    json.loads((runs / f"{d.run_b}.json").read_text()))))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", default="dev", help="a label set in data/labels/<split>.json (dev, heldout, v2-dev, v2-heldout)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only", help="comma-separated measure codes to evaluate (dev work on new measures)")
    ap.add_argument("--router", help="score the router variant: cheap model first, skips count as none")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--gate-min-numerator-precision", type=float)
    ap.add_argument("--gate-max-wrong-values", type=int)
    ap.add_argument("--gate-max-wrong-values-per-stratum", type=int,
                    help="fail if any tranche has more wrong values than this")
    args = ap.parse_args(argv)

    r = run(args.split, args.workers, [m.strip() for m in args.only.split(",")] if args.only else None, args.router)
    if not args.no_save:
        save(r)
    print(report(r))

    failures = []
    m = r["metrics"]
    p = m["per_type"]["numerator"]["precision"]
    if args.gate_min_numerator_precision is not None and (p is None or p < args.gate_min_numerator_precision):
        failures.append(f"numerator precision {p} < {args.gate_min_numerator_precision}")
    if args.gate_max_wrong_values is not None and m["wrong_value_count"] > args.gate_max_wrong_values:
        failures.append(f"wrong values {m['wrong_value_count']} > {args.gate_max_wrong_values}")
    if args.gate_max_wrong_values_per_stratum is not None:
        bad = [s for s in m.get("strata", {}).get("tranche", []) if s["wrong_values"] > args.gate_max_wrong_values_per_stratum]
        if bad:
            failures.append("wrong values in tranche(s): " + ", ".join(f"{s['key']}={s['wrong_values']}" for s in bad))
    if failures:
        print("EVAL GATE FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    if any(v is not None for v in (args.gate_min_numerator_precision, args.gate_max_wrong_values,
                                   args.gate_max_wrong_values_per_stratum)):
        print("EVAL GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
