"""Deterministic checks applied to every model finding before a nurse sees it.

The model reads the chart; code enforces the measure. Checks are organized by
rule family, parameterized from the measure registry. Any numerator or exclusion
finding that fails a check is downgraded (to abstain, or to none when the model's
own values show the gap is not closed) and the failed check is recorded on the
finding, so the reviewer sees why.
"""

import re
from datetime import date, timedelta
from typing import List, Optional, Tuple

from .config import MEASUREMENT_YEAR as MY
from .schema import MAX_EVIDENCE_SPANS, normalize_finding

# Abstain reasons that only the code path produces.
RULE_REASONS = ["evidence_not_verified", "failed_rule_check", "model_refused", "model_error"]

# Exclusions that must fall in the measurement year; the rest may be any time on or before it.
MY_BOUND_EXCLUSIONS = ("hospice", "palliative_care", "advanced_illness_frailty")


def locate(text: str, quote: Optional[str]) -> Optional[Tuple[int, int]]:
    """Character span of quote in text: exact, then whitespace-tolerant, then case-insensitive."""
    if not quote or not quote.strip():
        return None
    i = text.find(quote)
    if i >= 0:
        return i, i + len(quote)
    pattern = r"\s+".join(re.escape(tok) for tok in quote.split())
    for flags in (0, re.IGNORECASE):
        m = re.search(pattern, text, flags)
        if m:
            return m.start(), m.end()
    return None


def parse_date(s: Optional[str]) -> Optional[Tuple[int, Optional[int], Optional[int]]]:
    """Parse YYYY, YYYY-MM, or YYYY-MM-DD. Returns None if malformed."""
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", s.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), m.group(2), m.group(3)
    try:
        if d:
            date(y, int(mo), int(d))
        elif mo and not 1 <= int(mo) <= 12:
            return None
    except ValueError:
        return None
    return y, int(mo) if mo else None, int(d) if d else None


def _not_after(parsed, limit: date) -> bool:
    y, mo, d = parsed
    return (y, mo or 1, d or 1) <= (limit.year, limit.month, limit.day)


def _num_in(quote: str, value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    forms = {str(value)}
    if isinstance(value, float):
        forms |= {f"{value:.1f}", f"{value:g}", f"{value:.0f}" if value.is_integer() else f"{value:.2f}"}
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", quote) for f in forms)


class _Checks:
    def __init__(self):
        self.items: List[dict] = []
        self.failure: Optional[Tuple[str, Optional[str]]] = None  # (finding_type, reason)

    def require(self, ok: bool, rule: str, detail: str, on_fail=("abstain", "failed_rule_check")):
        self.items.append({"rule": rule, "passed": bool(ok), "detail": detail})
        if not ok and self.failure is None:
            self.failure = on_fail
        return ok


# ---------------------------------------------------------------------------
# Shared field checks
# ---------------------------------------------------------------------------

def _exact_date(c: _Checks, label: str, value, doc_date: date, in_my: bool = True):
    """An exact (YYYY-MM-DD) date, in the measurement year, not after the document. Returns parsed or None."""
    p = parse_date(value)
    if not c.require(p is not None and p[2] is not None, f"exact_{label}", f"{label}={value}",
                     on_fail=("abstain", "missing_or_ambiguous_date")):
        return None
    if in_my:
        c.require(p[0] == MY, f"{label}_in_measurement_year", f"{value} in {MY}",
                  on_fail=("abstain", "outside_lookback_window"))
    c.require(_not_after(p, doc_date), f"{label}_not_after_document", f"{label} on or before {doc_date}",
              on_fail=("abstain", "missing_or_ambiguous_date"))
    return p


def _value_in_quotes(c: _Checks, quotes: str, label: str, value):
    c.require(_num_in(quotes, value), f"{label}_in_quote", f"{value} appears in the quoted evidence",
              on_fail=("abstain", "evidence_not_verified"))


# ---------------------------------------------------------------------------
# Rule families
# ---------------------------------------------------------------------------

def _blood_pressure(c, spec, f, quotes, doc_date):
    p = spec["params"]
    s, d = f.get("systolic"), f.get("diastolic")
    if c.require(isinstance(s, int) and isinstance(d, int), "values_present", f"systolic={s}, diastolic={d}",
                 on_fail=("abstain", "missing_value")):
        c.require(_num_in(quotes, s) and _num_in(quotes, d), "values_in_quote",
                  f"{s} and {d} appear in the quoted evidence", on_fail=("abstain", "evidence_not_verified"))
        c.require(s < p["systolic_max"] and d < p["diastolic_max"], "controlled",
                  f"{s}/{d} vs <{p['systolic_max']}/{p['diastolic_max']}", on_fail=("none", None))
    _exact_date(c, "reading_date", f.get("reading_date"), doc_date)
    setting, source = f.get("setting"), f.get("source")
    eligible = {tuple(x) for x in p["eligible_setting_source"]}
    c.require((setting, source) in eligible, "eligible_setting_source", f"setting={setting}, source={source}",
              on_fail=("abstain", "ineligible_setting"))


def _lab_value(c, spec, f, quotes, doc_date):
    p = spec["params"]
    v = f.get(p["value_field"])
    lo, hi = p["plausible_range"]
    if c.require(isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi, "value_present",
                 f"{p['value_field']}={v}", on_fail=("abstain", "missing_value")):
        _value_in_quotes(c, quotes, p["value_field"], v)
    _exact_date(c, p["date_field"], f.get(p["date_field"]), doc_date)


def _paired_labs(c, spec, f, quotes, doc_date):
    """Every component must be present, plausible, quoted, and exactly dated in the measurement year."""
    for comp in spec["params"]["components"]:
        v = f.get(comp["value_field"])
        lo, hi = comp["plausible_range"]
        if c.require(isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi,
                     f"{comp['value_field']}_present", f"{comp['value_field']}={v}",
                     on_fail=("abstain", "incomplete_evidence")):
            _value_in_quotes(c, quotes, comp["value_field"], v)
        _exact_date(c, comp["date_field"], f.get(comp["date_field"]), doc_date)


def _procedure_window(c, spec, f, quotes, doc_date):
    p = spec["params"]
    pt = f.get(p["type_field"])
    years = p["lookback_years"]
    c.require(pt in years, p["type_field"], f"{p['type_field']}={pt}", on_fail=("abstain", "missing_value"))
    parsed = parse_date(f.get(p["date_field"]))
    if c.require(parsed is not None, "dated", f"{p['date_field']}={f.get(p['date_field'])}",
                 on_fail=("abstain", "missing_or_ambiguous_date")) and pt in years:
        lo = MY - years[pt]
        c.require(lo <= parsed[0] <= MY, "in_lookback_window", f"{parsed[0]} within {lo}-{MY} for {pt}",
                  on_fail=("abstain", "outside_lookback_window"))
        c.require(_not_after(parsed, doc_date), "not_after_document", f"procedure on or before {doc_date}",
                  on_fail=("abstain", "missing_or_ambiguous_date"))


def _event_window(c, spec, f, quotes, doc_date):
    """An event dated within [start, end] days of an anchor (admission or discharge), optionally by an
    eligible role or encounter type. The anchor may precede the measurement year (a December discharge)."""
    p = spec["params"]
    anchor = _exact_date(c, p["anchor_field"], f.get(p["anchor_field"]), doc_date, in_my=False)
    event = _exact_date(c, p["event_field"], f.get(p["event_field"]), doc_date, in_my=False)
    if anchor and event:
        a = date(*anchor)
        e = date(*event)
        lo, hi = p["window_days"]
        c.require(a + timedelta(days=lo) <= e <= a + timedelta(days=hi), "within_window",
                  f"{p['event_field']} {e} is {(e - a).days} days after {p['anchor_field']} {a}; window {lo}-{hi}",
                  on_fail=("abstain", "outside_lookback_window"))
        c.require(e.year == MY or a.year == MY, "touches_measurement_year", f"{a} or {e} in {MY}",
                  on_fail=("abstain", "outside_lookback_window"))
    if p.get("category_field"):
        cat = f.get(p["category_field"])
        reason = "ineligible_author" if p["category_field"] == "author_role" else "ineligible_setting"
        c.require(cat in p["eligible_categories"], f"eligible_{p['category_field']}",
                  f"{p['category_field']}={cat}", on_fail=("abstain", reason))


FAMILY_CHECKS = {"blood_pressure": _blood_pressure, "lab_value": _lab_value, "paired_labs": _paired_labs,
                 "procedure_window": _procedure_window, "event_window": _event_window}


def _exclusion(c, spec, f, member):
    et = f.get("exclusion_type")
    c.require(et in spec["exclusions"], "exclusion_applies_to_measure", f"{et} for {spec['key']}")
    if et == "advanced_illness_frailty":
        c.require(member["age_at_my_end"] >= 66, "age_66_plus", f"age {member['age_at_my_end']} at end of {MY}")
    p = parse_date(f.get("supporting_date"))
    if c.require(p is not None, "dated", f"supporting_date={f.get('supporting_date')}",
                 on_fail=("abstain", "missing_or_ambiguous_date")):
        if et in MY_BOUND_EXCLUSIONS:
            c.require(p[0] == MY, "in_measurement_year", f"{f.get('supporting_date')} in {MY}",
                      on_fail=("abstain", "outside_lookback_window"))
        else:
            c.require(p[0] <= MY, "on_or_before_measurement_year", f"{f.get('supporting_date')} <= {MY}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def apply(spec: dict, finding: dict, member: dict, docs: dict, texts: dict) -> dict:
    """Validate a model finding against its measure spec. docs: doc_id -> metadata; texts: doc_id -> text."""
    out = normalize_finding(finding)
    out["fields"] = dict(finding.get("fields") or {})
    ftype = out["finding_type"]
    c = _Checks()

    # Locate every quoted passage; a span is recorded only where the quote is really in the document.
    spans, located_quotes = [], []
    for ev in out["evidence"][:MAX_EVIDENCE_SPANS]:
        doc_id = ev.get("doc_id")
        span = locate(texts[doc_id], ev.get("quote")) if doc_id in texts else None
        if span:
            spans.append({"doc_id": doc_id, "start": span[0], "end": span[1]})
            located_quotes.append(texts[doc_id][span[0]:span[1]])
    out["evidence_spans"] = spans
    out["evidence_span"] = spans[0] if spans else None

    if ftype in ("numerator", "exclusion"):
        c.require(bool(spans) and len(spans) == len(out["evidence"][:MAX_EVIDENCE_SPANS]), "evidence_in_source",
                  "every quoted passage found verbatim in its cited document" if spans and len(spans) == len(out["evidence"])
                  else "a quoted passage was not found in the cited document",
                  on_fail=("abstain", "evidence_not_verified"))
        quotes = "\n".join(located_quotes)
        doc_dates = [date.fromisoformat(docs[s["doc_id"]]["doc_date"]) for s in spans if s["doc_id"] in docs]
        doc_date = max(doc_dates) if doc_dates else date(MY, 12, 31)
        f = out["fields"]
        if ftype == "numerator":
            FAMILY_CHECKS[spec["family"]](c, spec, f, quotes, doc_date)
        else:
            _exclusion(c, spec, f, member)

    if c.failure:
        out["model_finding_type"] = ftype
        out["model_abstain_reason"] = out.get("abstain_reason")
        out["finding_type"], out["abstain_reason"] = c.failure
    elif out["finding_type"] != "abstain":
        out["abstain_reason"] = None
    if out["finding_type"] == "abstain" and not out.get("abstain_reason"):
        out["abstain_reason"] = "failed_rule_check"
    out["rule_checks"] = c.items
    return out
