"""Scenario library additions for v2: TRC (four indicators) and KED.

Same contract as the v1 library (scripts/generate_data.py): each scenario takes a
context (member, provider, note date, pronouns), contributes lines to the shared
progress note and extra documents, and states its own expected finding. Two
extensions the new measures need:

- `indicator`: TRC gaps carry one of NIA, RDI, PED, MRP.
- `evidence` may be a list of (doc_ref, text) pairs when a finding rests on facts
  in different places (an eGFR line and a uACR line; a discharge date and a
  reconciliation note). doc_ref is "note" or an index into extra_docs.

Everything is invented. The hospital, the pharmacist, and the lab are made up.
"""

from datetime import date, timedelta

import generate_data as gd

MY = gd.MY
rng = gd.rng  # the shared, reseeded-per-chart stream
S = gd.S

FACILITY_HOSP = gd.FACILITY_ED  # "Mercy Vale Regional Medical Center", an invented acute hospital
PHARMACISTS = ["R. Adeyemi, PharmD", "L. Castellanos, PharmD", "M. Oyelaran, PharmD"]
NURSES = ["K. Brightwater, RN", "J. Thistlewood, RN", "A. Dunmore, RN"]
MAS = ["T. Pemberton, MA", "S. Ashdown, CMA"]
ADMIT_DX = ["community-acquired pneumonia", "COPD exacerbation", "acute decompensated heart failure",
            "cellulitis of the left lower leg", "urinary tract infection with sepsis, resolved", "syncope, workup negative"]


# ---------------------------------------------------------------------------
# Shared documents
# ---------------------------------------------------------------------------

def discharge_summary(c, adm: date, dc: date, dx: str) -> tuple:
    text = "\n".join([
        "DISCHARGE SUMMARY",
        f"Facility: {FACILITY_HOSP}",
        f"Patient: {c['m']['name']}    Member ID: {c['m']['member_id']}",
        f"Admitted: {gd.us(adm)}    Discharged: {gd.us(dc)}    Disposition: home",
        "", f"Principal diagnosis: {dx}.",
        "Hospital course: treated and improved; stable for discharge.",
        "Discharge medications: see attached reconciliation list (3 changes from home medications).",
        f"Follow-up: PCP within 7 days. Summary faxed to PCP office {gd.us(dc)}.",
    ]) + "\n"
    return ("discharge_summary", dc, text)


def lab_fax_multi(member: dict, d: date, rows: list) -> str:
    """A faxed lab report with several result rows (rows: (test, value, units, ref, flag))."""
    sent = d + timedelta(days=rng.randint(1, 3))
    lines = [
        f"*** FAX ***   FROM: {gd.FACILITY_LAB}   TO: PCP OFFICE   {gd.us(sent)} 07:{rng.randint(10, 59)}   PAGE 1 OF 1",
        "", "        LABORATORY REPORT - FINAL",
        f"PATIENT: {member['name'].upper()}        DOB: {gd.us(date.fromisoformat(member['dob']))}",
        f"MEMBER ID: {member['member_id']}",
        f"COLLECTED: {gd.us(d)}    RECEIVED: {gd.us(d)}    REPORTED: {gd.us(sent)}",
        "", "TEST                         RESULT     UNITS    REF RANGE   FLAG",
    ]
    for test, value, units, ref, flag in rows:
        lines.append(f"{test:<28} {value:<10} {units:<8} {ref:<11} {flag}")
    lines += ["", "Results verified by: M. Ostrowski, MLS(ASCP)", "CONFIDENTIAL - intended only for the addressee"]
    return "\n".join(lines) + "\n"


def _stay(c, max_days_before_note=28):
    """An inpatient stay that ended before the note: (admission, discharge, dx)."""
    dc = c["d"] - timedelta(days=rng.randint(2, max_days_before_note))
    adm = dc - timedelta(days=rng.randint(2, 6))
    return adm, dc, rng.choice(ADMIT_DX)


def _trc(category, finding, indicator, **kw):
    s = S(category, finding, **kw)
    s.indicator = indicator
    return s


# ---------------------------------------------------------------------------
# TRC: Notification of Inpatient Admission (NIA), window: admission day or the next day
# ---------------------------------------------------------------------------

def trc_nia_pos(c):
    adm, dc, dx = _stay(c)
    note_day = adm + timedelta(days=rng.randint(0, 1))
    ev = f"Admission notification received from {FACILITY_HOSP} on {gd.us(note_day)} for admission on {gd.us(adm)}"
    return _trc("positive", "numerator", "NIA", evidence=ev,
                hpi=[f"Hospital follow-up. {ev}; discharged {gd.us(dc)} after {dx}."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Post-hospital review completed."],
                fields={"admission_date": adm.isoformat(), "notification_date": note_day.isoformat()})


def trc_nia_trap_late(c):
    adm, dc, dx = _stay(c)
    note_day = adm + timedelta(days=rng.randint(3, 6))
    ev = f"Admission notification received on {gd.us(note_day)} for admission on {gd.us(adm)}"
    return _trc("trap", "abstain", "NIA", reason="outside_lookback_window", evidence=ev,
                hpi=[f"Hospital follow-up. {ev}; discharged {gd.us(dc)} after {dx}."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Post-hospital review completed."],
                note="notification must land on the admission day or the next day")


def trc_nia_neg(c):
    adm, dc, dx = _stay(c)
    return _trc("negative", "none", "NIA",
                hpi=[f"Hospital follow-up after admission for {dx}, discharged {gd.us(dc)}. Patient reports the stay; "
                     "no notification from the hospital is on file."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Post-hospital review completed."],
                note="no notification documented; the discharge summary alone is not a notification of admission")


# ---------------------------------------------------------------------------
# TRC: Receipt of Discharge Information (RDI), window: discharge day or the next day
# ---------------------------------------------------------------------------

def trc_rdi_pos(c):
    adm, dc, dx = _stay(c)
    rcv = dc + timedelta(days=rng.randint(0, 1))
    ev = f"Discharge summary received {gd.us(rcv)} (discharged {gd.us(dc)})"
    return _trc("positive", "numerator", "RDI", evidence=ev,
                hpi=[f"Hospital follow-up for {dx}. {ev}; reviewed with the patient."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Post-hospital review completed."],
                fields={"discharge_date": dc.isoformat(), "receipt_date": rcv.isoformat()})


def trc_rdi_trap_late(c):
    adm, dc, dx = _stay(c, 40)
    rcv = dc + timedelta(days=rng.randint(4, 12))
    ev = f"Discharge summary received {gd.us(rcv)} (discharged {gd.us(dc)})"
    return _trc("trap", "abstain", "RDI", reason="outside_lookback_window", evidence=ev,
                hpi=[f"Hospital follow-up for {dx}. {ev}."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Post-hospital review completed."],
                note="discharge information must arrive on the discharge day or the next day")


def trc_rdi_trap_undated(c):
    adm, dc, dx = _stay(c)
    ev = "Discharge paperwork brought in by the patient"
    return _trc("trap", "abstain", "RDI", reason="missing_or_ambiguous_date", evidence=ev,
                hpi=[f"Hospital follow-up for {dx}, discharged {gd.us(dc)}. {ev}; no transmission date documented."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Post-hospital review completed."])


def trc_rdi_neg(c):
    adm, dc, dx = _stay(c)
    return _trc("negative", "none", "RDI",
                hpi=[f"Hospital follow-up for {dx}, discharged {gd.us(dc)}. Records requested from the hospital; "
                     "not yet received."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Will review discharge information when received."])


# ---------------------------------------------------------------------------
# TRC: Patient Engagement After Inpatient Discharge (PED), window: days 1-30 after discharge
# ---------------------------------------------------------------------------

def trc_ped_pos_office(c):
    adm, dc, dx = _stay(c, 29)
    ev = f"Post-discharge office visit today, {(c['d'] - dc).days} days after discharge on {gd.us(dc)}"
    return _trc("positive", "numerator", "PED", evidence=ev,
                hpi=[f"{ev}, for {dx}."], extra_docs=[discharge_summary(c, adm, dc, dx)],
                ap=["Recovering as expected."],
                fields={"discharge_date": dc.isoformat(), "engagement_date": c["d"].isoformat(),
                        "encounter_type": "office_visit"})


def trc_ped_pos_telephone(c):
    adm, dc, dx = _stay(c, 29)
    ev = f"Telephone visit with the patient today, {(c['d'] - dc).days} days after discharge on {gd.us(dc)}"
    return _trc("positive", "numerator", "PED", evidence=ev, visit_type="Telephone visit",
                hpi=[f"{ev}; symptoms of {dx} improving, questions answered."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Office visit in 2 weeks."],
                fields={"discharge_date": dc.isoformat(), "engagement_date": c["d"].isoformat(),
                        "encounter_type": "telephone_visit"})


def trc_ped_trap_outreach_only(c):
    adm, dc, dx = _stay(c, 20)
    ev = f"Outreach call attempted {gd.us(dc + timedelta(days=3))}; voicemail left, no contact"
    return _trc("trap", "abstain", "PED", reason="not_completed", evidence=ev,
                visit_type="Chart review (no patient contact)",
                hpi=[f"Chart review after discharge on {gd.us(dc)} for {dx}. {ev}."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], ap=["Second outreach scheduled."],
                note="an attempted contact is not patient engagement; this note records no encounter")


def trc_ped_trap_same_day(c):
    dc = c["d"]
    adm = dc - timedelta(days=rng.randint(2, 6))
    dx = rng.choice(ADMIT_DX)
    ev = f"Seen in the office the day of discharge, {gd.us(dc)}"
    return _trc("trap", "abstain", "PED", reason="outside_lookback_window", evidence=ev,
                hpi=[f"{ev}, for {dx}."], extra_docs=[discharge_summary(c, adm, dc, dx)],
                ap=["Return in one week."], note="engagement on the discharge date itself does not count")


def trc_ped_trap_late(c):
    dc = c["d"] - timedelta(days=rng.randint(32, 45))
    adm = dc - timedelta(days=rng.randint(2, 6))
    dx = rng.choice(ADMIT_DX)
    ev = f"First visit since discharge on {gd.us(dc)}"
    return _trc("trap", "abstain", "PED", reason="outside_lookback_window", evidence=ev,
                hpi=[f"{ev} ({(c['d'] - dc).days} days), for {dx}."], extra_docs=[discharge_summary(c, adm, dc, dx)],
                ap=["Recovering."], note="first engagement after the 30-day window")


def trc_ped_neg(c):
    adm, dc, dx = _stay(c)
    return _trc("negative", "none", "PED",
                hpi=[f"Medication refill request processed by portal message; patient was discharged {gd.us(dc)} after {dx} "
                     "and has not been seen since."],
                extra_docs=[discharge_summary(c, adm, dc, dx)], visit_type="Portal message (asynchronous)",
                ap=["Refills sent. Offered appointment; patient declined for now."],
                note="a portal refill message is not patient engagement")


# ---------------------------------------------------------------------------
# TRC: Medication Reconciliation Post-Discharge (MRP), window: discharge day through day 30,
# by a prescribing practitioner, clinical pharmacist, or RN
# ---------------------------------------------------------------------------

def _mrp(c, author: str, role: str, days_after: int, category="positive", finding="numerator", reason=None, note=""):
    dc = c["d"] - timedelta(days=days_after)
    adm = dc - timedelta(days=rng.randint(2, 6))
    dx = rng.choice(ADMIT_DX)
    ev1 = f"discharged {gd.us(dc)}"
    ev2 = f"Discharge medication list reconciled against current medications on {gd.us(c['d'])} by {author}"
    return _trc(category, finding, "MRP", reason=reason, evidence=[("note", ev1), ("note", ev2)],
                hpi=[f"Hospital follow-up for {dx}, {ev1}."],
                ap=[f"{ev2}: lisinopril dose increased per discharge list, furosemide added, omeprazole stopped."],
                extra_docs=[discharge_summary(c, adm, dc, dx)],
                fields={"discharge_date": dc.isoformat(), "reconciliation_date": c["d"].isoformat(), "author_role": role},
                note=note)


def trc_mrp_pos_md(c):
    return _mrp(c, c["p"]["name"], "prescribing_practitioner", rng.randint(2, 28))


def trc_mrp_pos_pharmacist(c):
    return _mrp(c, rng.choice(PHARMACISTS), "clinical_pharmacist", rng.randint(2, 28))


def trc_mrp_pos_rn(c):
    return _mrp(c, rng.choice(NURSES), "registered_nurse", rng.randint(2, 28))


def trc_mrp_trap_ma(c):
    s = _mrp(c, rng.choice(MAS), "other", rng.randint(2, 28), category="trap", finding="abstain",
             reason="ineligible_author", note="a medical assistant is not an eligible reconciler")
    s.fields = {}
    return s


def trc_mrp_trap_late(c):
    s = _mrp(c, c["p"]["name"], "prescribing_practitioner", rng.randint(32, 45), category="trap", finding="abstain",
             reason="outside_lookback_window", note="reconciliation after day 30")
    s.fields = {}
    return s


def trc_mrp_trap_reviewed_only(c):
    adm, dc, dx = _stay(c)
    ev = "Medications reviewed with patient"
    return _trc("trap", "abstain", "MRP", reason="incomplete_evidence", evidence=ev,
                hpi=[f"Hospital follow-up for {dx}, discharged {gd.us(dc)}."],
                ap=[f"{ev}; continue current regimen."], extra_docs=[discharge_summary(c, adm, dc, dx)],
                note="a medication review without reconciliation against the discharge list is not med rec")


def trc_mrp_neg(c):
    adm, dc, dx = _stay(c)
    return _trc("negative", "none", "MRP",
                hpi=[f"Hospital follow-up for {dx}, discharged {gd.us(dc)}. Discharge list not available at this visit."],
                ap=["Will reconcile medications when the discharge list arrives."],
                extra_docs=[discharge_summary(c, adm, dc, dx)])


def trc_excl_hospice(c):
    adm, dc, dx = _stay(c)
    # Enrollment is on or after discharge and never after the note that records it (a note cannot
    # report a future enrollment; the model rightly abstains on one that does).
    hd = dc + timedelta(days=rng.randint(0, min(3, (c["d"] - dc).days)))
    ev = f"enrolled with hospice on {gd.us(hd)}"
    s = _trc("exclusion", "exclusion", rng.choice(["NIA", "RDI", "PED", "MRP"]), evidence=ev,
             hpi=[f"Hospital follow-up for {dx}, discharged {gd.us(dc)}; patient {ev}."],
             ap=["Goals of care per hospice plan."], extra_docs=[discharge_summary(c, adm, dc, dx)],
             fields={"exclusion_type": "hospice", "supporting_date": hd.isoformat()})
    return s


# ---------------------------------------------------------------------------
# KED: eGFR and uACR both resulted in the measurement year
# ---------------------------------------------------------------------------

def _egfr():
    return rng.randint(38, 95)


def _uacr():
    return rng.choice([8, 12, 18, 25, 34, 61, 140, 320])


def ked_pos_fax(c):
    d = c["d"] - timedelta(days=rng.randint(5, 40))
    e, u = _egfr(), _uacr()
    fax = ("lab_report", d, lab_fax_multi(c["m"], d, [
        ("CREATININE", f"{rng.choice([0.9, 1.1, 1.3, 1.6])}", "mg/dL", "0.6-1.3", ""),
        ("EGFR (CKD-EPI 2021)", f"{e}", "mL/min", ">60", "L" if e < 60 else ""),
        ("UALB/CREAT RATIO", f"{u}", "mg/g", "<30", "H" if u >= 30 else ""),
    ]))
    return S("positive", "numerator", evidence=[(0, f"{'EGFR (CKD-EPI 2021)':<28} {e}"), (0, f"{'UALB/CREAT RATIO':<28} {u}")],
             extra_docs=[fax], ap=["Diabetes: annual kidney labs reviewed; continue metformin."],
             fields={"egfr_value": e, "egfr_date": d.isoformat(), "uacr_value": u, "uacr_date": d.isoformat()})


def ked_pos_note(c):
    d = c["d"] - timedelta(days=rng.randint(2, 14))
    e, u = _egfr(), _uacr()
    ev = f"Labs {gd.us(d)}: eGFR {e}, urine albumin/creatinine ratio {u} mg/g"
    return S("positive", "numerator", evidence=ev, results=[f"{ev}; LDL 92."],
             ap=["Diabetes: kidney evaluation complete for the year; continue metformin."],
             fields={"egfr_value": e, "egfr_date": d.isoformat(), "uacr_value": u, "uacr_date": d.isoformat()})


def ked_pos_split(c):
    """eGFR on one date in a fax; uACR months later in the note. Both in the year; both needed."""
    d1 = max(c["d"] - timedelta(days=rng.randint(90, 150)), date(MY, 1, 5))  # both results in the measurement year
    d2 = c["d"] - timedelta(days=rng.randint(2, 10))
    e, u = _egfr(), _uacr()
    fax = ("lab_report", d1, lab_fax_multi(c["m"], d1, [("EGFR (CKD-EPI 2021)", f"{e}", "mL/min", ">60", "")]))
    ev2 = f"uACR {gd.us(d2)}: {u} mg/g"
    return S("positive", "numerator", evidence=[(0, f"{'EGFR (CKD-EPI 2021)':<28} {e}"), ("note", ev2)],
             results=[f"{ev2}."], extra_docs=[fax], ap=["Diabetes: both kidney markers now on file this year."],
             fields={"egfr_value": e, "egfr_date": d1.isoformat(), "uacr_value": u, "uacr_date": d2.isoformat()},
             note="the two results come from different documents and dates; both are in the year")


def ked_trap_egfr_only(c):
    d = c["d"] - timedelta(days=rng.randint(5, 40))
    e = _egfr()
    fax = ("lab_report", d, lab_fax_multi(c["m"], d, [("CREATININE", "1.0", "mg/dL", "0.6-1.3", ""),
                                                       ("EGFR (CKD-EPI 2021)", f"{e}", "mL/min", ">60", "")]))
    return S("trap", "abstain", reason="incomplete_evidence", evidence=(0, f"{'EGFR (CKD-EPI 2021)':<28} {e}"),
             extra_docs=[fax], ap=["Diabetes: urine albumin not yet done this year; ordered."],
             note="eGFR without a uACR is half the evaluation")


def ked_trap_dipstick(c):
    d = c["d"] - timedelta(days=rng.randint(5, 30))
    e = _egfr()
    ev = f"eGFR {e} ({gd.us(d)}); urine dipstick protein negative"
    return S("trap", "abstain", reason="incomplete_evidence", evidence=ev, results=[f"{ev}."],
             ap=["Diabetes: quantitative urine albumin/creatinine ordered."],
             note="a dipstick protein is not a quantitative uACR")


def ked_trap_prior_year(c):
    d = date(MY - 1, rng.randint(9, 12), rng.randint(1, 28))
    e, u = _egfr(), _uacr()
    ev = f"Last kidney labs {gd.us(d)}: eGFR {e}, uACR {u} mg/g"
    return S("trap", "abstain", reason="outside_lookback_window", evidence=ev, results=[f"{ev}."],
             ap=["Diabetes: repeat kidney labs due; ordered today."])


def ked_trap_undated(c):
    e = _egfr()
    ev = f"eGFR {e} and uACR normal per outside records"
    return S("trap", "abstain", reason="missing_or_ambiguous_date", evidence=ev,
             hpi=[f"{ev}; records not yet received."], ap=["Request outside labs."])


def ked_trap_ordered(c):
    ev = "eGFR and urine albumin/creatinine ratio ordered today"
    return S("trap", "abstain", reason="not_completed", evidence=ev, ap=[f"Diabetes: {ev}; results pending."])


def ked_neg_none(c):
    return S("negative", "none", ap=["Diabetes: continue metformin; foot exam normal.", rng.choice(gd.AP_FILLER)])


def ked_excl_esrd(c):
    start = date(rng.choice([2023, 2024, 2025]), rng.randint(1, 12), 1)
    ev = f"ESRD on hemodialysis three times weekly since {start.month:02d}/{start.year}"
    return S("exclusion", "exclusion", evidence=ev, ap=[f"{ev}; nephrology manages labs."],
             fields={"exclusion_type": "esrd_dialysis", "supporting_date": f"{start.year}-{start.month:02d}"})


def ked_excl_hospice(c):
    hd = max(c["d"] - timedelta(days=rng.randint(10, 90)), date(MY, 1, 1))
    ev = f"on hospice since {gd.us(hd)}"
    return S("exclusion", "exclusion", evidence=ev, hpi=[f"Patient {ev} for metastatic disease."],
             ap=["Comfort-focused care; no further diabetes monitoring labs."],
             fields={"exclusion_type": "hospice", "supporting_date": hd.isoformat()})


# (measure, fn, count weight, needs Monday, pairable) in the v1 plan's shape, plus indicator for TRC via S.indicator.
PLAN_V2 = [
    ("TRC", trc_nia_pos, 3, False, False), ("TRC", trc_nia_trap_late, 2, False, False), ("TRC", trc_nia_neg, 2, False, False),
    ("TRC", trc_rdi_pos, 3, False, False), ("TRC", trc_rdi_trap_late, 1, False, False),
    ("TRC", trc_rdi_trap_undated, 1, False, False), ("TRC", trc_rdi_neg, 2, False, False),
    ("TRC", trc_ped_pos_office, 2, False, False), ("TRC", trc_ped_pos_telephone, 1, False, False),
    ("TRC", trc_ped_trap_outreach_only, 1, False, False), ("TRC", trc_ped_trap_same_day, 1, False, False),
    ("TRC", trc_ped_trap_late, 1, False, False), ("TRC", trc_ped_neg, 2, False, False),
    ("TRC", trc_mrp_pos_md, 2, False, False), ("TRC", trc_mrp_pos_pharmacist, 1, False, False),
    ("TRC", trc_mrp_pos_rn, 1, False, False), ("TRC", trc_mrp_trap_ma, 1, False, False),
    ("TRC", trc_mrp_trap_late, 1, False, False), ("TRC", trc_mrp_trap_reviewed_only, 1, False, False),
    ("TRC", trc_mrp_neg, 2, False, False), ("TRC", trc_excl_hospice, 1, False, False),
    ("KED", ked_pos_fax, 2, False, True), ("KED", ked_pos_note, 2, False, True), ("KED", ked_pos_split, 1, False, True),
    ("KED", ked_trap_egfr_only, 1, False, True), ("KED", ked_trap_dipstick, 1, False, True),
    ("KED", ked_trap_prior_year, 1, False, True), ("KED", ked_trap_undated, 1, False, True),
    ("KED", ked_trap_ordered, 1, False, True), ("KED", ked_neg_none, 2, False, True),
    ("KED", ked_excl_esrd, 1, False, False), ("KED", ked_excl_hospice, 1, False, False),
]
