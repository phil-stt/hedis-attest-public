from attest import data, registry, rules

MEMBER = {"age_at_my_end": 60}
DOCS = {"D1": {"doc_date": "2026-06-10"}}


def finding(ftype, fields, quote, reason=None):
    quotes = quote if isinstance(quote, list) else [quote]
    return {"finding_type": ftype, "abstain_reason": reason, "fields": fields,
            "evidence": [{"doc_id": "D1", "quote": q} for q in quotes], "confidence": 0.9, "rationale": ""}


def run(measure, f, text, indicator=None):
    return rules.apply(registry.spec(measure, indicator), f, MEMBER, DOCS, {"D1": text})


def test_locate_whitespace_tolerant():
    text = "TEST   HEMOGLOBIN A1C        7.8   %"
    assert rules.locate(text, "HEMOGLOBIN A1C 7.8") == (7, 32)
    assert rules.locate(text, "not there") is None


def test_cbp_controlled_passes_and_records_span():
    text = "Vitals: BP 128/76, HR 70"
    out = run("CBP", finding("numerator", {"systolic": 128, "diastolic": 76, "reading_date": "2026-06-10",
                                           "setting": "outpatient", "source": "office"}, "BP 128/76"), text)
    assert out["finding_type"] == "numerator"
    assert out["evidence_span"] == {"doc_id": "D1", "start": 8, "end": 17}


def test_cbp_uncontrolled_becomes_none():
    out = run("CBP", finding("numerator", {"systolic": 146, "diastolic": 88, "reading_date": "2026-06-10",
                                           "setting": "outpatient", "source": "office"}, "BP 146/88"), "BP 146/88")
    assert out["finding_type"] == "none" and out["model_finding_type"] == "numerator"


def test_value_not_in_quote_is_not_verified():
    out = run("CBP", finding("numerator", {"systolic": 120, "diastolic": 76, "reading_date": "2026-06-10",
                                           "setting": "outpatient", "source": "office"}, "BP 128/76"), "BP 128/76")
    assert out["finding_type"] == "abstain" and out["abstain_reason"] == "evidence_not_verified"


def test_hallucinated_quote_is_not_verified():
    out = run("GSD", finding("numerator", {"a1c_value": 7.1, "test_date": "2026-05-01"}, "A1c 7.1"), "no labs today")
    assert out["abstain_reason"] == "evidence_not_verified"


def test_home_reading_requires_digital_source():
    out = run("CBP", finding("numerator", {"systolic": 120, "diastolic": 76, "reading_date": "2026-06-01",
                                           "setting": "home", "source": "office"}, "120/76"), "home 120/76")
    assert out["abstain_reason"] == "ineligible_setting"


def test_gsd_prior_year_is_outside_window():
    out = run("GSD", finding("numerator", {"a1c_value": 7.0, "test_date": "2025-11-18"}, "HbA1c 7.0%"), "HbA1c 7.0%")
    assert out["abstain_reason"] == "outside_lookback_window"


def test_cole_windows():
    ok = run("COL-E", finding("numerator", {"procedure_type": "colonoscopy", "procedure_date": "2019"},
                              "colonoscopy 2019"), "colonoscopy 2019")
    old = run("COL-E", finding("numerator", {"procedure_type": "colonoscopy", "procedure_date": "2015"},
                               "colonoscopy 2015"), "colonoscopy 2015")
    fit = run("COL-E", finding("numerator", {"procedure_type": "fit", "procedure_date": "2025-09-12"},
                               "FIT 2025"), "FIT 2025")
    assert ok["finding_type"] == "numerator"
    assert old["abstain_reason"] == "outside_lookback_window"
    assert fit["abstain_reason"] == "outside_lookback_window"


def test_paired_labs_need_both_components_each_quoted():
    text = "eGFR 62 mL/min/1.73m2 (05/02/2026)\nUrine albumin/creatinine ratio 18 mg/g (05/02/2026)"
    both = finding("numerator", {"egfr_value": 62, "egfr_date": "2026-05-02", "uacr_value": 18, "uacr_date": "2026-05-02"},
                   ["eGFR 62", "ratio 18 mg/g"])
    out = run("KED", both, text)
    assert out["finding_type"] == "numerator" and len(out["evidence_spans"]) == 2
    half = finding("numerator", {"egfr_value": 62, "egfr_date": "2026-05-02", "uacr_value": None, "uacr_date": None},
                   ["eGFR 62"])
    assert run("KED", half, text)["abstain_reason"] == "incomplete_evidence"
    unquoted = finding("numerator", {"egfr_value": 62, "egfr_date": "2026-05-02", "uacr_value": 18, "uacr_date": "2026-05-02"},
                       ["eGFR 62"])
    assert run("KED", unquoted, text)["abstain_reason"] == "evidence_not_verified"


def test_trc_indicator_windows_and_roles():
    text = "Discharged 04/02/2026. Medications reconciled against the discharge list on 04/10/2026 by Dana Okonjo-Lind, MD."
    ok = finding("numerator", {"discharge_date": "2026-04-02", "reconciliation_date": "2026-04-10",
                               "author_role": "prescribing_practitioner"}, ["Discharged 04/02/2026", "reconciled against the discharge list on 04/10/2026"])
    assert run("TRC", ok, text, "MRP")["finding_type"] == "numerator"
    late = finding("numerator", {"discharge_date": "2026-04-02", "reconciliation_date": "2026-05-10",
                                 "author_role": "prescribing_practitioner"}, ["Discharged 04/02/2026"])
    assert run("TRC", late, text, "MRP")["abstain_reason"] == "outside_lookback_window"
    ma = finding("numerator", {"discharge_date": "2026-04-02", "reconciliation_date": "2026-04-10",
                               "author_role": "other"}, ["Discharged 04/02/2026"])
    assert run("TRC", ma, text, "MRP")["abstain_reason"] == "ineligible_author"
    same_day = finding("numerator", {"discharge_date": "2026-04-02", "engagement_date": "2026-04-02",
                                     "encounter_type": "office_visit"}, ["Discharged 04/02/2026"])
    assert run("TRC", same_day, text, "PED")["abstain_reason"] == "outside_lookback_window"


def test_registry_lists_every_measure_with_a_chart_lever_statement():
    ms = registry.all_measures()
    assert len(ms) == 14
    assert {m["chart_lever"] for m in ms.values()} == {"strong", "supplemental", "small", "none"}
    assert [k for k, m in ms.items() if m["chart_lever"] == "none"] == ["PCR", "PDC-DM", "PDC-RAS", "PDC-STA"]
    assert list(registry.all_measures()["TRC"]["indicators"]) == ["NIA", "RDI", "PED", "MRP"]


def test_frailty_requires_66():
    out = run("GSD", finding("exclusion", {"exclusion_type": "advanced_illness_frailty",
                                           "supporting_date": "2026-03-03"}, "uses a walker"), "uses a walker")
    assert out["finding_type"] == "abstain" and out["abstain_reason"] == "failed_rule_check"


def test_labels_are_consistent_with_documents(tmp_path, monkeypatch):
    import importlib

    from conftest import _fresh_db

    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.delenv("ATTEST_DATASET", raising=False)
    from attest import store

    importlib.reload(store)
    importlib.reload(data)
    for split in ("dev", "heldout"):
        for lb in data.labels(split):
            ev = lb["evidence"]
            if ev:
                assert data.document_text(ev["doc_id"])[ev["start"]:ev["end"]] == ev["text"]
            if lb["expected_finding"] in ("numerator", "exclusion"):
                assert lb["expected_fields"] and ev


def test_splits_do_not_share_members():
    dev = {lb["member_id"] for lb in data.labels("dev")}
    ho = {lb["member_id"] for lb in data.labels("heldout")}
    assert not dev & ho
