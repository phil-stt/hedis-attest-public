import importlib

import pytest
from conftest import _fresh_db


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from attest import seed, service, store
    importlib.reload(store)
    from attest import data
    importlib.reload(data)
    seed.load_snapshot()
    return service, store


def first(store, ftype):
    return next(g for g, f in sorted(store.findings().items()) if f["finding_type"] == ftype)


def test_accept_writes_record_with_signatures(svc):
    service, store = svc
    gap = first(store, "exclusion")
    rec = service.record_decision(gap, "accept", "nurse.b")
    assert rec["finding_type"] == "exclusion" and set(rec["fields"]) == {"exclusion_type", "supporting_date"}
    assert rec["submission_channel"] == "exclusion"
    for key in ("reviewer", "model_version", "prompt_version", "source_document_id", "source_span", "source_text"):
        assert rec[key], key
    assert store.records(gap) == [rec]


def test_numerator_record_routes_to_submission_channel(svc):
    service, store = svc
    cole = next(g for g, f in sorted(store.findings().items())
                if f["finding_type"] == "numerator" and f["call"]["measure"] == "COL-E")
    assert service.record_decision(cole, "accept", "nurse.b")["submission_channel"] == "supplemental data (ECDS)"


def test_abstain_and_none_cannot_be_accepted(svc):
    service, store = svc
    for ftype in ("abstain", "none"):
        with pytest.raises(service.NothingToAccept):
            service.record_decision(first(store, ftype), "accept", "nurse.b")
    assert store.records() == []


def test_check_rules_downgrades_a_tampered_finding(svc):
    service, store = svc
    gap = first(store, "numerator")
    f = dict(store.finding(gap))
    f["evidence"] = [{"doc_id": f["evidence"][0]["doc_id"], "quote": "text that is not in the chart"}]
    out = service.check_rules(gap, f)
    assert out["finding_type"] == "abstain" and out["abstain_reason"] == "evidence_not_verified"
