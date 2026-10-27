"""Generate the synthetic Attest dataset.

Everything here is invented: member names come from small generic name lists,
providers and clinics are made up, and every clinical detail is templated.
No real member, provider, or claims data is used or referenced.

Each gap is built from a hand-written scenario. The scenario writes the chart
text and, in the same place, states the expected finding (the label). Labels
are therefore authored by hand once, alongside the text they describe, and
the generator only checks that the evidence passage actually appears in the
document and records its character offsets.

Run:  python3 scripts/generate_data.py
Output (overwritten): data/members.json, data/gaps.json, data/documents/*.txt,
data/documents.json, data/splits.json, data/labels/{dev,heldout}.json,
data/labels/heldout_review.md

Label files that differ from the generated ones (hand corrections) are not
overwritten unless --force-labels is passed.
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

SEED = 20261005
MY = 2026  # measurement year
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

rng = random.Random(SEED)

# ---------------------------------------------------------------------------
# Synthetic people and places
# ---------------------------------------------------------------------------

FIRST_F = ["Alma", "Bernice", "Carla", "Delia", "Edith", "Fern", "Gloria", "Hazel",
           "Irene", "Joan", "Lena", "Marisol", "Nadine", "Opal", "Paula", "Rosa",
           "Sylvia", "Thea", "Vera", "Wanda", "Yvette", "Zora"]
FIRST_M = ["Abel", "Bruno", "Calvin", "Dmitri", "Elmer", "Floyd", "Gus", "Hector",
           "Ivan", "Jerome", "Kurt", "Lionel", "Milo", "Nestor", "Otis", "Percy",
           "Rufus", "Silas", "Tobias", "Vernon", "Wendell", "Ezra"]
LAST = ["Abernathy", "Bellweather", "Castellano", "Delacroix", "Ellsworth", "Fairbanks",
        "Galloway", "Hargrove", "Ingersoll", "Jablonski", "Kowalczyk", "Lindqvist",
        "Marchetti", "Nakashima-Reyes", "Oyelaran", "Pemberton", "Quintero", "Rasmussen",
        "Szabo", "Thistlewood", "Underhill", "Valdivia", "Whitcombe", "Yarborough",
        "Zielinski", "Ashdown", "Brightwater", "Coldiron", "Dunmore", "Everhart"]

# Made-up providers. Weights skew where evidence-in-chart lands, so the insight
# view has a "documents the care, doesn't code it" cluster to show.
PROVIDERS = [
    {"provider_id": "PRV-01", "name": "Dana Okonjo-Lind, MD", "clinic": "Riverbend Family Medicine", "pos_weight": 3.0},
    {"provider_id": "PRV-02", "name": "Marcus Tellefsen, DO", "clinic": "Riverbend Family Medicine", "pos_weight": 2.5},
    {"provider_id": "PRV-03", "name": "Priya Vantongeren, MD", "clinic": "Northgate Primary Care", "pos_weight": 1.0},
    {"provider_id": "PRV-04", "name": "Samuel Achterberg, NP", "clinic": "Northgate Primary Care", "pos_weight": 1.0},
    {"provider_id": "PRV-05", "name": "Helena Quarshie, MD", "clinic": "Lakeshore Internal Medicine", "pos_weight": 0.8},
    {"provider_id": "PRV-06", "name": "Theo Brannigan, PA-C", "clinic": "Lakeshore Internal Medicine", "pos_weight": 0.6},
]
FACILITY_ED = "Mercy Vale Regional Medical Center"  # made up
FACILITY_LAB = "Cardinal Point Reference Laboratory"  # made up
FACILITY_ENDO = "Lakeshore Endoscopy Center"  # made up

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def us(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def us_short(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def long(d: date) -> str:
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def any_fmt(d: date) -> str:
    return rng.choice([us, us_short, long])(d)


def rand_date(start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def to_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
# Note scaffolding
# ---------------------------------------------------------------------------

HPI_FILLER = [
    "Here for routine follow-up of chronic conditions.",
    "Reports intermittent right knee pain with stairs, worse in the morning.",
    "Seasonal allergies flaring; using over-the-counter loratadine.",
    "Sleeping poorly over the last month; discussed sleep hygiene.",
    "Requests refills of maintenance medications.",
    "Denies chest pain, shortness of breath, or palpitations.",
    "Walking about 20 minutes most days with a neighbor.",
    "Mild low back stiffness after gardening, improving with heat.",
]
AP_FILLER = [
    "Knee pain: likely osteoarthritis; trial of topical diclofenac gel.",
    "Health maintenance: influenza vaccine administered today.",
    "Allergic rhinitis: continue loratadine, add saline rinses.",
    "Insomnia: sleep hygiene handout given; reassess next visit.",
]


def base_vitals(member: dict) -> str:
    hr, temp = rng.randint(62, 88), rng.choice(['97.9', '98.2', '98.4', '98.6'])
    rng.randint(58, 112)  # keep the main random stream unchanged
    spo2 = rng.randint(95, 99)
    # Weight is stable per member (+/- 2 kg between visits), from its own stream.
    mr = random.Random(f"{member['member_id']}-{hr}-{temp}-{spo2}")
    base = random.Random(member["member_id"]).randint(58, 112)
    return f"HR {hr}, Temp {temp} F, Wt {base + mr.randint(-2, 2)} kg, SpO2 {spo2}%"


def header(kind: str, member: dict, provider: dict, d: date, extra: str = "") -> str:
    lines = [kind,
             f"Clinic: {provider['clinic']}",
             f"Provider: {provider['name']}",
             f"Patient: {member['name']}    DOB: {us(date.fromisoformat(member['dob']))}    Member ID: {member['member_id']}",
             f"Date of service: {us(d)}"]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def progress_note(member, provider, d, hpi, vitals, results, ap, visit_type="Office visit (in person)"):
    body = [header("PROGRESS NOTE", member, provider, d, f"Visit type: {visit_type}"), "",
            "SUBJECTIVE", *hpi, "",
            "OBJECTIVE", f"Vitals: {vitals}", *results, "",
            "ASSESSMENT AND PLAN", *ap, "",
            f"Electronically signed by {provider['name']} on {us(d)}"]
    return "\n".join(body) + "\n"


def lab_fax(member, d: date, test: str, value: str, units: str, ref: str, flag: str = "") -> str:
    sent = d + timedelta(days=rng.randint(1, 3))
    return "\n".join([
        f"*** FAX ***   FROM: {FACILITY_LAB}   TO: PCP OFFICE   {us(sent)} 07:{rng.randint(10, 59)}   PAGE 1 OF 1",
        "",
        "        LABORATORY REPORT - FINAL",
        f"PATIENT: {member['name'].upper()}        DOB: {us(date.fromisoformat(member['dob']))}",
        f"MEMBER ID: {member['member_id']}",
        f"COLLECTED: {us(d)}    RECEIVED: {us(d)}    REPORTED: {us(sent)}",
        "",
        "TEST                         RESULT     UNITS    REF RANGE   FLAG",
        f"{test:<28} {value:<10} {units:<8} {ref:<11} {flag}",
        "",
        "Results verified by: M. Ostrowski, MLS(ASCP)",
        "CONFIDENTIAL - intended only for the addressee",
    ]) + "\n"


# ---------------------------------------------------------------------------
# Scenarios
#
# A scenario receives a context (member, provider, note date, pronouns) and
# returns contributions to the shared progress note plus any extra documents,
# and the hand-written expected finding for its gap.
#
# Label conventions (see data/README.md):
#   finding: numerator | exclusion | abstain | none
#   abstain_reason: bp_range_or_threshold | ineligible_setting |
#     home_reading_not_digital | missing_value | missing_or_ambiguous_date |
#     outside_lookback_window | not_completed
#   evidence: the exact passage a reviewer should see highlighted ("doc" is
#     "note" for the shared progress note, or an index into extra_docs)
# ---------------------------------------------------------------------------


class S:
    """One scenario's output."""

    def __init__(self, category: str, finding: str, *, fields: Optional[dict] = None,
                 reason: Optional[str] = None, evidence: Optional[str] = None, doc="note",
                 hpi=(), vitals_bp: Optional[str] = None, results=(), ap=(), extra_docs=(),
                 visit_type: Optional[str] = None, note: str = ""):
        self.category, self.finding, self.fields, self.reason = category, finding, fields or {}, reason
        self.evidence, self.doc = evidence, doc
        self.hpi, self.vitals_bp, self.results, self.ap = list(hpi), vitals_bp, list(results), list(ap)
        self.extra_docs = list(extra_docs)  # list of (doc_type, date, text)
        self.visit_type, self.note = visit_type, note


def ctl_bp():
    return rng.randint(112, 138), rng.randint(64, 86)


def high_bp():
    return rng.choice([(rng.randint(142, 164), rng.randint(82, 96)), (rng.randint(130, 138), rng.randint(90, 96))])


HTN_AP = "Hypertension: continue lisinopril 20 mg daily; low-sodium diet reviewed."
DM_AP = "Type 2 diabetes: continue metformin 1000 mg twice daily; foot exam normal."


# ---- CBP ------------------------------------------------------------------

def cbp_pos_office(c):
    s, d = ctl_bp()
    form = rng.choice(["plain", "mmhg", "repeat"])
    if form == "plain":
        ev, bp = f"BP {s}/{d}", f"BP {s}/{d}"
    elif form == "mmhg":
        ev = bp = f"Blood pressure {s}/{d} mmHg (seated, left arm)"
    else:
        hs, hd = rng.randint(142, 152), rng.randint(d + 1, 94)
        ev = f"repeat after 5 minutes seated {s}/{d}"
        bp = f"BP {hs}/{hd} on arrival; {ev}"
    return S("positive", "numerator", evidence=ev, vitals_bp=bp, ap=[HTN_AP],
             fields={"systolic": s, "diastolic": d, "reading_date": c["d"].isoformat(),
                     "setting": "outpatient", "source": "office"},
             note="same-day repeat: lowest systolic and lowest diastolic govern" if form == "repeat" else "")


def cbp_pos_multi(c):
    early = c["d"] - timedelta(days=rng.randint(45, 110))
    hs, hd = rng.randint(148, 160), rng.randint(90, 98)
    s, d = ctl_bp()
    early_note = progress_note(c["m"], c["p"], early, [rng.choice(HPI_FILLER)],
                               f"BP {hs}/{hd}, {base_vitals(c['m'])}", [],
                               ["Elevated blood pressure: increase amlodipine to 10 mg daily; recheck in 6-8 weeks."])
    return S("positive", "numerator", evidence=f"BP {s}/{d}", vitals_bp=f"BP {s}/{d}",
             ap=["Hypertension: recheck after amlodipine increase; continue current dose."],
             extra_docs=[("progress_note", early, early_note)],
             fields={"systolic": s, "diastolic": d, "reading_date": c["d"].isoformat(),
                     "setting": "outpatient", "source": "office"},
             note=f"earlier reading {hs}/{hd} on {early.isoformat()} is superseded by the most recent")


def cbp_pos_home_digital(c):
    s, d = ctl_bp()
    rd = c["d"] - timedelta(days=rng.randint(2, 9))
    ev = f"reading from {us(rd)} was {s}/{d}"
    return S("positive", "numerator", evidence=ev, visit_type="Telehealth (video)",
             hpi=[f"{c['He']} uploaded readings from {c['his']} digital upper-arm BP monitor to the patient portal; the most recent {ev}."],
             vitals_bp=None, ap=[HTN_AP],
             fields={"systolic": s, "diastolic": d, "reading_date": rd.isoformat(),
                     "setting": "home", "source": "digital_home_device"})


def cbp_pos_relative(c):  # note date is a Monday
    s, d = ctl_bp()
    rd = c["d"] - timedelta(days=5)
    ev = f"nurse BP check here last Wednesday: {s}/{d}"
    return S("positive", "numerator", evidence=ev, visit_type="Telephone follow-up",
             hpi=[f"Calling to review medications after {c['his']} {ev}. No dizziness."],
             ap=[HTN_AP],
             fields={"systolic": s, "diastolic": d, "reading_date": rd.isoformat(),
                     "setting": "outpatient", "source": "office"},
             note="relative date resolved from the dated note (Monday note, prior Wednesday)")


def cbp_neg_no_bp(c):
    return S("negative", "none", ap=[rng.choice(AP_FILLER)])


def cbp_neg_uncontrolled(c):
    s, d = high_bp()
    return S("negative", "none", vitals_bp=f"BP {s}/{d}",
             ap=["Hypertension, above goal today: add hydrochlorothiazide 12.5 mg daily; recheck in 4 weeks."],
             note=f"most recent reading {s}/{d} does not meet <140/90; gap remains open")


def cbp_neg_multi_later_high(c):
    early = c["d"] - timedelta(days=rng.randint(45, 110))
    s0, d0 = ctl_bp()
    s, d = high_bp()
    early_note = progress_note(c["m"], c["p"], early, [rng.choice(HPI_FILLER)],
                               f"BP {s0}/{d0}, {base_vitals(c['m'])}", [], [HTN_AP])
    return S("trap", "none", vitals_bp=f"BP {s}/{d}",
             ap=["Hypertension: above goal today after stopping amlodipine on own; restart and recheck."],
             extra_docs=[("progress_note", early, early_note)],
             note=f"earlier controlled reading {s0}/{d0} is superseded by most recent {s}/{d}; extracting the earlier one is a wrong value")


def cbp_trap_range(c):
    ev = "BPs have been running in the 130s over 80s at recent checks"
    return S("trap", "abstain", reason="bp_range_or_threshold", evidence=ev,
             hpi=[f"Reports {ev}."], ap=[HTN_AP])


def cbp_trap_threshold(c):
    ev = "BP under control on current regimen"
    return S("trap", "abstain", reason="bp_range_or_threshold", evidence=ev,
             ap=[f"Hypertension: {ev}, continue lisinopril."])


def cbp_trap_ed(c):
    s, d = ctl_bp()
    ed = c["d"] - timedelta(days=rng.randint(10, 60))
    ev = f"Triage vitals: BP {s}/{d}"
    text = "\n".join([
        "EMERGENCY DEPARTMENT NOTE",
        f"Facility: {FACILITY_ED}",
        "Attending: R. Feldkamp, MD",
        f"Patient: {c['m']['name']}    Member ID: {c['m']['member_id']}",
        f"Arrival: {us(ed)} 18:42",
        "", "Chief complaint: left ankle pain after a fall from a step stool.",
        f"{ev}, HR 92, RR 16, SpO2 98%.",
        "Exam: swelling over lateral malleolus, able to bear weight with pain.",
        "X-ray: no fracture.",
        "Disposition: discharged home with ankle brace; follow up with PCP.",
    ]) + "\n"
    return S("trap", "abstain", reason="ineligible_setting", evidence=ev, doc=0,
             extra_docs=[("ed_note", ed, text)], ap=[HTN_AP],
             note="ED readings are not eligible for CBP")


def cbp_trap_inpatient(c):
    s, d = ctl_bp()
    adm = c["d"] - timedelta(days=rng.randint(20, 70))
    dc = adm + timedelta(days=3)
    ev = f"Vital signs at discharge: BP {s}/{d}"
    text = "\n".join([
        "DISCHARGE SUMMARY",
        f"Facility: {FACILITY_ED}",
        f"Patient: {c['m']['name']}    Member ID: {c['m']['member_id']}",
        f"Admitted: {us(adm)}    Discharged: {us(dc)}",
        "", "Principal diagnosis: community-acquired pneumonia.",
        "Hospital course: treated with ceftriaxone and azithromycin, transitioned to oral antibiotics, afebrile x48h.",
        f"{ev}, HR 80, SpO2 96% on room air.",
        "Discharge medications: amoxicillin-clavulanate x5 days; home medications unchanged.",
        "Follow-up: PCP within 7 days.",
    ]) + "\n"
    return S("trap", "abstain", reason="ineligible_setting", evidence=ev, doc=0,
             extra_docs=[("discharge_summary", dc, text)], ap=[HTN_AP],
             note="inpatient readings are not eligible for CBP")


def cbp_trap_procedure_day(c):
    s, d = ctl_bp()
    pd = c["d"] - timedelta(days=rng.randint(10, 60))
    ev = f"Pre-procedure vitals: BP {s}/{d}"
    text = "\n".join([
        "PROCEDURE NOTE - UPPER ENDOSCOPY (EGD)",
        f"Facility: {FACILITY_ENDO}",
        f"Patient: {c['m']['name']}    Member ID: {c['m']['member_id']}",
        f"Date of procedure: {us(pd)}",
        "", "Indication: chronic dyspepsia.",
        f"{ev}, HR 70.",
        "Sedation: moderate sedation.",
        "Findings: mild antral gastritis; biopsies taken.",
        "Complications: none.",
    ]) + "\n"
    return S("trap", "abstain", reason="ineligible_setting", evidence=ev, doc=0,
             extra_docs=[("procedure_note", pd, text)], ap=[HTN_AP],
             note="readings taken on the day of a procedure are not eligible")


def cbp_trap_home_nondigital(c):
    s, d = ctl_bp()
    ev = f"home BP last week was {s}/{d}"
    return S("trap", "abstain", reason="home_reading_not_digital", evidence=ev,
             hpi=[f"{c['He']} states {c['his']} {ev}."], ap=[HTN_AP],
             note="member-reported home reading with no digital device documented")


def cbp_trap_vague_date(c):
    s, d = ctl_bp()
    ev = f"BP was {s}/{d} when checked at urgent care a few months ago"
    return S("trap", "abstain", reason="missing_or_ambiguous_date", evidence=ev,
             hpi=[f"Notes {ev}."], ap=[HTN_AP])


def cbp_excl_esrd(c):
    start = date(rng.choice([2023, 2024]), rng.randint(1, 12), 1)
    ev = f"ESRD on in-center hemodialysis Mon/Wed/Fri, started {start.month:02d}/{start.year}"
    return S("exclusion", "exclusion", evidence=ev, ap=[ev + "; nephrology following.", HTN_AP],
             fields={"exclusion_type": "esrd_dialysis", "supporting_date": f"{start.year}-{start.month:02d}"})


def cbp_excl_hospice(c):
    md = c["d"] - timedelta(days=rng.randint(20, 120))  # enrollment precedes the visit that records it
    md = max(md, date(MY, 1, 1))
    ev = f"enrolled with hospice {md.month}/{md.year}"
    return S("exclusion", "exclusion", evidence=ev,
             hpi=[f"Daughter present. Patient {ev} for end-stage COPD."],
             ap=["Goals of care reviewed; continue hospice plan of care."],
             fields={"exclusion_type": "hospice", "supporting_date": f"{md.year}-{md.month:02d}"})


# ---- GSD ------------------------------------------------------------------

def a1c_fax_doc(c, d, v):
    flag = "H" if v > 5.6 else ""
    return ("lab_report", d, lab_fax(c["m"], d, "HEMOGLOBIN A1C", f"{v:.1f}", "%", "4.0-5.6", flag))


def gsd_pos(c, v=None, tier_note=""):
    v = v if v is not None else round(rng.uniform(6.2, 7.8), 1)
    form = rng.choice(["results", "fax", "poc"])
    fields = {"a1c_value": v}
    if form == "results":
        td = c["d"] - timedelta(days=rng.randint(3, 20))
        ev = f"HbA1c {v}% (collected {us(td)})"
        out = S("positive", "numerator", evidence=ev, results=[f"Labs: {ev}; LDL 96."], ap=[DM_AP])
    elif form == "fax":
        td = c["d"] - timedelta(days=rng.randint(5, 30))
        doc = a1c_fax_doc(c, td, v)
        ev = f"{'HEMOGLOBIN A1C':<28} {v:.1f}"
        out = S("positive", "numerator", evidence=ev, doc=0, extra_docs=[doc],
                ap=[DM_AP, "Reviewed outside lab report."])
    else:
        td = c["d"]
        ev = f"POC HbA1c today: {v}%"
        out = S("positive", "numerator", evidence=ev, results=[ev], ap=[DM_AP])
    fields["test_date"] = td.isoformat()
    out.fields, out.note = fields, tier_note
    return out


def gsd_pos_controlled(c):
    return gsd_pos(c)


def gsd_pos_poor(c):
    return gsd_pos(c, round(rng.uniform(9.2, 11.4), 1), "poor control tier (>9.0)")


def gsd_pos_mid(c):
    return gsd_pos(c, round(rng.uniform(8.1, 8.9), 1), "between tiers (8.0-9.0)")


def gsd_pos_multi(c):
    early = c["d"] - timedelta(days=rng.randint(60, 120))
    v0 = round(rng.uniform(9.1, 10.2), 1)
    v = round(rng.uniform(7.0, 7.9), 1)
    ev = f"POC HbA1c today: {v}%"
    return S("positive", "numerator", evidence=ev, results=[ev], extra_docs=[a1c_fax_doc(c, early, v0)],
             ap=["Type 2 diabetes: improved since dose increase; continue metformin and empagliflozin."],
             fields={"a1c_value": v, "test_date": c["d"].isoformat()},
             note=f"earlier result {v0} on {early.isoformat()} is superseded")


def gsd_pos_relative(c):  # note date is a Monday
    v = round(rng.uniform(6.3, 7.6), 1)
    ev = f"A1c drawn at the lab last Wednesday returned {v}%"
    return S("positive", "numerator", evidence=ev, visit_type="Telephone follow-up",
             hpi=[f"Reviewed results by phone: {ev}."], ap=[DM_AP],
             fields={"a1c_value": v, "test_date": (c["d"] - timedelta(days=5)).isoformat()},
             note="relative date resolved from the dated note")


def gsd_neg_none(c):
    return S("negative", "none", ap=[DM_AP, rng.choice(AP_FILLER)])


def gsd_trap_pending(c):
    ev = "HbA1c ordered today, results pending"
    return S("trap", "abstain", reason="not_completed", evidence=ev,
             ap=[f"Type 2 diabetes: {ev}; continue metformin."],
             note="ordered with no result is not evidence")


def gsd_trap_recent(c):
    ev = "recent A1c was good per patient"
    return S("trap", "abstain", reason="missing_value", evidence=ev,
             ap=[f"Type 2 diabetes: {ev}; continue metformin."])


def gsd_trap_normal_nodate(c):
    ev = f"A1c was normal at {c['his']} last check"
    return S("trap", "abstain", reason="missing_value", evidence=ev,
             hpi=[f"Reports {ev}."], ap=[DM_AP])


def gsd_trap_number_nodate(c):
    v = round(rng.uniform(6.4, 7.6), 1)
    ev = f"Last A1c {v} per outside records"
    return S("trap", "abstain", reason="missing_or_ambiguous_date", evidence=ev,
             hpi=[f"{ev}; records not yet received."], ap=[DM_AP])


def gsd_trap_prior_year(c):
    v = round(rng.uniform(6.5, 7.5), 1)
    td = date(MY - 1, rng.randint(10, 12), rng.randint(1, 28))
    ev = f"HbA1c {v}% ({us(td)})"
    return S("trap", "abstain", reason="outside_lookback_window", evidence=ev,
             results=[f"Most recent labs: {ev}."], ap=[DM_AP + " Repeat A1c ordered."],
             note="result is from the prior year, outside MY2026")


def gsd_trap_vague_date(c):
    v = round(rng.uniform(6.5, 7.5), 1)
    ev = f"A1c was {v} a few months ago"
    return S("trap", "abstain", reason="missing_or_ambiguous_date", evidence=ev,
             hpi=[f"{c['He']} recalls {c['his']} {ev}."], ap=[DM_AP])


def gsd_excl_palliative(c):
    ev = "comfort care discussed and elected"
    return S("exclusion", "exclusion", evidence=ev,
             ap=[f"Goals of care meeting with patient and son: {ev}. Palliative care referral placed.",
                 "Type 2 diabetes: relax glycemic targets; stop glipizide."],
             fields={"exclusion_type": "palliative_care", "supporting_date": c["d"].isoformat()})


def gsd_excl_frailty(c):
    ev = "Frailty: uses a walker, two falls since spring"
    return S("exclusion", "exclusion", evidence=ev,
             hpi=["Advanced illness: heart failure, NYHA class IV, on home oxygen.", f"{ev}."],
             ap=["Heart failure: continue furosemide; cardiology following.", DM_AP],
             fields={"exclusion_type": "advanced_illness_frailty", "supporting_date": c["d"].isoformat()},
             note="requires age 66+ and both advanced illness and frailty")


# ---- COL-E ----------------------------------------------------------------

def cole_hist_colonoscopy(c):
    y = rng.choice([2019, 2021])
    ev = rng.choice([f"colonoscopy {y}, normal", f"colonoscopy in {y} with two small polyps removed"])
    return S("positive", "numerator", evidence=ev,
             hpi=[f"Past surgical/procedural history: cholecystectomy; {ev}."], ap=[rng.choice(AP_FILLER)],
             fields={"procedure_type": "colonoscopy", "procedure_date": str(y)},
             note="history with a stated year counts; lands as supplemental data")


def cole_fit(c):
    td = c["d"] - timedelta(days=rng.randint(10, 90))
    if td.year < MY:
        td = date(MY, 1, 15)
    if rng.random() < 0.5:
        ev = f"FIT (fecal immunochemical test) {us(td)}: negative"
        return S("positive", "numerator", evidence=ev, results=[ev],
                 ap=["Colorectal cancer screening: FIT negative, repeat in one year."],
                 fields={"procedure_type": "fit", "procedure_date": td.isoformat()})
    doc = ("lab_report", td, lab_fax(c["m"], td, "FECAL IMMUNOCHEM TEST", "NEGATIVE", "", "NEGATIVE"))
    return S("positive", "numerator", evidence=f"{'FECAL IMMUNOCHEM TEST':<28} NEGATIVE", doc=0,
             extra_docs=[doc], ap=[rng.choice(AP_FILLER)],
             fields={"procedure_type": "fit", "procedure_date": td.isoformat()})


def cole_fit_dna(c):
    m = rng.randint(1, 12)
    y = rng.choice([2024, 2025])
    ev = f"Cologuard (FIT-DNA) {m:02d}/{y}: negative"
    return S("positive", "numerator", evidence=ev, hpi=[f"Screening history: {ev}."], ap=[rng.choice(AP_FILLER)],
             fields={"procedure_type": "fit_dna", "procedure_date": f"{y}-{m:02d}"})


def cole_ct(c):
    m = rng.randint(1, 12)
    ev = f"CT colonography ({m:02d}/2023): no polyps identified"
    return S("positive", "numerator", evidence=ev, hpi=[f"Screening history: {ev}."], ap=[rng.choice(AP_FILLER)],
             fields={"procedure_type": "ct_colonography", "procedure_date": f"2023-{m:02d}"})


def cole_flexsig(c):
    m = rng.randint(1, 12)
    ev = f"flexible sigmoidoscopy in {MONTHS[m - 1]} 2022, unremarkable"
    return S("positive", "numerator", evidence=ev, hpi=[f"Prior {ev}."], ap=[rng.choice(AP_FILLER)],
             fields={"procedure_type": "flexible_sigmoidoscopy", "procedure_date": f"2022-{m:02d}"})


def cole_colonoscopy_report(c):
    pd = c["d"] - timedelta(days=rng.randint(10, 60))
    text = "\n".join([
        "PROCEDURE NOTE - COLONOSCOPY",
        f"Facility: {FACILITY_ENDO}",
        f"Patient: {c['m']['name']}    Member ID: {c['m']['member_id']}",
        f"Date of procedure: {us(pd)}",
        "", "Indication: average-risk colorectal cancer screening.",
        "Preparation: good (Boston Bowel Prep Score 8).",
        "Extent: cecum reached, appendiceal orifice and ileocecal valve identified.",
        "Findings: normal colon. No polyps.",
        "Recommendation: repeat colonoscopy in 10 years.",
    ]) + "\n"
    return S("positive", "numerator", evidence="Findings: normal colon. No polyps.", doc=0,
             extra_docs=[("procedure_note", pd, text)], ap=["Reviewed colonoscopy report; normal."],
             fields={"procedure_type": "colonoscopy", "procedure_date": pd.isoformat()})


def cole_neg_none(c):
    return S("negative", "none", ap=[rng.choice(AP_FILLER)])


def cole_neg_declined(c):
    return S("negative", "none",
             ap=["Colorectal cancer screening: discussed colonoscopy and stool-based options; patient declines at this time."])


def cole_trap_old_colonoscopy(c):
    ev = "colonoscopy 2015, normal"
    return S("trap", "abstain", reason="outside_lookback_window", evidence=ev,
             hpi=[f"Past procedural history: {ev}."], ap=[rng.choice(AP_FILLER)],
             note="2015 is outside the 10-year window (2017-2026)")


def cole_trap_fit_old(c):
    td = date(MY - 1, rng.randint(3, 11), rng.randint(1, 28))
    ev = f"FIT {us(td)}: negative"
    return S("trap", "abstain", reason="outside_lookback_window", evidence=ev,
             results=[f"Prior screening: {ev}."], ap=["Colorectal cancer screening: due; FIT kit offered."],
             note="FIT must be in the measurement year")


def cole_trap_fitdna_old(c):
    ev = "Cologuard 2022, negative"
    return S("trap", "abstain", reason="outside_lookback_window", evidence=ev,
             hpi=[f"Screening history: {ev}."], ap=[rng.choice(AP_FILLER)],
             note="FIT-DNA window is 2024-2026")


def cole_trap_undated(c):
    ev = "had a colonoscopy years ago and does not remember when"
    return S("trap", "abstain", reason="missing_or_ambiguous_date", evidence=ev,
             hpi=[f"{c['He']} {ev}; says it was fine."], ap=["Request outside GI records."])


def cole_trap_ordered(c):
    ev = "FIT kit provided today with instructions"
    return S("trap", "abstain", reason="not_completed", evidence=ev,
             ap=[f"Colorectal cancer screening: {ev}; patient to return by mail."],
             note="a kit handed out is not a completed test")


def cole_excl_colectomy(c):
    ev = "s/p total colectomy (2012) for ulcerative colitis"
    return S("exclusion", "exclusion", evidence=ev, hpi=[f"Past surgical history: {ev}."], ap=[rng.choice(AP_FILLER)],
             fields={"exclusion_type": "total_colectomy", "supporting_date": "2012"})


def cole_excl_crc(c):
    ev = "History of colon cancer, stage II, s/p right hemicolectomy 2018"
    return S("exclusion", "exclusion", evidence=ev, hpi=[f"{ev}; surveillance with oncology."],
             ap=[rng.choice(AP_FILLER)],
             fields={"exclusion_type": "colorectal_cancer", "supporting_date": "2018"})


def cole_excl_hospice(c):
    hd = c["d"] - timedelta(days=rng.randint(7, 90))  # election precedes the visit that records it
    hd = max(hd, date(MY, 1, 1))
    ev = f"elected the hospice benefit on {us(hd)}"
    return S("exclusion", "exclusion", evidence=ev,
             hpi=[f"Patient {ev}; metastatic pancreatic cancer."],
             ap=["Coordinate with hospice team; no further screening indicated."],
             fields={"exclusion_type": "hospice", "supporting_date": hd.isoformat()})


# (measure, scenario fn, count, needs Monday note date, can pair with GSD)
PLAN = [
    ("CBP", cbp_pos_office, 4, False, True),
    ("CBP", cbp_pos_multi, 2, False, True),
    ("CBP", cbp_pos_home_digital, 1, False, False),
    ("CBP", cbp_pos_relative, 1, True, False),
    ("CBP", cbp_neg_no_bp, 2, False, True),
    ("CBP", cbp_neg_uncontrolled, 2, False, True),
    ("CBP", cbp_neg_multi_later_high, 1, False, True),
    ("CBP", cbp_trap_range, 1, False, True),
    ("CBP", cbp_trap_threshold, 1, False, True),
    ("CBP", cbp_trap_ed, 1, False, True),
    ("CBP", cbp_trap_inpatient, 1, False, True),
    ("CBP", cbp_trap_procedure_day, 1, False, False),
    ("CBP", cbp_trap_home_nondigital, 1, False, True),
    ("CBP", cbp_trap_vague_date, 1, False, True),
    ("CBP", cbp_excl_esrd, 1, False, False),
    ("CBP", cbp_excl_hospice, 1, False, False),
    ("GSD", gsd_pos_controlled, 3, False, True),
    ("GSD", gsd_pos_poor, 1, False, True),
    ("GSD", gsd_pos_mid, 1, False, True),
    ("GSD", gsd_pos_multi, 1, False, True),
    ("GSD", gsd_pos_relative, 1, True, False),
    ("GSD", gsd_neg_none, 3, False, True),
    ("GSD", gsd_trap_pending, 1, False, True),
    ("GSD", gsd_trap_recent, 1, False, True),
    ("GSD", gsd_trap_normal_nodate, 1, False, True),
    ("GSD", gsd_trap_number_nodate, 1, False, True),
    ("GSD", gsd_trap_prior_year, 1, False, True),
    ("GSD", gsd_trap_vague_date, 1, False, True),
    ("GSD", gsd_excl_palliative, 1, False, False),
    ("GSD", gsd_excl_frailty, 1, False, False),
    ("COL-E", cole_hist_colonoscopy, 2, False, False),
    ("COL-E", cole_fit, 2, False, False),
    ("COL-E", cole_fit_dna, 1, False, False),
    ("COL-E", cole_ct, 1, False, False),
    ("COL-E", cole_flexsig, 1, False, False),
    ("COL-E", cole_colonoscopy_report, 1, False, False),
    ("COL-E", cole_neg_none, 3, False, False),
    ("COL-E", cole_neg_declined, 1, False, False),
    ("COL-E", cole_trap_old_colonoscopy, 1, False, False),
    ("COL-E", cole_trap_fit_old, 1, False, False),
    ("COL-E", cole_trap_fitdna_old, 1, False, False),
    ("COL-E", cole_trap_undated, 1, False, False),
    ("COL-E", cole_trap_ordered, 1, False, False),
    ("COL-E", cole_excl_colectomy, 1, False, False),
    ("COL-E", cole_excl_crc, 1, False, False),
    ("COL-E", cole_excl_hospice, 1, False, False),
]
N_PAIRS = 12        # members with both a CBP and a GSD gap in one chart
N_CHASE_ONLY = 8    # gaps with no chart yet (feed the chase-worthiness view)

AGE_RANGE = {"CBP": (45, 85), "GSD": (40, 75), "COL-E": (46, 75)}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def make_member(idx: int, age_lo: int, age_hi: int) -> dict:
    sex = rng.choice(["F", "M"])
    first = rng.choice(FIRST_F if sex == "F" else FIRST_M)
    age = rng.randint(age_lo, age_hi)
    dob = date(MY - age, rng.randint(1, 12), rng.randint(1, 28))
    return {"member_id": f"SYN-{100200 + idx * 7}", "name": f"{first} {rng.choice(LAST)}",
            "sex": sex, "dob": dob.isoformat(), "age_at_my_end": MY - dob.year}


def pick_provider(category: str) -> dict:
    weights = [p["pos_weight"] if category == "positive" else 1.0 for p in PROVIDERS]
    return rng.choices(PROVIDERS, weights=weights)[0]


def main() -> None:
    # Expand the plan into gap specs.
    specs = []
    for measure, fn, n, monday, pairable in PLAN:
        for _ in range(n):
            specs.append({"measure": measure, "fn": fn, "monday": monday, "pairable": pairable})

    # Form charts: pair some CBP+GSD gaps, rest single.
    cbp_p = [s for s in specs if s["measure"] == "CBP" and s["pairable"]]
    gsd_p = [s for s in specs if s["measure"] == "GSD" and s["pairable"]]
    rng.shuffle(cbp_p)
    rng.shuffle(gsd_p)
    charts = [[cbp_p[i], gsd_p[i]] for i in range(N_PAIRS)]
    paired = {id(s) for ch in charts for s in ch}
    charts += [[s] for s in specs if id(s) not in paired]
    rng.shuffle(charts)

    members, gaps, documents, labels = [], [], [], []
    docs_dir = DATA / "documents"
    if docs_dir.exists():
        shutil.rmtree(docs_dir)
    docs_dir.mkdir(parents=True)

    gap_n = 0
    for ci, chart in enumerate(charts):
        lo = max(AGE_RANGE[s["measure"]][0] for s in chart)
        hi = min(AGE_RANGE[s["measure"]][1] for s in chart)
        if any(s["fn"] is gsd_excl_frailty for s in chart):
            lo = max(lo, 68)
        member = make_member(ci, lo, hi)
        members.append(member)

        is_pos = any(s["fn"].__name__.split("_")[1] not in ("neg", "trap", "excl") for s in chart)
        provider = pick_provider("positive" if is_pos else "other")
        member["pcp_provider_id"] = provider["provider_id"]

        nd = rand_date(date(MY, 3, 2), date(MY, 9, 25))
        if any(s["monday"] for s in chart):
            nd = to_monday(nd)
        he = "She" if member["sex"] == "F" else "He"
        ctx = {"m": member, "p": provider, "d": nd, "He": he, "his": "her" if he == "She" else "his"}

        outs = [s["fn"](ctx) for s in chart]

        # Assemble the shared progress note.
        hpi = rng.sample(HPI_FILLER, 2)
        for o in outs:
            hpi += o.hpi
        bp = next((o.vitals_bp for o in outs if o.vitals_bp), None)
        vitals = f"{bp}, {base_vitals(member)}" if bp else base_vitals(member)
        visit_type = next((o.visit_type for o in outs if o.visit_type), "Office visit (in person)")
        if visit_type.startswith(("Telehealth", "Telephone")):
            vitals = "not obtained (remote visit)"
        results = [r for o in outs for r in o.results]
        ap = []
        for o in outs:
            ap += o.ap
        ap.append("Follow up in 3 months.")
        note_text = progress_note(member, provider, nd, hpi, vitals, results, ap, visit_type)

        chart_docs = []  # (doc_type, date, text)
        extra_index = {}
        for oi, o in enumerate(outs):
            for ei, ed in enumerate(o.extra_docs):
                extra_index[(oi, ei)] = len(chart_docs)
                chart_docs.append(ed)
        note_pos = len(chart_docs)
        chart_docs.append(("progress_note", nd, note_text))

        doc_ids = []
        order = sorted(range(len(chart_docs)), key=lambda i: chart_docs[i][1])
        id_of = {}
        for rank, i in enumerate(order):
            dtype, dd, text = chart_docs[i]
            doc_id = f"DOC-{member['member_id'][4:]}-{rank + 1}"
            id_of[i] = doc_id
            (docs_dir / f"{doc_id}.txt").write_text(text)
            documents.append({"doc_id": doc_id, "member_id": member["member_id"], "doc_type": dtype,
                              "doc_date": dd.isoformat(), "path": f"data/documents/{doc_id}.txt"})
            doc_ids.append(doc_id)

        for s, o in zip(chart, outs):
            gap_n += 1
            gap_id = f"GAP-{gap_n:04d}"
            gaps.append({"gap_id": gap_id, "member_id": member["member_id"], "measure": s["measure"],
                         "provider_id": provider["provider_id"],
                         "due_date": rand_date(date(MY, 11, 1), date(MY, 12, 31)).isoformat(),
                         "document_ids": [id_of[i] for i in order]})
            label = {"gap_id": gap_id, "member_id": member["member_id"], "measure": s["measure"],
                     "scenario": s["fn"].__name__, "category": o.category,
                     "expected_finding": o.finding, "expected_fields": o.fields,
                     "abstain_reason": o.reason, "evidence": None, "note": o.note}
            if o.evidence:
                src = note_pos if o.doc == "note" else extra_index[(outs.index(o), o.doc)]
                text = chart_docs[src][2]
                start = text.find(o.evidence)
                assert start >= 0, f"evidence not found for {gap_id} {s['fn'].__name__}: {o.evidence!r}"
                label["evidence"] = {"doc_id": id_of[src], "start": start, "end": start + len(o.evidence),
                                     "text": o.evidence}
            labels.append(label)

    # Chase-only gaps: members with an open gap and no chart retrieved yet.
    for k in range(N_CHASE_ONLY):
        measure = ["CBP", "GSD", "COL-E"][k % 3]
        member = make_member(len(charts) + k, *AGE_RANGE[measure])
        provider = rng.choice(PROVIDERS)
        member["pcp_provider_id"] = provider["provider_id"]
        members.append(member)
        gap_n += 1
        gaps.append({"gap_id": f"GAP-{gap_n:04d}", "member_id": member["member_id"], "measure": measure,
                     "provider_id": provider["provider_id"],
                     "due_date": rand_date(date(MY, 11, 1), date(MY, 12, 31)).isoformat(),
                     "document_ids": []})

    # 70/30 member-level split, stratified by each chart's first gap (measure, category).
    by_member = {}
    for lb in labels:
        by_member.setdefault(lb["member_id"], f"{lb['measure']}/{lb['category']}")
    strata: Dict[str, List[str]] = {}
    for mid, cat in by_member.items():
        strata.setdefault(cat, []).append(mid)
    heldout = set()
    for cat in sorted(strata):
        mids = sorted(strata[cat])
        rng.shuffle(mids)
        heldout.update(mids[:round(len(mids) * 0.3)])
    dev_labels = [lb for lb in labels if lb["member_id"] not in heldout]
    ho_labels = [lb for lb in labels if lb["member_id"] in heldout]

    def dump(path: Path, obj) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, indent=2) + "\n")

    providers_out = [{k: v for k, v in p.items() if k != "pos_weight"} for p in PROVIDERS]
    dump(DATA / "members.json", members)
    dump(DATA / "providers.json", providers_out)
    dump(DATA / "gaps.json", gaps)
    dump(DATA / "documents.json", documents)
    dump(DATA / "splits.json", {"seed": SEED, "unit": "member",
                                "dev": sorted(set(by_member) - heldout), "heldout": sorted(heldout)})
    for name, new in (("dev.json", dev_labels), ("heldout.json", ho_labels)):
        path = DATA / "labels" / name
        # Labels may carry hand corrections; never overwrite them silently.
        if path.exists() and json.loads(path.read_text()) != new and "--force-labels" not in sys.argv:
            sys.exit(f"{path} differs from the generated labels (hand edits?). "
                     "Re-run with --force-labels to overwrite.")
        dump(path, new)

    # Human-readable review sheet for the held-out labels.
    lines = ["# Held-out labels: review sheet", "",
             "Generated from the scenario definitions in `scripts/generate_data.py`. "
             "Review each row against the document before the first eval run; edit `heldout.json` directly if a label is wrong.", "",
             "For `none` rows there is no evidence passage: check that the listed documents contain nothing that closes the gap.", "",
             "| Gap | Measure | Documents | Expected | Fields / reason | Evidence | Note |", "|---|---|---|---|---|---|---|"]
    docs_of = {g["gap_id"]: g["document_ids"] for g in gaps}
    for lb in ho_labels:
        detail = lb["abstain_reason"] or ", ".join(f"{k}={v}" for k, v in lb["expected_fields"].items()) or "-"
        ev = f"`{lb['evidence']['doc_id']}`: \"{lb['evidence']['text']}\"" if lb["evidence"] else "-"
        docs = "<br>".join(f"`{d}`" for d in docs_of[lb["gap_id"]])
        lines.append(f"| {lb['gap_id']} | {lb['measure']} | {docs} | {lb['expected_finding']} | {detail} | {ev} | {lb['note'] or ''} |")
    (DATA / "labels" / "heldout_review.md").write_text("\n".join(lines) + "\n")

    # Summary.
    from collections import Counter
    print(f"members={len(members)} charts={len(charts)} documents={len(documents)} "
          f"gaps={len(gaps)} (labeled={len(labels)}, chase-only={N_CHASE_ONLY})")
    print("category mix:", dict(Counter(lb["category"] for lb in labels)))
    print("finding mix:", dict(Counter(lb["expected_finding"] for lb in labels)))
    print(f"dev: {len(dev_labels)} gaps / {len(by_member) - len(heldout)} members;  "
          f"heldout: {len(ho_labels)} gaps / {len(heldout)} members")
    print("heldout by measure:", dict(Counter(lb["measure"] for lb in ho_labels)),
          "by finding:", dict(Counter(lb["expected_finding"] for lb in ho_labels)))


if __name__ == "__main__":
    main()
