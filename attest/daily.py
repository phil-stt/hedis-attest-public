"""The daily-set surface: a dealt set of gaps per reviewer, progress, stretch sets, QA audits, and
the supervisor view.

Guardrails, from docs/plan-v2.md: the set scores correct decisions, not volume. A resolved abstain, a
justified reject, and an accept each count as one decision done; a catch (rejecting a wrong proposal,
confirmed by QA) is a positive event; accepted findings are sampled for audit; there is no throughput
leaderboard. Each set is dealt stratified by finding type so a reviewer never sees forty accepts in a row.
"""

import uuid
from collections import defaultdict
from datetime import date

from . import data, store
from .eval import lower_bound_95

SET_SIZE = 40
STRETCH_SIZE = 10
BUCKETS = ("numerator", "exclusion", "abstain", "none")  # finding types a set is stratified over
# What a decision on each finding type is called in the day's tally.
TALLY = {"accept": "accepted", "reject": "caught", "needs_more": "needs_more"}


def today() -> str:
    return store.now()[:10]


# ---------------------------------------------------------------------------
# The pool and the deal
# ---------------------------------------------------------------------------

def _open_gaps(tranche_id=None) -> dict:
    """Gaps with a finding and no decision yet: the work a set can be dealt from."""
    decided = store.latest_decisions()
    findings = store.findings()
    out = {}
    for gid, gap in data.gaps().items():
        if tranche_id and gap.get("tranche_id") != tranche_id:
            continue
        if gid in decided or not gap["document_ids"]:
            continue
        f = findings.get(gid)
        if f is None:
            continue
        out[gid] = (gap, f)
    return out


def _reserved_elsewhere(reviewer: str) -> set:
    """Gaps sitting in another reviewer's unfinished set, or under another reviewer's lease."""
    items = store.set_items_all()
    taken = set()
    for s in store.sets():
        if s["reviewer"] != reviewer and not s["completed_at"]:
            taken.update(items.get(s["set_id"], []))
    for gid, lease in store.active_assignments().items():
        if lease["reviewer"] != reviewer:
            taken.add(gid)
    return taken


def _allocate(counts: dict, size: int) -> dict:
    """Largest-remainder apportionment of `size` over the buckets in proportion to the pool."""
    total = sum(counts.values())
    if total == 0:
        return {b: 0 for b in counts}
    size = min(size, total)
    raw = {b: size * n / total for b, n in counts.items()}
    alloc = {b: min(int(raw[b]), counts[b]) for b in counts}
    short = size - sum(alloc.values())
    for b in sorted(counts, key=lambda b: -(raw[b] - int(raw[b]))):
        while short > 0 and alloc[b] < counts[b]:
            alloc[b] += 1
            short -= 1
    return alloc


def deal(reviewer: str, tranche_id=None, kind: str = "daily", size: int | None = None) -> dict:
    """Deal a set from the open pool: stratified by finding type in the pool's proportions, each bucket in
    priority order, buckets interleaved so the set mixes. Returns the set row (empty set if nothing is open)."""
    size = size or (STRETCH_SIZE if kind == "stretch" else SET_SIZE)
    taken = _reserved_elsewhere(reviewer)
    by_bucket = defaultdict(list)
    for gid, (gap, f) in _open_gaps(tranche_id).items():
        if gid in taken:
            continue
        by_bucket[f["finding_type"]].append((gap, f))
    for b in by_bucket:
        by_bucket[b].sort(key=lambda gf: (-(gf[1].get("confidence") or 0), gf[0]["due_date"], gf[0]["gap_id"]))
    alloc = _allocate({b: len(by_bucket.get(b, [])) for b in BUCKETS}, size)
    queues = {b: [gf[0]["gap_id"] for gf in by_bucket.get(b, [])[:alloc[b]]] for b in BUCKETS}
    dealt = []
    while any(queues.values()):  # round-robin over the buckets
        for b in BUCKETS:
            if queues[b]:
                dealt.append(queues[b].pop(0))
    set_id = f"SET-{uuid.uuid4().hex[:8].upper()}"
    store.add_set(set_id, reviewer, tranche_id, kind, dealt, today())
    return next(s for s in store.sets(reviewer) if s["set_id"] == set_id)


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

def progress(set_row: dict) -> dict:
    """The set with its items, what has been decided, the tally by kind, and the next undecided gap."""
    gap_ids = store.set_items(set_row["set_id"])
    decisions, findings = store.latest_decisions(), store.findings()
    gaps, members = data.gaps(), data.members()
    items, tally = [], defaultdict(int)
    next_gap = None
    for gid in gap_ids:
        d, f = decisions.get(gid), findings.get(gid)
        ftype = (f or {}).get("finding_type")
        kind = None
        if d:
            if d["action"] == "accept":
                kind = "accepted"
            elif d["action"] == "reject":
                kind = "caught" if ftype in ("numerator", "exclusion") else "confirmed"
            elif d["action"] == "needs_more":
                kind = "needs_more"
            if ftype == "abstain" and d["action"] != "needs_more":
                kind = "resolved"
            tally[kind] += 1
        elif next_gap is None:
            next_gap = gid
        items.append({"gap": gaps[gid], "member": members[gaps[gid]["member_id"]], "finding": f, "decision": d,
                      "kind": kind})
    done = sum(1 for i in items if i["decision"])
    complete = done == len(items)
    if complete and not set_row["completed_at"] and items:
        store.complete_set(set_row["set_id"])
        set_row = {**set_row, "completed_at": store.now()}
    return {**set_row, "items": items, "done": done, "size": len(items), "complete": complete,
            "pct": round(100 * done / len(items)) if items else 100, "tally": dict(tally), "next_gap": next_gap}


def current_set(reviewer: str):
    """The reviewer's most recent set dealt today, with progress; None if nothing has been dealt today."""
    todays = [s for s in store.sets(reviewer) if s["dealt_on"] == today()]
    return progress(todays[-1]) if todays else None


def sets_today(reviewer: str) -> list:
    return [progress(s) for s in store.sets(reviewer) if s["dealt_on"] == today()]


def set_for_gap(reviewer: str, gap_id: str):
    """If the gap is in the reviewer's unfinished set today: position, size, and the next undecided gap."""
    for s in reversed(store.sets(reviewer)):
        if s["dealt_on"] != today():
            break
        ids = store.set_items(s["set_id"])
        if gap_id in ids:
            p = progress(s)
            after = [i["gap"]["gap_id"] for i in p["items"][ids.index(gap_id) + 1:] if not i["decision"]]
            before = [i["gap"]["gap_id"] for i in p["items"][:ids.index(gap_id)] if not i["decision"]]
            return {"set_id": s["set_id"], "kind": s["kind"], "position": ids.index(gap_id) + 1, "size": p["size"],
                    "done": p["done"], "next_gap": (after or before or [None])[0]}
    return None


def default_tranche(reviewer: str):
    """The provider-group tranche with the most open, unreserved work; None when the dataset has no tranches."""
    tr = {k: v for k, v in data.tranches().items() if v["kind"] != "line_of_business"}
    if not tr:
        return None
    taken = _reserved_elsewhere(reviewer)
    counts = defaultdict(int)
    for gid, (gap, _) in _open_gaps().items():
        if gid not in taken and gap.get("tranche_id") in tr:
            counts[gap["tranche_id"]] += 1
    return max(tr, key=lambda t: (counts[t], t)) if counts else next(iter(tr))


# ---------------------------------------------------------------------------
# QA and the supervisor view
# ---------------------------------------------------------------------------

def _rate(k, n):
    return {"k": k, "n": n, "value": round(k / n, 3) if n else None, "lb": lower_bound_95(k, n)}


def qa_by_surface() -> dict:
    """Per surface arm: audited accepts and how many were overturned (the audited error rate), and
    rejections of proposals that QA upheld (confirmed catches)."""
    out = defaultdict(lambda: {"audited_accepts": 0, "overturned_accepts": 0, "audited_rejects": 0,
                               "confirmed_catches": 0, "pending": 0})
    for a in store.qa_audits():
        s = out[a["surface"] or "unassigned"]
        if not a["verdict"]:
            s["pending"] += 1
            continue
        if a["action"] == "accept":
            s["audited_accepts"] += 1
            s["overturned_accepts"] += a["verdict"] == "overturned"
        elif a["action"] == "reject":
            s["audited_rejects"] += 1
            s["confirmed_catches"] += a["verdict"] == "upheld"
    return dict(out)


def supervisor(tranche_id=None) -> dict:
    """One tranche (or all): open gaps by status, today's decisions and reviewers, abstain rate, the audited
    error rate with n, confirmed catches, and sets dealt today. Catches sit next to throughput on purpose."""
    gaps = {g: v for g, v in data.gaps().items() if not tranche_id or v.get("tranche_id") == tranche_id}
    findings, decisions = store.findings(), store.latest_decisions()
    by_status = defaultdict(int)
    for gid, gap in gaps.items():
        d, f = decisions.get(gid), findings.get(gid)
        if d:
            by_status[d["action"]] += 1
        elif not gap["document_ids"]:
            by_status["no_document"] += 1
        elif f is None:
            by_status["not_processed"] += 1
        else:
            by_status[f["finding_type"]] += 1
    processed = sum(by_status[k] for k in BUCKETS)
    abstain_rate = _rate(by_status["abstain"], processed)
    t = today()
    todays = [d for d in store.decisions_all() if d["gap_id"] in gaps and d["decided_at"][:10] == t]
    reviewers = sorted({d["reviewer"] for d in todays})
    audits = [a for a in store.qa_audits() if a["gap_id"] in gaps]
    audited_accepts = [a for a in audits if a["action"] == "accept" and a["verdict"]]
    audited_rejects = [a for a in audits if a["action"] == "reject" and a["verdict"]]
    error = _rate(sum(a["verdict"] == "overturned" for a in audited_accepts), len(audited_accepts))
    catches = sum(a["verdict"] == "upheld" for a in audited_rejects)
    items = store.set_items_all()
    sets_rows = [s for s in store.sets() if s["dealt_on"] == t
                 and (not tranche_id or s["tranche_id"] == tranche_id or any(g in gaps for g in items.get(s["set_id"], [])))]
    per_reviewer = []
    for r in reviewers:
        mine = [d for d in todays if d["reviewer"] == r]
        per_reviewer.append({
            "reviewer": r, "decisions": len(mine),
            "accepts": sum(d["action"] == "accept" for d in mine),
            "catches_unaudited": sum(d["action"] == "reject" and (findings.get(d["gap_id"]) or {}).get("finding_type") in ("numerator", "exclusion") for d in mine),
            "abstains_resolved": sum((findings.get(d["gap_id"]) or {}).get("finding_type") == "abstain" and d["action"] != "needs_more" for d in mine),
            "needs_more": sum(d["action"] == "needs_more" for d in mine),
            "sets": sum(s["reviewer"] == r for s in sets_rows),
            "sets_complete": sum(s["reviewer"] == r and bool(s["completed_at"]) for s in sets_rows),
        })
    return {
        "tranche_id": tranche_id, "gaps": len(gaps), "by_status": dict(by_status), "processed": processed,
        "open": sum(by_status[k] for k in BUCKETS), "abstain_rate": abstain_rate,
        "decisions_today": len(todays), "reviewers_today": reviewers, "per_reviewer": per_reviewer,
        "audited_error_rate": error, "audits_pending": sum(1 for a in audits if not a["verdict"]),
        "confirmed_catches": catches, "catches_audited": len(audited_rejects),
        "sets_dealt": len(sets_rows), "sets_complete": sum(bool(s["completed_at"]) for s in sets_rows),
        "date": date.fromisoformat(t),
    }
