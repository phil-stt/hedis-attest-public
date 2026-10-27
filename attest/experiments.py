"""The experimentation layer: variants, assignment, exposure, and per-arm metrics.

Two kinds of variant:
- extraction: the configuration that produced a finding (model, effort, prompt versions, router).
  Registered by the pipeline when it runs; compared offline on labeled sets (`eval diff`) and on
  common gaps (`pipeline compare`).
- surface: the UI arm a reviewer works in. Assigned by a hash of the reviewer id, overridable with a
  cookie so one person can see every arm. Exposures record what each reviewer saw; decisions carry
  the arm and the extraction variant behind the finding.

With one reviewer there is no statistical test here; the layer exists so that when there are many,
the comparison is already instrumented. The comparison page shows n and a lower bound on every rate,
and the QA-audited error rate (the metric that matters) comes from a second reviewer's verdicts on a sample.
"""

import hashlib
from collections import defaultdict
from datetime import datetime

from . import daily, store
from .eval import lower_bound_95

# Surface arms. The daily set is the third; it lands the reviewer on /today instead of the queue.
SURFACES = {
    "daily_set": {"label": "Daily set", "sort": "priority", "home": "/today",
                  "description": "A dealt set of 40 from the reviewer's tranche, mixed by finding type, with progress and a stretch set."},
    "queue": {"label": "Queue, Attest priority order", "sort": "priority",
              "description": "One list; findings to confirm first, most confident first, then abstains."},
    "queue_due": {"label": "Queue, due date first", "sort": "due",
                  "description": "The same list ordered by the gap's due date, so urgency leads."},
}
DEFAULT_SURFACE = "queue"
MIN_RATE_HOURS = 0.25  # decisions per hour is shown only once an arm has 15 minutes of reviewer time
OVERRIDE_COOKIE = "attest_arm"


def ensure_registered() -> None:
    for sid, s in SURFACES.items():
        store.upsert_variant(sid, "surface", s["label"], {"sort": s["sort"], "description": s["description"]})


def assigned_surface(reviewer: str, override: str | None = None) -> tuple[str, bool]:
    """The arm for this reviewer: the override cookie if it names a live arm, else a stable hash."""
    if override in SURFACES:
        return override, True
    arms = sorted(SURFACES)
    h = int(hashlib.sha1(reviewer.encode()).hexdigest(), 16)
    return arms[h % len(arms)], False


def record_exposure(reviewer: str, surface: str, page: str, gap_id=None, override=False) -> None:
    store.add_exposure(reviewer, surface, page, gap_id=gap_id, override=override)


def _rate(k: int, n: int) -> dict:
    return {"k": k, "n": n, "value": round(k / n, 3) if n else None, "lb": lower_bound_95(k, n)}


def _active_hours(stamps: list) -> float:
    """Reviewer-hours from exposure timestamps: gaps of more than 30 minutes between page views don't count."""
    if not stamps:
        return 0.0
    ts = sorted(datetime.fromisoformat(s) for s in stamps)
    total = 0.0
    for a, b in zip(ts, ts[1:]):
        d = (b - a).total_seconds()
        if d <= 1800:
            total += d
    return round(total / 3600 + 1 / 60, 3)  # a minimum minute per session so a single view isn't zero


def surface_metrics() -> list:
    """Per surface arm: reviewers, exposures, decisions, decisions per reviewer-hour, rates with n and LB."""
    exps = store.exposures()
    decs = store.decisions_all()
    findings = store.findings()
    qa = daily.qa_by_surface()
    by_arm_exp = defaultdict(list)
    for e in exps:
        by_arm_exp[e["surface"]].append(e)
    by_arm_dec = defaultdict(list)
    for d in decs:
        by_arm_dec[d.get("surface") or "unassigned"].append(d)
    out = []
    for sid in list(SURFACES) + [a for a in by_arm_dec if a not in SURFACES]:
        es, ds = by_arm_exp.get(sid, []), by_arm_dec.get(sid, [])
        reviewers = {e["reviewer"] for e in es} | {d["reviewer"] for d in ds}
        hours = sum(_active_hours([e["seen_at"] for e in es if e["reviewer"] == r]) for r in reviewers)
        n = len(ds)
        accepts = sum(d["action"] == "accept" for d in ds)
        rejects = sum(d["action"] == "reject" for d in ds)
        needs = sum(d["action"] == "needs_more" for d in ds)
        # Abstain resolutions: decisions on gaps whose finding was an abstain (the nurse resolved what Attest refused).
        abst = sum(1 for d in ds if (findings.get(d["gap_id"]) or {}).get("finding_type") == "abstain")
        # Catches: rejections of a proposed numerator/exclusion (QA confirmation comes in Phase 5).
        catches = sum(1 for d in ds if d["action"] == "reject"
                      and (findings.get(d["gap_id"]) or {}).get("finding_type") in ("numerator", "exclusion"))
        out.append({
            "surface": sid, "label": SURFACES.get(sid, {}).get("label", sid),
            "reviewers": len(reviewers), "exposures": len(es), "reviewer_hours": round(hours, 2),
            "decisions": n,
            # No rate on a sliver of time: under MIN_RATE_HOURS of exposure the number is noise, so it stays blank.
            "decisions_per_hour": round(n / hours, 1) if hours >= MIN_RATE_HOURS else None,
            "accept": _rate(accepts, n), "reject": _rate(rejects, n), "needs_more": _rate(needs, n),
            "abstain_resolutions": abst, "catches_unaudited": catches,
            # From QA: a sample of accepts re-reviewed (overturned / audited) and rejects of proposals upheld.
            "qa_audited_error_rate": _rate(q["overturned_accepts"], q["audited_accepts"]) if (q := qa.get(sid)) else _rate(0, 0),
            "catches_confirmed": qa.get(sid, {}).get("confirmed_catches", 0),
            "qa_pending": qa.get(sid, {}).get("pending", 0),
        })
    return out


def extraction_metrics() -> list:
    """Per extraction variant seen in findings: count, finding mix, cost per gap from the pipeline's items."""
    findings = store.findings()
    items = store.job_items()
    by_v = defaultdict(lambda: {"findings": 0, "mix": defaultdict(int), "items": 0, "cost": 0.0, "skipped": 0,
                                "qa": 0, "qa_missed": 0})
    for f in findings.values():
        v = f.get("variant_id") or "unversioned"
        by_v[v]["findings"] += 1
        by_v[v]["mix"][f["finding_type"]] += 1
    for r in items:
        v = r["variant_id"]
        by_v[v]["items"] += 1
        by_v[v]["cost"] += r.get("cost_usd") or 0
        by_v[v]["skipped"] += r["status"] == "skipped_by_router"
        by_v[v]["qa"] += r["status"] == "qa_sample"
        by_v[v]["qa_missed"] += r.get("qa_router_missed") or 0
    registry = {v["variant_id"]: v for v in store.variants("extraction")}
    out = []
    for v, t in sorted(by_v.items()):
        out.append({"variant_id": v, "config": (registry.get(v) or {}).get("config") or {},
                    "findings": t["findings"], "mix": dict(t["mix"]), "items": t["items"],
                    "cost_per_gap": round(t["cost"] / t["items"], 4) if t["items"] else None,
                    "skip_share": round(t["skipped"] / t["items"], 3) if t["items"] else None,
                    "unread_bucket_recall": round(1 - t["qa_missed"] / t["qa"], 3) if t["qa"] else None})
    return out
