"""The service seam: the operations every surface goes through.

The review-queue web app is one surface on these functions. An MCP tool, a
Claude skill, or a batch job would call the same ones, so a decision made
anywhere carries the same signatures (reviewer, model version, prompt
version, source span) and passes the same checks.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from . import data, extractor, registry, rules, store

EXCLUSION_FIELDS = ["exclusion_type", "supporting_date"]
ACTIONS = ("accept", "reject", "needs_more")
LEASE_MINUTES = 15
# QA sampling: accepted findings are re-reviewed at this rate (deterministic per gap so a run is
# reproducible); every rejection of a proposed finding is sampled, because a confirmed rejection is a
# catch and an unconfirmed one is the only way to find out the nurse was wrong.
QA_ACCEPT_SAMPLE_RATE = 0.2


class NothingToAccept(ValueError):
    """Raised when Accept is attempted on anything but a verified numerator or exclusion."""


class AlreadyDecided(ValueError):
    """Raised when a gap already has a record; a closed review takes no further decisions."""


class SelfAudit(ValueError):
    """Raised when a reviewer tries to audit their own decision."""


class Locked(ValueError):
    """Raised when another reviewer holds the lease on the gap."""

    def __init__(self, reviewer: str, until: str):
        super().__init__(f"Under review by {reviewer} until {until}")
        self.reviewer, self.until = reviewer, until


def find_evidence(gap_id: str) -> dict:
    """Read the gap's chart with the model and return a checked finding (numerator,
    exclusion, abstain with reason, or none). Proposes only; never closes a gap."""
    return extractor.extract(gap_id)


def check_rules(gap_id: str, finding: dict) -> dict:
    """Run the deterministic checks on a finding: quote verbatim in source, values in
    quote, dates in window, thresholds. Failures downgrade it, with the failed checks listed."""
    gap = data.gaps()[gap_id]
    docs = {d: data.documents()[d] for d in gap["document_ids"]}
    texts = {d: data.document_text(d) for d in gap["document_ids"]}
    return rules.apply(registry.spec_for_gap(gap), finding, data.members()[gap["member_id"]], docs, texts)


def claim(gap_id: str, reviewer: str, minutes: int = LEASE_MINUTES) -> dict:
    """Take or renew the lease on a gap for one reviewer. Raises Locked if someone else holds it."""
    current = store.active_assignment(gap_id)
    if current and current["reviewer"] != reviewer:
        raise Locked(current["reviewer"], current["lease_expires_at"])
    expires = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with store.tx() as conn:
        store.release_assignments(gap_id, conn=conn)
        store.add_assignment(gap_id, reviewer, expires, conn=conn)
    return {"gap_id": gap_id, "reviewer": reviewer, "lease_expires_at": expires}


def release(gap_id: str) -> None:
    store.release_assignments(gap_id)


def record_decision(gap_id: str, action: str, reviewer: str, reason=None, note=None, surface=None):
    """Record a person's decision on a gap's finding. Accept is the only path that
    creates a submittable record, and only for a verified numerator or exclusion.
    Refuses if another reviewer holds the lease or the gap already has a record.
    Returns the record for Accept, otherwise None."""
    gap = data.gaps()[gap_id]
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    held = store.active_assignment(gap_id)
    if held and held["reviewer"] != reviewer:
        raise Locked(held["reviewer"], held["lease_expires_at"])
    if store.records(gap_id):
        raise AlreadyDecided(f"{gap_id} already has a record; a closed review takes no further decisions.")
    finding = store.finding(gap_id)
    variant_id = (finding or {}).get("variant_id")  # the extraction variant behind the finding decided on

    if action != "accept":
        with store.tx() as conn:
            decision_id = store.add_decision(gap_id, action, reviewer, reason=reason, note=note, conn=conn,
                                             variant_id=variant_id, surface=surface)
            if action == "reject" and (finding or {}).get("finding_type") in ("numerator", "exclusion"):
                store.add_qa_audit(decision_id, gap_id, conn=conn)
            store.release_assignments(gap_id, conn=conn)
        return None

    if (not finding or finding["finding_type"] not in ("numerator", "exclusion")
            or not finding.get("evidence_spans")):
        raise NothingToAccept("Nothing to accept: Attest did not propose verifiable evidence for this gap.")
    spec = registry.spec_for_gap(gap)
    numerator = finding["finding_type"] == "numerator"
    keep = spec["fields"] if numerator else EXCLUSION_FIELDS
    spans = finding["evidence_spans"]
    first = spans[0]
    call = finding.get("call") or {}
    record = {
        "record_id": f"REC-{uuid.uuid4().hex[:10].upper()}",
        "gap_id": gap_id, "member_id": gap["member_id"], "measure": gap["measure"],
        "indicator": gap.get("indicator"), "tranche_id": gap.get("tranche_id"),
        "finding_type": finding["finding_type"],
        "fields": {k: finding["fields"].get(k) for k in keep},
        "submission_channel": spec["submission_channel"] if numerator else "exclusion",
        # First span kept in the v1 shape; every span listed with its text.
        "source_document_id": first["doc_id"],
        "source_span": {"start": first["start"], "end": first["end"]},
        "source_text": data.document_text(first["doc_id"])[first["start"]:first["end"]],
        "source_spans": [{"doc_id": s["doc_id"], "start": s["start"], "end": s["end"],
                          "text": data.document_text(s["doc_id"])[s["start"]:s["end"]]} for s in spans],
        "reviewer": reviewer, "created_at": store.now(),
        "model_version": call.get("model_served"), "prompt_version": call.get("prompt_version"),
        "model_confidence": finding.get("confidence"),
        "variant_id": variant_id, "surface": surface,
        "schema_version": store.SCHEMA_VERSION,
    }
    # One transaction: the decision, the record, and the lease release land together or not at all.
    with store.tx() as conn:
        decision_id = store.add_decision(gap_id, "accept", reviewer, note=note, conn=conn,
                                         variant_id=variant_id, surface=surface)
        store.add_record(record, decision_id, conn=conn)
        if qa_sampled(gap_id):
            store.add_qa_audit(decision_id, gap_id, conn=conn)
        store.release_assignments(gap_id, conn=conn)
    return record


def qa_sampled(gap_id: str, rate: float = QA_ACCEPT_SAMPLE_RATE) -> bool:
    """Whether an accept on this gap goes to QA: a stable hash, so the sample is the same on every run."""
    h = int(hashlib.sha1(f"qa:{gap_id}".encode()).hexdigest(), 16) % 1000
    return h < rate * 1000


def record_qa_verdict(audit_id: int, auditor: str, verdict: str, note=None) -> None:
    """A second person's verdict on a sampled decision: upheld or overturned. The original reviewer
    cannot audit their own decision."""
    if verdict not in ("upheld", "overturned"):
        raise ValueError(f"unknown verdict {verdict!r}")
    audit = next((a for a in store.qa_audits() if a["id"] == audit_id), None)
    if audit is None:
        raise KeyError(audit_id)
    if audit["reviewer"] == auditor:
        raise SelfAudit(f"{auditor} made this decision and cannot audit it.")
    if audit["verdict"]:
        raise AlreadyDecided("This audit already has a verdict.")
    store.record_qa_verdict(audit_id, auditor, verdict, note=note)
