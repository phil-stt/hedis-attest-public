"""JSON schema for a finding, built from a measure spec. Sent as the structured-output format.

Evidence is a list of spans (one to three verbatim quotes), because some findings
rest on two facts on different lines: an eGFR and a uACR, a discharge date and a
reconciliation note, an advanced illness and frailty.
"""

from typing import Dict

FINDING_TYPES = ["numerator", "exclusion", "abstain", "none"]

ABSTAIN_REASONS = [
    "bp_range_or_threshold",
    "ineligible_setting",
    "home_reading_not_digital",
    "missing_value",
    "missing_or_ambiguous_date",
    "outside_lookback_window",
    "not_completed",
    "incomplete_evidence",   # one half of a required pair (KED), or an event without its anchor (TRC)
    "ineligible_author",     # the right act by the wrong role (TRC med rec by a medical assistant)
]

EXCLUSION_TYPES = ["hospice", "palliative_care", "advanced_illness_frailty", "esrd_dialysis",
                   "total_colectomy", "colorectal_cancer"]

MAX_EVIDENCE_SPANS = 3


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


STR = {"type": "string"}


def finding_schema(spec: dict) -> dict:
    fields: Dict[str, dict] = {name: _nullable(spec["field_schema"][name]) for name in spec["fields"]}
    fields["exclusion_type"] = _nullable({"type": "string", "enum": spec["exclusions"] or EXCLUSION_TYPES})
    fields["supporting_date"] = _nullable(STR)
    return {
        "type": "object",
        "properties": {
            "candidates_considered": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"doc_id": STR, "quote": STR, "assessment": STR},
                    "required": ["doc_id", "quote", "assessment"],
                    "additionalProperties": False,
                },
            },
            "finding_type": {"type": "string", "enum": FINDING_TYPES},
            "abstain_reason": _nullable({"type": "string", "enum": ABSTAIN_REASONS}),
            "fields": {
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            },
            # Verbatim passages the finding rests on; empty for a `none` with nothing to show.
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"doc_id": STR, "quote": STR},
                    "required": ["doc_id", "quote"],
                    "additionalProperties": False,
                },
            },
            "confidence": {"type": "number"},
            "rationale": STR,
        },
        "required": ["candidates_considered", "finding_type", "abstain_reason", "fields",
                     "evidence", "confidence", "rationale"],
        "additionalProperties": False,
    }


def normalize_finding(f: dict) -> dict:
    """One shape for every finding, whatever version wrote it: `evidence` is a list of
    {doc_id, quote}; `evidence_spans` a list of {doc_id, start, end}; `evidence_span` the first span."""
    out = dict(f)
    ev = out.get("evidence")
    if ev is None:
        out["evidence"] = []
    elif isinstance(ev, dict):
        out["evidence"] = [ev] if ev.get("quote") else []
    spans = out.get("evidence_spans")
    if spans is None:
        single = out.get("evidence_span")
        out["evidence_spans"] = [single] if single else []
    out["evidence_span"] = out["evidence_spans"][0] if out["evidence_spans"] else None
    return out
