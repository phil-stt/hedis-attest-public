# Held-out labels: review sheet

Generated from the scenario definitions in `scripts/generate_data.py`. Review each row against the document before the first eval run; edit `heldout.json` directly if a label is wrong.

For `none` rows there is no evidence passage: check that the listed documents contain nothing that closes the gap.

| Gap | Measure | Documents | Expected | Fields / reason | Evidence | Note |
|---|---|---|---|---|---|---|
| GAP-0006 | CBP | `DOC-100228-1`<br>`DOC-100228-2` | numerator | systolic=112, diastolic=65, reading_date=2026-07-21, setting=outpatient, source=office | `DOC-100228-2`: "BP 112/65" | earlier reading 148/98 on 2026-04-09 is superseded by the most recent |
| GAP-0007 | GSD | `DOC-100228-1`<br>`DOC-100228-2` | abstain | missing_or_ambiguous_date | `DOC-100228-2`: "A1c was 7.0 a few months ago" |  |
| GAP-0018 | COL-E | `DOC-100298-1` | none | - | - |  |
| GAP-0019 | COL-E | `DOC-100305-1` | numerator | procedure_type=colonoscopy, procedure_date=2019 | `DOC-100305-1`: "colonoscopy 2019, normal" | history with a stated year counts; lands as supplemental data |
| GAP-0023 | COL-E | `DOC-100333-1` | abstain | not_completed | `DOC-100333-1`: "FIT kit provided today with instructions" | a kit handed out is not a completed test |
| GAP-0026 | CBP | `DOC-100347-1` | exclusion | exclusion_type=esrd_dialysis, supporting_date=2023-12 | `DOC-100347-1`: "ESRD on in-center hemodialysis Mon/Wed/Fri, started 12/2023" |  |
| GAP-0028 | COL-E | `DOC-100361-1` | abstain | outside_lookback_window | `DOC-100361-1`: "FIT 09/12/2025: negative" | FIT must be in the measurement year |
| GAP-0029 | CBP | `DOC-100368-1`<br>`DOC-100368-2` | numerator | systolic=120, diastolic=71, reading_date=2026-06-07, setting=outpatient, source=office | `DOC-100368-2`: "repeat after 5 minutes seated 120/71" | same-day repeat: lowest systolic and lowest diastolic govern |
| GAP-0030 | GSD | `DOC-100368-1`<br>`DOC-100368-2` | numerator | a1c_value=7.8, test_date=2026-05-08 | `DOC-100368-1`: "HEMOGLOBIN A1C               7.8" |  |
| GAP-0032 | CBP | `DOC-100382-1`<br>`DOC-100382-2`<br>`DOC-100382-3` | none | - | - | earlier controlled reading 137/80 is superseded by most recent 136/91; extracting the earlier one is a wrong value |
| GAP-0033 | GSD | `DOC-100382-1`<br>`DOC-100382-2`<br>`DOC-100382-3` | numerator | a1c_value=6.6, test_date=2026-08-13 | `DOC-100382-2`: "HEMOGLOBIN A1C               6.6" |  |
| GAP-0034 | COL-E | `DOC-100389-1`<br>`DOC-100389-2` | numerator | procedure_type=colonoscopy, procedure_date=2026-06-18 | `DOC-100389-1`: "Findings: normal colon. No polyps." |  |
| GAP-0036 | COL-E | `DOC-100403-1` | exclusion | exclusion_type=hospice, supporting_date=2026-03-28 | `DOC-100403-1`: "elected the hospice benefit on 03/28/2026" |  |
| GAP-0037 | GSD | `DOC-100410-1` | abstain | missing_value | `DOC-100410-1`: "recent A1c was good per patient" |  |
| GAP-0038 | GSD | `DOC-100417-1` | exclusion | exclusion_type=advanced_illness_frailty, supporting_date=2026-03-03 | `DOC-100417-1`: "Frailty: uses a walker, two falls since spring" | requires age 66+ and both advanced illness and frailty |
| GAP-0039 | CBP | `DOC-100424-1`<br>`DOC-100424-2` | abstain | bp_range_or_threshold | `DOC-100424-2`: "BP under control on current regimen" |  |
| GAP-0040 | GSD | `DOC-100424-1`<br>`DOC-100424-2` | numerator | a1c_value=6.3, test_date=2026-07-20 | `DOC-100424-1`: "HEMOGLOBIN A1C               6.3" |  |
| GAP-0060 | CBP | `DOC-100529-1` | none | - | - | most recent reading 158/88 does not meet <140/90; gap remains open |
