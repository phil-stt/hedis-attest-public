"""Attest web app: review queue, review view, eval panel, insight view.

Server-rendered (FastAPI + Jinja). The AI's findings are read from the state
store; only a reviewer's Accept writes a record. There is no code path in which
the model marks a gap closed.
"""

import base64
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import daily, data, experiments, registry, seed, service, store
from . import eval as evaluation
from .config import EVAL_DIR, HELDOUT_SPLIT, MEASUREMENT_YEAR

HERE = Path(__file__).resolve().parent
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD")
TODAY = date.fromisoformat(os.environ["ATTEST_TODAY"]) if os.environ.get("ATTEST_TODAY") else date.today()



@asynccontextmanager
async def lifespan(_app):
    data.clear_cache()  # (re)start: pick up the selected dataset and schema
    data.gaps()  # ensures the store schema and the selected dataset
    # Findings come from the committed snapshot: loaded into an empty store, and reloaded when the snapshot
    # file changes (a re-extracted gap, a new variant), so a deploy carries its findings without a reseed.
    if not store.findings() or store.get_meta("snapshot") != seed.snapshot_tag():
        seed.load_snapshot()
    experiments.ensure_registered()
    yield


app = FastAPI(title="Attest", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

# Measures the UI lists: the active ones from the registry (planned and registry-only measures appear
# on the measures page).
MEASURES = {code: {"name": m["name"], "closes": m["closes"]} for code, m in registry.active().items()}

FINDING_LABELS = {"numerator": "Numerator evidence", "exclusion": "Exclusion evidence",
                  "abstain": "Abstained", "none": "No evidence found"}

REASON_LABELS = {
    "bp_range_or_threshold": "BP given as a range or description, not an exact reading",
    "ineligible_setting": "Reading from an ineligible setting (ED, inpatient, or day of procedure)",
    "home_reading_not_digital": "Member-reported home reading without a digital device",
    "missing_value": "No numeric result documented",
    "missing_or_ambiguous_date": "Date missing or too vague to determine",
    "outside_lookback_window": "Outside the measurement year or lookback window",
    "not_completed": "Ordered or handed out, no completed result",
    "evidence_not_verified": "Quoted evidence could not be verified in the source",
    "failed_rule_check": "Failed a measure rule check",
    "model_refused": "Model declined to process this chart",
    "model_error": "Model call failed",
    "incomplete_evidence": "Only part of the required evidence is documented",
    "ineligible_author": "Documented by a role the measure does not accept",
}

FIELD_LABELS = {"exclusion_type": "Exclusion type", "supporting_date": "Supporting date"}
for _spec in registry.extraction_specs().values():
    FIELD_LABELS.update(_spec["field_labels"])

REJECT_REASONS = ["Wrong value", "Wrong date", "Ineligible setting or source", "Not this member",
                  "Evidence insufficient", "Other"]


# ---------------------------------------------------------------------------
# Demo password (HTTP Basic; the username is recorded as the reviewer)
# ---------------------------------------------------------------------------

@app.middleware("http")
async def demo_password(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    # The Basic-auth username is the reviewer identity. Without DEMO_PASSWORD (local dev) any
    # username is accepted, which lets two local sessions act as two reviewers.
    user, password = "local.reviewer", None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            u, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            user = u or "reviewer"
        except ValueError:
            password = None
    if DEMO_PASSWORD and not (password is not None and secrets.compare_digest(password, DEMO_PASSWORD)):
        return Response("Attest demo: password required", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="Attest demo"'})
    request.state.reviewer = user
    # The experiment arm: a stable hash of the reviewer, or the override cookie.
    request.state.surface, request.state.arm_override = experiments.assigned_surface(
        user, request.cookies.get(experiments.OVERRIDE_COOKIE))
    return await call_next(request)


# Not /healthz: Cloud Run reserves paths ending in "z" and answers them itself with a 404.
@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------

def gap_status(gap: dict, finding, decision) -> str:
    if decision:
        return {"accept": "accepted", "reject": "rejected", "needs_more": "needs_more"}[decision["action"]]
    if not gap["document_ids"]:
        return "no_document"
    if finding is None:
        return "not_processed"
    return {"numerator": "finding_ready", "exclusion": "finding_ready",
            "abstain": "abstained", "none": "no_evidence"}[finding["finding_type"]]


STATUS_LABELS = {"finding_ready": "Finding ready", "abstained": "Abstained", "no_evidence": "No evidence found",
                 "not_processed": "Not yet processed", "no_document": "No chart yet (chase)",
                 "accepted": "Accepted", "rejected": "Rejected", "needs_more": "Needs more"}

# The AI decides queue order: findings to confirm first (most confident first), then abstains a
# person must resolve, then charts with nothing found, then unprocessed work.
PRIORITY = {"finding_ready": 0, "abstained": 1, "no_evidence": 2, "not_processed": 3, "no_document": 4,
            "needs_more": 5, "rejected": 6, "accepted": 7}


def queue_rows() -> list:
    members, fs, ds = data.members(), store.findings(), store.latest_decisions()
    leases = store.active_assignments()
    rows = []
    for gap in data.gaps().values():
        f, d = fs.get(gap["gap_id"]), ds.get(gap["gap_id"])
        status = gap_status(gap, f, d)
        due = date.fromisoformat(gap["due_date"])
        rows.append({
            "gap": gap, "member": members[gap["member_id"]], "finding": f, "decision": d,
            "lease": leases.get(gap["gap_id"]),
            "status": status, "status_label": STATUS_LABELS[status],
            "confidence": f.get("confidence") if f else None,
            "days_left": (due - TODAY).days,
        })
    return rows


def highlight(text: str, spans) -> Markup:
    """Mark every evidence span that falls in this document (spans may overlap; they are merged)."""
    ranges = sorted((s["start"], s["end"]) for s in (spans or []))
    merged = []
    for a, b in ranges:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    out, pos = [], 0
    for i, (a, b) in enumerate(merged):
        out.append(escape(text[pos:a]))
        out.append(Markup(f'<mark id="evidence{"" if i == 0 else i + 1}">{escape(text[a:b])}</mark>'))
        pos = b
    out.append(escape(text[pos:]))
    return Markup("").join(out)


def ctx(request: Request, **kw) -> dict:
    return {"request": request, "reviewer": request.state.reviewer, "measures": MEASURES,
            "surface": request.state.surface, "arm_override": request.state.arm_override,
            "surfaces": experiments.SURFACES,
            "finding_labels": FINDING_LABELS, "reason_labels": REASON_LABELS, "field_labels": FIELD_LABELS,
            "my": MEASUREMENT_YEAR, **kw}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request):
    # The arm decides the landing page: the daily set lands on /today, the queue arms on /queue.
    return RedirectResponse(experiments.SURFACES[request.state.surface].get("home", "/queue"), status_code=303)


PAGE_SIZE = 100


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request, sort: str = "", measure: str = "", show: str = "open", tranche: str = "",
          page_no: int = 1):
    # The arm decides the default order; an explicit sort in the URL still wins.
    sort = sort or experiments.SURFACES[request.state.surface]["sort"]
    experiments.record_exposure(request.state.reviewer, request.state.surface, "queue", override=request.state.arm_override)
    rows = queue_rows()
    if tranche:
        rows = [r for r in rows if r["gap"].get("tranche_id") == tranche]
    today = TODAY.isoformat()
    counters = {
        "in_queue": sum(r["status"] not in ("accepted", "rejected") for r in rows),
        "awaiting": sum(r["status"] == "finding_ready" for r in rows),
        "accepted_today": sum(r["status"] == "accepted" and r["decision"]["decided_at"][:10] == today for r in rows),
        "abstained": sum(r["status"] == "abstained" for r in rows),
        "decided": sum(r["decision"] is not None for r in rows),
    }
    if measure:
        rows = [r for r in rows if r["gap"]["measure"] == measure]
    if show == "open":
        rows = [r for r in rows if r["status"] not in ("accepted", "rejected")]
    if sort == "measure":
        rows.sort(key=lambda r: (r["gap"]["measure"], PRIORITY[r["status"]], r["gap"]["gap_id"]))
    elif sort == "confidence":
        rows.sort(key=lambda r: (r["confidence"] is None, -(r["confidence"] or 0), r["gap"]["gap_id"]))
    elif sort == "due":
        rows.sort(key=lambda r: (r["days_left"], r["gap"]["gap_id"]))
    else:
        rows.sort(key=lambda r: (PRIORITY[r["status"]], -(r["confidence"] or 0), r["gap"]["gap_id"]))
    total = len(rows)
    pages = max(1, -(-total // PAGE_SIZE))
    page_no = min(max(1, page_no), pages)
    rows = rows[(page_no - 1) * PAGE_SIZE:page_no * PAGE_SIZE]
    return templates.TemplateResponse(request, "queue.html", ctx(
        request, rows=rows, counters=counters, sort=sort, measure=measure, show=show, tranche=tranche,
        tranches={k: v for k, v in data.tranches().items() if v["kind"] != "line_of_business"},
        total=total, page_no=page_no, pages=pages, page_size=PAGE_SIZE, page="queue"))


def _next_gap(after: str):
    for r in sorted(queue_rows(), key=lambda r: (PRIORITY[r["status"]], -(r["confidence"] or 0), r["gap"]["gap_id"])):
        if r["status"] in ("finding_ready", "abstained", "no_evidence") and r["gap"]["gap_id"] != after:
            return r["gap"]["gap_id"]
    return None


@app.get("/gap/{gap_id}", response_class=HTMLResponse)
def review(request: Request, gap_id: str, doc: str = "", expand: str = ""):
    gap = data.gaps().get(gap_id)
    if not gap:
        raise HTTPException(404)
    f = store.finding(gap_id)
    decision = store.latest_decisions().get(gap_id)
    records = store.records(gap_id)
    experiments.record_exposure(request.state.reviewer, request.state.surface, "review", gap_id=gap_id,
                                override=request.state.arm_override)
    # Opening a gap takes a lease on it (or renews yours). Someone else's lease means read-only.
    lock = None
    if not records and f is not None:
        try:
            service.claim(gap_id, request.state.reviewer)
        except service.Locked as e:
            lock = {"reviewer": e.reviewer, "until": e.until}
    spans = (f or {}).get("evidence_spans") or []
    doc_ids = gap["document_ids"]
    evidence_docs = [s["doc_id"] for s in spans]
    active = doc or (evidence_docs[0] if evidence_docs else (doc_ids[-1] if doc_ids else None))
    docs = [{**data.documents()[d], "active": d == active, "has_evidence": d in evidence_docs} for d in doc_ids]
    body = None
    if active:
        body = highlight(data.document_text(active), [s for s in spans if s["doc_id"] == active])
    return templates.TemplateResponse(request, "review.html", ctx(
        request, gap=gap, member=data.members()[gap["member_id"]], provider=data.providers()[gap["provider_id"]],
        finding=f, decision=decision, status=gap_status(gap, f, decision), docs=docs, active=active, body=body,
        spec=registry.spec_for_gap(gap), fields=registry.spec_for_gap(gap)["fields"],
        record=records[-1] if records else None,
        record_json=json.dumps(records[-1], indent=2) if records else None,
        reject_reasons=REJECT_REASONS, next_gap=_next_gap(gap_id), expand=expand, lock=lock,
        in_set=daily.set_for_gap(request.state.reviewer, gap_id),
        tranche=data.tranches().get(gap.get("tranche_id")),
        identities=data.identities().get(gap["member_id"], []), page="queue"))


@app.post("/gap/{gap_id}/decision")
def decide(request: Request, gap_id: str, action: str = Form(...), reason: str = Form(""), note: str = Form("")):
    if gap_id not in data.gaps() or action not in service.ACTIONS:
        raise HTTPException(400)
    try:
        service.record_decision(gap_id, action, request.state.reviewer, reason=reason or None, note=note or None,
                                surface=request.state.surface)
    except (service.NothingToAccept, service.AlreadyDecided, service.Locked) as e:
        raise HTTPException(409, str(e)) from e
    return RedirectResponse(f"/gap/{gap_id}", status_code=303)


# ---------------------------------------------------------------------------
# Daily set, QA, supervisor
# ---------------------------------------------------------------------------

def _tranche_choices() -> dict:
    return {k: v for k, v in data.tranches().items() if v["kind"] != "line_of_business"}


@app.get("/today", response_class=HTMLResponse)
def today_page(request: Request):
    reviewer = request.state.reviewer
    experiments.record_exposure(reviewer, request.state.surface, "today", override=request.state.arm_override)
    sets = daily.sets_today(reviewer)
    current = sets[-1] if sets else None
    return templates.TemplateResponse(request, "today.html", ctx(
        request, page="today", current=current, sets=sets, tranches=_tranche_choices(),
        default_tranche=daily.default_tranche(reviewer), set_size=daily.SET_SIZE, stretch_size=daily.STRETCH_SIZE,
        day=date.fromisoformat(daily.today())))


@app.post("/today/deal")
def deal(request: Request, tranche: str = Form(""), kind: str = Form("daily")):
    if kind not in ("daily", "stretch") or (tranche and tranche not in _tranche_choices()):
        raise HTTPException(400)
    current = daily.current_set(request.state.reviewer)
    if current and not current["complete"]:
        raise HTTPException(409, "Finish the set you have before dealing another.")
    if kind == "stretch" and not current:
        raise HTTPException(409, "A stretch set follows a completed daily set.")
    daily.deal(request.state.reviewer, tranche_id=tranche or None, kind=kind)
    return RedirectResponse("/today", status_code=303)


@app.get("/qa", response_class=HTMLResponse)
def qa_page(request: Request, show: str = "pending"):
    audits = store.qa_audits(pending_only=(show == "pending"))
    gaps, fs, members = data.gaps(), store.findings(), data.members()
    rows = [{**a, "gap": gaps.get(a["gap_id"]), "finding": fs.get(a["gap_id"]),
             "member": members.get((gaps.get(a["gap_id"]) or {}).get("member_id")),
             "mine": a["reviewer"] == request.state.reviewer} for a in audits if a["gap_id"] in gaps]
    return templates.TemplateResponse(request, "qa.html", ctx(
        request, page="qa", rows=rows, show=show, sample_rate=service.QA_ACCEPT_SAMPLE_RATE))


@app.post("/qa/{audit_id}")
def qa_verdict(request: Request, audit_id: int, verdict: str = Form(...), note: str = Form("")):
    try:
        service.record_qa_verdict(audit_id, request.state.reviewer, verdict, note=note or None)
    except (service.SelfAudit, service.AlreadyDecided) as e:
        raise HTTPException(409, str(e)) from e
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    return RedirectResponse("/qa", status_code=303)


@app.get("/supervise", response_class=HTMLResponse)
def supervise(request: Request, tranche: str = ""):
    choices = _tranche_choices()
    if tranche and tranche not in choices:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "supervise.html", ctx(
        request, page="supervise", tranche=tranche, tranches=choices, view=daily.supervisor(tranche or None),
        status_labels={**STATUS_LABELS, "numerator": "Finding ready", "exclusion": "Exclusion ready",
                       "abstain": "Abstained", "none": "No evidence found", "accept": "Accepted",
                       "reject": "Rejected", "needs_more": "Needs more"}))


@app.get("/records.json")
def records_json():
    return JSONResponse(store.records())


@app.post("/admin/reset")
def admin_reset():
    store.reset_reviews()
    return RedirectResponse("/queue", status_code=303)


@app.get("/gap/{gap_id}/finding.json")
def finding_json(gap_id: str):
    f = store.finding(gap_id)
    if f is None:
        raise HTTPException(404)
    return JSONResponse(json.loads(json.dumps(f)))


def _pct(k, n):
    return {"k": k, "n": n, "value": round(k / n, 3) if n else None, "lb": evaluation.lower_bound_95(k, n)}


def _load_runs():
    hist_path = EVAL_DIR / "history.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    latest = next((h for h in reversed(history) if h["split"] == HELDOUT_SPLIT), None)
    run = None
    if latest and (EVAL_DIR / "runs" / f"{latest['run_id']}.json").exists():
        run = json.loads((EVAL_DIR / "runs" / f"{latest['run_id']}.json").read_text())
    return history, latest, run


def _enriched_rows(run: dict) -> list:
    """Rows with their stratum keys. Runs saved before Phase 6 lack them; they are rebuilt from the labels."""
    rows = run.get("rows", [])
    if rows and "tranche" in rows[0]:
        return rows
    try:
        labels = {lb["gap_id"]: lb for lb in data.labels(run["split"])}
    except ValueError:
        return rows
    return [{**r, **evaluation._strata_of(labels[r["gap_id"]])} if r["gap_id"] in labels else r for r in rows]


def _previous_heldout_strata(history: list, latest_run_id: str, key: str = "tranche") -> dict:
    """{stratum: accuracy} from the most recent earlier held-out run that saved strata (drift baseline)."""
    for h in reversed(history):
        if h["split"] != HELDOUT_SPLIT or h["run_id"] == latest_run_id:
            continue
        s = (h.get("metrics") or {}).get("strata", {}).get(key)
        if s:
            return {x["key"]: x["accuracy"] for x in s}
        path = EVAL_DIR / "runs" / f"{h['run_id']}.json"
        if path.exists():  # older run: rebuild from its rows
            prev = json.loads(path.read_text())
            return {x["key"]: x["accuracy"] for x in evaluation.stratified(_enriched_rows(prev), key)}
    return {}


@app.get("/eval", response_class=HTMLResponse)
def eval_panel(request: Request):
    history, latest, run = _load_runs()
    view = None
    if latest:
        m = latest["metrics"]
        per_type = [{"type": t, "precision": _pct(v["correct"], v["predicted"]), "recall": _pct(v["correct"], v["expected"])}
                    for t, v in m["per_type"].items()]
        rows = (run or {}).get("rows", [])
        measures = []
        for code in MEASURES:
            rs = [r for r in rows if r["measure"] == code]
            measures.append({"code": code, "n": len(rs), "correct": sum(r["expected"] == r["predicted"] for r in rs),
                             "wrong": sum(r["wrong_value"] for r in rs),
                             "abstains": sum(r["predicted"] == "abstain" for r in rs)})
        view = {
            "run": latest, "per_type": per_type, "measures": measures,
            "overall": _pct(sum(v["correct"] for v in m["per_type"].values()), m["gaps"]),
            "fields": _pct(round((m["field_accuracy"] or 0) * m["field_checked"]), m["field_checked"]),
            "abstains": _pct(m["correct_abstains"], m["abstains"]),
            "traps": _pct(m["traps_not_accepted"], m["traps"]),
            "misses": [r for r in rows if r["expected"] != r["predicted"] or r["wrong_value"]],
            "gate_pass": evaluation.gate_passes(m),
        }
        # Phase 6: the same run cut by tranche, line of business, evidence source, fragmentation, document types.
        enriched = _enriched_rows(run or {"rows": [], "split": latest["split"]})
        baseline = _previous_heldout_strata(history, latest["run_id"])
        view["strata"] = []
        for key, label in evaluation.STRATA.items():
            if key == "measure":
                continue  # the measure table above already covers it
            rows_k = evaluation.stratified(enriched, key)
            if key == "tranche":
                for s in rows_k:
                    prev = baseline.get(s["key"])
                    s["drift"] = round(s["accuracy"]["value"] - prev["value"], 3) if prev and prev.get("value") is not None and s["accuracy"]["value"] is not None else None
            view["strata"].append({"key": key, "label": label, "rows": rows_k})
        view["is_router"] = bool((run or {}).get("router"))
    recent = [{**h, "gate_pass": evaluation.gate_passes(h["metrics"]), "is_heldout": h["split"] == HELDOUT_SPLIT}
              for h in reversed(history[-12:]) if h["split"] in (HELDOUT_SPLIT, HELDOUT_SPLIT.replace("heldout", "dev"))]
    return templates.TemplateResponse(request, "eval.html", ctx(
        request, page="eval", view=view, history=recent, min_n=evaluation.MIN_STRATUM_N,
        gate={"precision": evaluation.GATE_MIN_NUMERATOR_PRECISION, "wrong": evaluation.GATE_MAX_WRONG_VALUES,
              "wrong_per_stratum": evaluation.GATE_MAX_WRONG_VALUES_PER_STRATUM}))


# Assumed supplemental-data cutoff for MY2026 chase work (a plan-specific date; configurable).
SUPPLEMENTAL_CUTOFF = date.fromisoformat(os.environ.get("ATTEST_SUPPLEMENTAL_CUTOFF", "2027-01-31"))
CHASE_WEIGHTS = {"provider_yield": 0.5, "measure_yield": 0.3, "urgency": 0.2}
PRIOR_CHARTS = 4


def _bucket(gap, finding):
    if not gap["document_ids"]:
        return "no_chart"
    if finding is None:
        return "not_processed"
    return {"numerator": "evidence", "exclusion": "evidence"}.get(finding["finding_type"], finding["finding_type"])


def insight_data(tranche_id=None) -> dict:
    gaps, fs, provs = data.gaps(), store.findings(), data.providers()
    if tranche_id:
        gaps = {g: v for g, v in gaps.items() if v.get("tranche_id") == tranche_id}
    accepted = {r["gap_id"] for r in store.records()}
    buckets = {g: _bucket(v, fs.get(g)) for g, v in gaps.items()}

    by_measure = []
    for m in MEASURES:
        ids = [g for g, v in gaps.items() if v["measure"] == m]
        c = {k: sum(buckets[g] == k for g in ids) for k in ("evidence", "abstain", "none", "no_chart")}
        by_measure.append({"measure": m, "total": len(ids), "accepted": sum(g in accepted for g in ids), **c})

    def yield_of(ids):
        charted = [g for g in ids if buckets[g] in ("evidence", "abstain", "none")]
        found = sum(buckets[g] == "evidence" for g in charted)
        return found, len(charted)

    by_provider = []
    for pid, p in provs.items():
        ids = [g for g, v in gaps.items() if v["provider_id"] == pid]
        found, charted = yield_of(ids)
        by_provider.append({"provider": p, "gaps": len(ids), "charted": charted, "found": found,
                            "abstain": sum(buckets[g] == "abstain" for g in ids),
                            "share": found / charted if charted else 0.0,
                            "share_lb": evaluation.lower_bound_95(found, charted) or 0.0,
                            "accepted": sum(g in accepted for g in ids)})
    # Rank by the 95% lower bound, not the raw share: 4 of 6 is weaker evidence than 15 of 26.
    by_provider.sort(key=lambda r: (-r["share_lb"], -r["found"]))

    measure_yield = {m: yield_of([g for g, v in gaps.items() if v["measure"] == m]) for m in MEASURES}
    provider_yield = {r["provider"]["provider_id"]: (r["found"], r["charted"]) for r in by_provider}
    all_found, all_charted = yield_of(list(gaps))
    overall = all_found / all_charted if all_charted else 0.0
    days_left = (SUPPLEMENTAL_CUTOFF - TODAY).days
    urgency = max(0.0, min(1.0, 1 - days_left / 180))
    chase = []
    for v in gaps.values():
        if v["document_ids"]:
            continue
        pf, pc = provider_yield[v["provider_id"]]
        mf, mc = measure_yield[v["measure"]]
        # Provider yield is shrunk toward the overall rate (PRIOR_CHARTS pseudo-charts), so a provider
        # with few charts can't top the list on a lucky ratio.
        comp = {"provider_yield": (pf + PRIOR_CHARTS * overall) / (pc + PRIOR_CHARTS),
                "measure_yield": mf / mc if mc else 0.0, "urgency": urgency}
        chase.append({"gap": v, "member": data.members()[v["member_id"]], "provider": provs[v["provider_id"]],
                      "components": comp, "score": sum(CHASE_WEIGHTS[k] * comp[k] for k in comp)})
    chase.sort(key=lambda r: (-r["score"], r["gap"]["gap_id"]))
    # Lines of business the gap engine skips: members exist, gaps don't. Visible on purpose.
    tr = data.tranches()
    excluded = {}
    for m in data.members().values():
        t = tr.get(m.get("tranche_id") or "")
        lob = tr.get(t["parent_id"]) if t and t.get("parent_id") else t
        reason = (t or {}).get("excluded_reason") or (lob or {}).get("excluded_reason")
        if reason:
            key = (lob or t)["name"]
            excluded.setdefault(key, {"name": key, "reason": reason, "members": 0})["members"] += 1
    # Yield by provider-group tranche (all tranches, whatever the page filter), ranked by the lower bound.
    by_tranche = []
    all_gaps = data.gaps()
    all_buckets = {g: _bucket(v, fs.get(g)) for g, v in all_gaps.items()}
    for tid, t in tr.items():
        if t["kind"] == "line_of_business":
            continue
        ids = [g for g, v in all_gaps.items() if v.get("tranche_id") == tid]
        charted = [g for g in ids if all_buckets[g] in ("evidence", "abstain", "none")]
        found = sum(all_buckets[g] == "evidence" for g in charted)
        by_tranche.append({"tranche": t, "lob": (tr.get(t["parent_id"]) or {}).get("name") if t.get("parent_id") else None,
                           "gaps": len(ids), "charted": len(charted), "found": found,
                           "abstain": sum(all_buckets[g] == "abstain" for g in ids),
                           "no_chart": sum(all_buckets[g] == "no_chart" for g in ids),
                           "share": found / len(charted) if charted else 0.0,
                           "share_lb": evaluation.lower_bound_95(found, len(charted)) or 0.0,
                           "accepted": sum(g in accepted for g in ids)})
    by_tranche.sort(key=lambda r: (-r["share_lb"], -r["found"]))
    return {"by_measure": by_measure, "by_provider": by_provider, "chase": chase, "by_tranche": by_tranche,
            "cutoff": SUPPLEMENTAL_CUTOFF, "days_left": days_left, "weights": CHASE_WEIGHTS,
            "prior": PRIOR_CHARTS, "overall_yield": overall, "excluded": list(excluded.values())}


STATUS_ORDER = {"active": 0, "planned": 1, "registry_only": 2}


@app.get("/experiments", response_class=HTMLResponse)
def experiments_page(request: Request):
    return templates.TemplateResponse(request, "experiments.html", ctx(
        request, page="experiments", arms=experiments.surface_metrics(), extraction=experiments.extraction_metrics(),
        variants=store.variants()))


@app.post("/experiments/arm")
def set_arm(request: Request, arm: str = Form("")):
    """Override: put this browser in a named arm (empty clears it). The exposure log marks overrides."""
    resp = RedirectResponse("/experiments", status_code=303)
    if arm in experiments.SURFACES:
        resp.set_cookie(experiments.OVERRIDE_COOKIE, arm, max_age=60 * 60 * 24 * 30, samesite="lax")
    else:
        resp.delete_cookie(experiments.OVERRIDE_COOKIE)
    return resp


@app.get("/measures", response_class=HTMLResponse)
def measures_page(request: Request):
    rows = sorted(registry.all_measures().values(), key=lambda m: (STATUS_ORDER[m["status"]], -m["star_weight"], m["code"]))
    return templates.TemplateResponse(request, "measures.html", ctx(request, page="measures", measures_all=rows))


@app.get("/insights", response_class=HTMLResponse)
def insights(request: Request, tranche: str = ""):
    choices = _tranche_choices()
    if tranche and tranche not in choices:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "insights.html", ctx(
        request, page="insights", tranche=tranche, tranches=choices, **insight_data(tranche or None)))
