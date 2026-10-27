"""Batch extraction pipeline: idempotent, resumable jobs with per-tranche cost accounting.

    python -m attest.pipeline run --tranche TR-IMA-OWN --limit 200          # full extraction, this tranche
    python -m attest.pipeline run --limit 1000 --router claude-haiku-4-5    # cheap router first, QA-sampled
    python -m attest.pipeline run --job <job_id>                            # resume
    python -m attest.pipeline report [--job <job_id>] [--variant <id>]      # cost, cache, router recall by tranche

A variant is the configuration that produced a finding: model, effort, prompt versions, and
the router (model or none). Items are keyed on (gap, variant), so re-running continues from
the items that aren't done, and two variants can be compared on the same gaps.

The router guardrail: charts the router skips form a bucket that is
QA-sampled at QA_SAMPLE_RATE with the full extractor. A sampled skip where the full extractor
finds a numerator or exclusion is a router miss; recall on the unread bucket is reported per
tranche. Without that measurement, routing is a silent recall cut.
"""

import argparse
import json
import random
import sys
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from . import data, extractor, registry, store
from .config import EFFORT, MODEL, PRICE_PER_MTOK

QA_SAMPLE_RATE = 0.10
DEFAULT_RPM = 120  # requests per minute, per process

# USD per million tokens for models the pipeline may call (the extractor's table covers the default).
PRICES = {
    **PRICE_PER_MTOK,
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}


def price_for(model: str | None) -> dict | None:
    """Prices by model id, tolerating a served-model date suffix (claude-haiku-4-5-20251001 -> claude-haiku-4-5)."""
    if not model:
        return None
    if model in PRICES:
        return PRICES[model]
    for key in sorted(PRICES, key=len, reverse=True):
        if model.startswith(key):
            return PRICES[key]
    return None


def cost_usd(model: str, usage: dict) -> float:
    p = price_for(model)
    if p is None:
        raise KeyError(f"no price for model {model!r}; add it to attest.pipeline.PRICES")
    return round((usage.get("input_tokens", 0) * p["input"] + usage.get("output_tokens", 0) * p["output"]
                  + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
                  + usage.get("cache_creation_input_tokens", 0) * p["cache_write"]) / 1e6, 6)


def variant_id(router: str | None, model: str = MODEL, effort: str = EFFORT) -> str:
    return registry.variant_id(router, model=model, effort=effort)


class RateLimiter:
    def __init__(self, rpm: int):
        self.interval = 60.0 / max(rpm, 1)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        time.sleep(max(0.0, start - time.monotonic()))


def select_gaps(variant: str, tranche: str | None, measure: str | None, limit: int, include_done: bool = False) -> list:
    """Charted gaps for this run, minus the ones this variant already finished (idempotent)."""
    done = {(r["gap_id"]) for r in store.job_items(variant_id=variant) if r["status"] in ("done", "skipped_by_router", "qa_sample")}
    out = []
    for g in sorted(data.gaps().values(), key=lambda g: g["gap_id"]):
        if not g["document_ids"]:
            continue
        if tranche and g.get("tranche_id") != tranche:
            continue
        if measure and g["measure"] != measure:
            continue
        if not include_done and g["gap_id"] in done:
            continue
        out.append(g["gap_id"])
        if limit and len(out) >= limit:
            break
    return out


def process_one(gap_id: str, variant: str, job_id: str, router: str | None, limiter: RateLimiter, rng: random.Random,
                extract=extractor.extract, route=extractor.route) -> dict:
    gap = data.gaps()[gap_id]
    row = {"gap_id": gap_id, "variant_id": variant, "job_id": job_id, "tranche_id": gap.get("tranche_id"),
           "measure": gap["measure"], "status": "pending", "input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0, "router_model": router,
           "router_input_tokens": 0, "router_output_tokens": 0, "router_cache_read_tokens": 0, "router_cost_usd": 0.0}
    started = time.monotonic()

    def add_usage(call: dict, model: str, is_router: bool = False):
        u = call.get("usage") or {}
        c = cost_usd(model, u)
        if is_router:  # the router's tokens and cost are kept apart from the extractor's
            row["router_input_tokens"] += u.get("input_tokens", 0)
            row["router_output_tokens"] += u.get("output_tokens", 0)
            row["router_cache_read_tokens"] += u.get("cache_read_input_tokens", 0)
            row["router_cost_usd"] = round(row["router_cost_usd"] + c, 6)
        else:
            row["input_tokens"] += u.get("input_tokens", 0)
            row["output_tokens"] += u.get("output_tokens", 0)
            row["cache_read_tokens"] += u.get("cache_read_input_tokens", 0)
            row["cache_write_tokens"] += u.get("cache_creation_input_tokens", 0)
        row["cost_usd"] = round(row["cost_usd"] + c, 6)

    try:
        qa_sample = False
        if router:
            limiter.wait()
            decision = route(gap_id, router)
            add_usage(decision["call"], decision["call"].get("model_served") or router, is_router=True)
            row["router_decision"] = "error" if decision.get("error") else ("read" if decision["worth_reading"] else "skip")
            if not decision["worth_reading"]:
                qa_sample = rng.random() < QA_SAMPLE_RATE
                if not qa_sample:
                    row["status"] = "skipped_by_router"
                    row["latency_ms"] = round((time.monotonic() - started) * 1000)
                    store.put_job_item(row)
                    return row
        limiter.wait()
        finding = extract(gap_id)
        call = finding.get("call") or {}
        add_usage(call, call.get("model_served") or MODEL)
        row["model_served"] = call.get("model_served")
        row["finding_type"] = finding["finding_type"]
        finding["variant_id"] = variant
        if qa_sample:
            # The router said skip; the full extractor read it anyway so the skip bucket is measured.
            row["status"] = "qa_sample"
            row["qa_router_missed"] = int(finding["finding_type"] in ("numerator", "exclusion"))
            finding["router"] = {"model": router, "decision": "skip", "qa_sample": True}
        else:
            row["status"] = "done"
            if router:
                finding["router"] = {"model": router, "decision": "read"}
        store.put_finding(gap_id, finding)
    except Exception as e:  # noqa: BLE001  (a failed item is recorded and the job continues)
        row["status"] = "failed"
        row["error"] = f"{type(e).__name__}: {e}"[:500]
    row["latency_ms"] = round((time.monotonic() - started) * 1000)
    store.put_job_item(row)
    return row


def run(tranche=None, measure=None, limit=0, router=None, workers=8, rpm=DEFAULT_RPM, job_id=None,
        seed=0, extract=extractor.extract, route=extractor.route, progress=print) -> dict:
    store.init()
    data.gaps()  # the dataset must be in place before any job row: a first import clears the job tables
    variant = variant_id(router)
    if job_id:
        jobs = {j["job_id"]: j for j in store.all_rows("jobs")}
        if job_id not in jobs:
            raise SystemExit(f"no job {job_id}")
        params = jobs[job_id]["params"]
        tranche, measure, limit, router = params.get("tranche"), params.get("measure"), params.get("limit", 0), params.get("router")
        variant = jobs[job_id]["variant_id"]
    else:
        job_id = (f"job-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:4]}" + (f"-{tranche}" if tranche else "")
                  + ("-router" if router else ""))
        config = {"model": MODEL, "effort": EFFORT, "prompts": registry.prompt_versions(), "router": router,
                  "qa_sample_rate": QA_SAMPLE_RATE if router else None}
        store.upsert_variant(variant, "extraction", f"{MODEL} {EFFORT}" + (f" + router {router}" if router else ""), config)
        store.upsert_job(job_id, variant, {"tranche": tranche, "measure": measure, "limit": limit, "router": router,
                                           "workers": workers, "rpm": rpm, **config})
    if job_id in {j["job_id"] for j in store.all_rows("jobs")} and store.job_items(job_id=job_id):
        # Resume: retry this job's failed items, then fill up to the job's original limit with new gaps.
        mine = store.job_items(job_id=job_id)
        failed = [r["gap_id"] for r in mine if r["status"] in ("failed", "pending")]
        finished = sum(r["status"] in ("done", "skipped_by_router", "qa_sample") for r in mine)
        budget = max(0, limit - finished - len(failed)) if limit else 0
        gap_ids = failed + (select_gaps(variant, tranche, measure, budget) if (budget or not limit) else [])
    else:
        gap_ids = select_gaps(variant, tranche, measure, limit)
    progress(f"{job_id}: variant {variant}; {len(gap_ids)} gaps to process" + (f" (router {router})" if router else ""))
    limiter = RateLimiter(rpm)
    rng = random.Random(f"{seed}-{job_id}")
    done = 0
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process_one, g, variant, job_id, router, limiter, random.Random(f"{seed}-{g}"), extract, route)
                   for g in gap_ids]
        for fut in as_completed(futures):
            done += 1
            r = fut.result()
            if done % 25 == 0 or done == len(gap_ids):
                progress(f"  {done}/{len(gap_ids)} ({time.monotonic() - t0:.0f}s) last={r['gap_id']} {r['status']}")
    _ = rng
    return report(job_id=job_id)


def report(job_id: str | None = None, variant: str | None = None) -> dict:
    items = store.job_items(variant_id=variant, job_id=job_id)
    by_tranche: dict = defaultdict(lambda: {"items": 0, "done": 0, "skipped": 0, "qa_samples": 0, "qa_missed": 0,
                                           "failed": 0, "router_errors": 0, "cost_usd": 0.0, "router_cost_usd": 0.0,
                                           "input_tokens": 0, "output_tokens": 0,
                                           "cache_read_tokens": 0, "cache_write_tokens": 0,
                                           "findings": defaultdict(int)})
    for r in items:
        t = by_tranche[r["tranche_id"] or "-"]
        t["items"] += 1
        t["done"] += r["status"] == "done"
        t["skipped"] += r["status"] == "skipped_by_router"
        t["qa_samples"] += r["status"] == "qa_sample"
        t["qa_missed"] += r.get("qa_router_missed") or 0
        t["failed"] += r["status"] == "failed"
        t["router_errors"] += r.get("router_decision") == "error"
        for k in ("cost_usd", "router_cost_usd", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            t[k] += r.get(k) or 0
        if r.get("finding_type"):
            t["findings"][r["finding_type"]] += 1
    total = {"items": 0, "done": 0, "skipped": 0, "qa_samples": 0, "qa_missed": 0, "failed": 0, "router_errors": 0,
             "cost_usd": 0.0, "router_cost_usd": 0.0,
             "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    for t in by_tranche.values():
        for k in total:
            total[k] += t[k]
        t["findings"] = dict(t["findings"])
        t["cost_usd"] = round(t["cost_usd"], 4)
        t["router_cost_usd"] = round(t["router_cost_usd"], 4)
        n_read = t["done"] + t["qa_samples"]
        t["cost_per_gap"] = round(t["cost_usd"] / t["items"], 4) if t["items"] else None
        t["cache_hit_share"] = round(t["cache_read_tokens"] / (t["cache_read_tokens"] + t["input_tokens"]), 3) \
            if (t["cache_read_tokens"] + t["input_tokens"]) else None
        # Recall on the unread bucket: of the sampled skips, the share where the full extractor found nothing to accept.
        t["unread_bucket_recall"] = round(1 - t["qa_missed"] / t["qa_samples"], 3) if t["qa_samples"] else None
        t["read_share"] = round(n_read / t["items"], 3) if t["items"] else None
    total["cost_usd"] = round(total["cost_usd"], 4)
    total["cost_per_gap"] = round(total["cost_usd"] / total["items"], 4) if total["items"] else None
    total["cache_hit_share"] = round(total["cache_read_tokens"] / (total["cache_read_tokens"] + total["input_tokens"]), 3) \
        if (total["cache_read_tokens"] + total["input_tokens"]) else None
    total["unread_bucket_recall"] = round(1 - total["qa_missed"] / total["qa_samples"], 3) if total["qa_samples"] else None
    return {"job_id": job_id, "variant_id": variant or (items[0]["variant_id"] if items else None),
            "by_tranche": dict(by_tranche), "total": total}


def format_report(rep: dict) -> str:
    lines = [f"job {rep['job_id'] or '-'}  variant {rep['variant_id']}",
             "tranche        items  done  skip  qa  miss  fail   cost$   $/gap  cache  unread-recall"]
    for k, t in sorted(rep["by_tranche"].items()):
        lines.append(f"{k:<14} {t['items']:>5} {t['done']:>5} {t['skipped']:>5} {t['qa_samples']:>3} {t['qa_missed']:>5} "
                     f"{t['failed']:>5} {t['cost_usd']:>8.2f} {t['cost_per_gap'] or 0:>7.4f} "
                     f"{'-' if t['cache_hit_share'] is None else t['cache_hit_share']:>6} "
                     f"{'-' if t['unread_bucket_recall'] is None else t['unread_bucket_recall']:>7}")
    t = rep["total"]
    lines.append(f"{'total':<14} {t['items']:>5} {t['done']:>5} {t['skipped']:>5} {t['qa_samples']:>3} {t['qa_missed']:>5} "
                 f"{t['failed']:>5} {t['cost_usd']:>8.2f} {t['cost_per_gap'] or 0:>7.4f} "
                 f"{'-' if t['cache_hit_share'] is None else t['cache_hit_share']:>6} "
                 f"{'-' if t['unread_bucket_recall'] is None else t['unread_bucket_recall']:>7}")
    lines.append(f"tokens: input {t['input_tokens']:,}  output {t['output_tokens']:,}  cache read {t['cache_read_tokens']:,}  "
                 f"cache write {t['cache_write_tokens']:,}" + (f"  | router cost ${t['router_cost_usd']:.2f} (included)" if t["router_cost_usd"] else ""))
    if t["router_errors"]:
        lines.append(f"ROUTER ERRORS: {t['router_errors']} (those charts were read by the full extractor; the router did not run)")
    return "\n".join(lines)


def compare(variant_a: str, variant_b: str) -> dict:
    """Two variants on the gaps they both processed: per-tranche cost, cache, findings, and where they disagree."""
    a = {r["gap_id"]: r for r in store.job_items(variant_id=variant_a)}
    b = {r["gap_id"]: r for r in store.job_items(variant_id=variant_b)}
    common = sorted(set(a) & set(b))
    by: dict = defaultdict(lambda: {"gaps": 0, "cost_a": 0.0, "cost_b": 0.0, "skipped_b": 0, "qa_b": 0, "qa_missed_b": 0,
                                   "agree": 0, "b_missed_closure": 0, "b_missed_abstain": 0})
    for g in common:
        ra, rb = a[g], b[g]
        t = by[ra["tranche_id"] or "-"]
        t["gaps"] += 1
        t["cost_a"] += ra.get("cost_usd") or 0
        t["cost_b"] += rb.get("cost_usd") or 0
        t["skipped_b"] += rb["status"] == "skipped_by_router"
        t["qa_b"] += rb["status"] == "qa_sample"
        t["qa_missed_b"] += rb.get("qa_router_missed") or 0
        fa, fb = ra.get("finding_type"), rb.get("finding_type") or ("none" if rb["status"] == "skipped_by_router" else None)
        t["agree"] += fa == fb
        if rb["status"] == "skipped_by_router":
            t["b_missed_closure"] += fa in ("numerator", "exclusion")
            t["b_missed_abstain"] += fa == "abstain"
    for t in by.values():
        n = t["gaps"] or 1
        t["cost_per_gap_a"] = round(t["cost_a"] / n, 4)
        t["cost_per_gap_b"] = round(t["cost_b"] / n, 4)
        t["saving"] = round(1 - t["cost_b"] / t["cost_a"], 3) if t["cost_a"] else None
        t["skip_share_b"] = round(t["skipped_b"] / n, 3)
        t["agreement"] = round(t["agree"] / n, 3)
        t["unread_bucket_recall_b"] = round(1 - t["qa_missed_b"] / t["qa_b"], 3) if t["qa_b"] else None
    return {"variant_a": variant_a, "variant_b": variant_b, "gaps": len(common), "by_tranche": dict(by)}


def format_compare(c: dict) -> str:
    lines = [f"A = {c['variant_a']}   B = {c['variant_b']}   common gaps: {c['gaps']}",
             "tranche        gaps  $/gap A  $/gap B  saving  skip B  agree  B missed: closures abstains  unread-recall B"]
    tot = defaultdict(float)
    for k, t in sorted(c["by_tranche"].items()):
        saving = "-" if t["saving"] is None else f"{t['saving']:.0%}"
        lines.append(f"{k:<14} {t['gaps']:>4} {t['cost_per_gap_a']:>8.4f} {t['cost_per_gap_b']:>8.4f} "
                     f"{saving:>7} {t['skip_share_b']:>7.0%} {t['agreement']:>6.0%}  "
                     f"{t['b_missed_closure']:>17} {t['b_missed_abstain']:>8}  "
                     f"{'-' if t['unread_bucket_recall_b'] is None else t['unread_bucket_recall_b']:>8}")
        for key in ("gaps", "cost_a", "cost_b", "skipped_b", "agree", "b_missed_closure", "b_missed_abstain", "qa_b", "qa_missed_b"):
            tot[key] += t[key]
    n = tot["gaps"] or 1
    lines.append(f"{'total':<14} {int(tot['gaps']):>4} {tot['cost_a'] / n:>8.4f} {tot['cost_b'] / n:>8.4f} "
                 f"{(1 - tot['cost_b'] / tot['cost_a']) if tot['cost_a'] else 0:>7.0%} {tot['skipped_b'] / n:>7.0%} {tot['agree'] / n:>6.0%}  "
                 f"{int(tot['b_missed_closure']):>17} {int(tot['b_missed_abstain']):>8}  "
                 f"{'-' if not tot['qa_b'] else round(1 - tot['qa_missed_b'] / tot['qa_b'], 3):>8}")
    lines.append("B missed closures: gaps the router skipped where the full variant found a numerator or exclusion (on the same chart).")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tranche")
    r.add_argument("--measure")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--router", help="cheap model for the first pass, e.g. claude-haiku-4-5")
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--rpm", type=int, default=DEFAULT_RPM)
    r.add_argument("--job", help="resume this job")
    rp = sub.add_parser("report")
    rp.add_argument("--job")
    rp.add_argument("--variant")
    cp = sub.add_parser("compare")
    cp.add_argument("variant_a")
    cp.add_argument("variant_b")
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        store.init()
        print(format_compare(compare(args.variant_a, args.variant_b)))
        return 0
    if args.cmd == "run":
        rep = run(args.tranche, args.measure, args.limit, args.router, args.workers, args.rpm, args.job)
    else:
        store.init()
        rep = report(args.job, args.variant)
    print(format_report(rep))
    out = store.DB_PATH.parent / f"pipeline-{rep['job_id'] or 'variant'}.json" if store.DATABASE_URL.startswith("sqlite") else None
    if out:
        out.write_text(json.dumps(rep, indent=1, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
