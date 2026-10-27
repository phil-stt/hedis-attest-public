# Changelog

## v2 (in development, `develop`)

### Held-out run of record after the G2-000111 fix (2026-10-12)
- `20261012T172436Z-v2-heldout`: 136/136, zero wrong values, 34/34 traps refused with the correct reason, field accuracy 68/68, every tranche 17/17 and clean on the per-tranche wrong-value gate; fallback model served 4 of 136 calls; $1.95 (cache share high). Replaces `20261011T190713Z` (and the CI gate run `20261012T164559Z`, 135/136, whose one miss was the G2-000111 defect) as the run of record.
- G2-000111: TRC hospice scenario drew the enrollment date relative to discharge and could place it after the note's date of service; now capped at the note date. One held-out row (re-reviewed by Phil) and one dev row changed.

### Phase 6: stratified eval (2026-10-12)
- `eval.stratified` / `metrics["strata"]`: per tranche, LOB, measure, evidence source, fragmentation, document types; n, correct, wrong values, abstains, numerator precision and actionable recall with lower bounds; `below_min_n` under 10 gaps; router skipped/lost per stratum. Text report prints the tranche and evidence-source cuts.
- Gate: `--gate-max-wrong-values-per-stratum 0` in CI; `metrics["max_wrong_values_in_a_tranche"]` in history.
- Eval page: "By stratum" tables for the latest held-out run, rebuilt from labels for runs saved before this phase; drift per tranche against the previous held-out run.
- Insights: yield by tranche, ranked by lower bound; keeps all tranches while the page is filtered.

### Managed Postgres for the hosted v2 (2026-10-12)
- The hosted instance moved from SQLite-in-the-container to a managed Postgres (smallest tier), with `DATABASE_URL` supplied as a secret, so several reviewers share one store and state survives a redeploy.

### Phase 5: daily-set surface, QA audits, supervisor view (2026-10-12)
- `daily_set` is the third surface arm; its home is `/today`. Sets of 40 dealt per reviewer from one tranche, stratified by finding type (largest-remainder apportionment, buckets interleaved), exclusive across reviewers' unfinished sets and others' leases. Progress read from decisions, not stored; stretch set of 10 after completion.
- Tables `sets`, `set_items`, `qa_audits`. Reset and dataset replacement clear them.
- QA sampling in `service.record_decision`: 20% of accepts (stable hash per gap) and every rejection of a proposed finding. `/qa` for a second person's verdict (`upheld` / `overturned`); self-audit refused. Audited error rate and confirmed catches now fill the Experiments page columns that said "not yet collected".
- `/supervise?tranche=`: open by status, decisions and reviewers today, abstain rate, audited error rate with n and LB, confirmed catches, sets dealt. Tranche filter added to Insights.
- Fixed: the Experiments page printed `dict.items` for the pipeline-items column.
- Queue measure filter now lists every active measure (was the three v1 ones).

### Phase 4: experimentation layer (2026-10-12)
- Variants table (extraction and surface), exposures table, and `variant_id` + `surface` on decisions and records. The pipeline registers its extraction variant per run.
- Surface arms: queue in priority order vs. queue in due-date order (the daily set joins in Phase 5). Assignment by reviewer hash; `attest_arm` cookie override, marked in exposures.
- `/experiments`: per-arm reviewers, exposures, reviewer-hours, decisions per hour, rates with n and lower bounds, abstains resolved, unaudited catches; extraction variants with cost per gap, skip share, unread-bucket recall. QA-audited error rate shown as not yet collected.
- `attest.eval diff <run A> <run B>`: two runs on one label set, gap by gap.
- Fixed: on a fresh store the pipeline created its job row before the first dataset import, which clears the job tables; the dataset is now ensured first.
- First hosted v2 (eval block, 408 findings, five measures). Fixed a thread race on first use of a fresh store that failed the CI gate once.

### Phase 3d: batch pipeline (2026-10-12)
- `attest/pipeline.py`: idempotent, resumable jobs keyed on (gap, variant); rate limiting; per-item tokens and cost; per-tranche report with cost per gap, cache-hit share, finding mix.
- Explicit prompt-cache breakpoint on the per-measure system prompt.
- Router variant: a cheap first pass (`--router claude-haiku-4-5`) with its skipped charts QA-sampled at 10% and recall on the unread bucket reported per tranche; `attest.eval --router` scores it with skips counted as `none`.
- Router prompt history, stated plainly: `router.v1` and `router.v2` never ran (the call passed an `effort` parameter Haiku 4.5 rejects, and the fail-open path hid the error as "read everything"; router errors are now counted in the report and the eval summary). `router.v3` adds an explicit content kind so an exclusion can never be classed as "nothing". The v3 wording was iterated using held-out spot checks, so the router's held-out figure is tuned; its figure of record comes from the dev set: run `20261012T043746Z-v2-dev-router`, 272 gaps, 53 skipped (19.5%), zero wrong values, numerator and exclusion recall 68/68 each; the 12 misses are abstain traps that came through as `none`. Cost $0.0125 per gap including the router versus about $0.0139 cached without it: roughly a 10% saving on this population, whose empty charts are 25% of the charted set.
- `pipeline compare` (two variants on common gaps, per tranche) and `seed export-snapshot` (a store's findings as a bundle's committed snapshot).
- Population measurement (2026-10-12): 1,088 gaps full extraction at $0.0119 per gap (83% cache-hit share), then the router variant on 1,000 of the same gaps: 23% skipped, zero closures lost, 60 abstains hidden, 3% cheaper. Eight workers tripped rate limiting (p90 latency 76 s); four workers ran three times faster.
- Pricing bug found by the comparison: served-model ids carry a date suffix (`claude-haiku-4-5-20251001`), so the router's Haiku calls were priced at Opus rates and the first comparison showed the router 32% dearer. Price lookup now tolerates the suffix and refuses unknown models; router tokens and cost are stored apart from the extractor's; this run's items were recosted from the call log.
- The store adds missing columns in place (`ALTER TABLE ADD COLUMN`) instead of rebuilding a SQLite store, so a code update never wipes paid-for findings.
- `seed/eval-block.findings.json`: the eval block's 408 findings from the population run, the v2 service's starting snapshot.
- Prompt caching measured: the 136-gap held-out run cost $1.89 with the cache breakpoint at batch concurrency versus $3.20 without (353k of 485k input tokens served from cache); output tokens are now two-thirds of the cost.

### Phase 3c: TRC and KED prompts (2026-10-11)
- Prompts `trc.v3` (one prompt, four indicator sections) and `ked.v1`; both measures active in the registry.
- Dev (80 TRC/KED gaps): 80/80, zero wrong values, no refusals. Two generator defects found on the way, both by the model being right: the outreach-only PED trap sat in a note that was itself an office visit (now a no-encounter chart review); and `trc.v1` let a member's later mention of the stay read as a late notification (clarified in `trc.v3`).
- Safeguard refusals: `trc.v1` tripped the `reasoning_extraction` classifier on 25% of TRC charts, deterministically per chart; asking for conclusions instead of narrated date arithmetic (`trc.v2`, then `trc.v3`) took it to zero on dev. A named fallback model (`claude-opus-4-8`, no such classifier) now backs every call; a finding served by the fallback records that model.
- `attest.eval --only CODE,CODE` runs a label set for a subset of measures.
- Held-out run of record with five measures: `20261011T190713Z-v2-heldout`, 136 gaps, 136/136 correct, zero wrong values, gate passed; the fallback served 2 of 136 (both CBP traps, both correct).

### Phase 3b: TRC and KED synthetic scenarios (2026-10-11)
- `scripts/scenarios_v2.py`: TRC by indicator (notification of admission, receipt of discharge information, patient engagement, medication reconciliation) with an invented discharge summary as context and traps for late notification, late receipt, undated paperwork, outreach-only, same-day and late engagement, reconciliation by a medical assistant, late reconciliation, review-without-reconciliation; KED with eGFR + uACR on faxes and in notes, split across documents, and traps for eGFR-only, dipstick protein, prior-year results, undated outside results, and orders.
- Eval block grows to 408 gaps: the 96 reviewed held-out rows unchanged, plus 40 new (two tranches per measure × category). Labels carry `indicator`; evidence may list several passages.
- New test: every labeled numerator/exclusion, replayed as a finding, must pass the rule checks. It caught a KED date drawn in the prior year before any model saw it.

### Phase 3a: measure registry (2026-10-11)
- Measures as data (`attest/measures/*.yaml`): all 14 priority measures with status and chart lever; TRC carries four indicator specs; KED is a paired-labs spec. `/measures` page.
- Rule checks are families parameterized from the registry (`blood_pressure`, `lab_value`, `paired_labs`, `procedure_window`, `event_window`, exclusions). CBP, GSD, and COL-E moved onto it with no behavior change (v1 dev eval reproduced).
- Evidence is a list of up to three verbatim spans; the review page highlights all of them; records carry `source_spans`. Closes the frailty two-quote roadmap item. Prompt versions bumped (`cbp.v2`, `gsd.v2`, `cole.v3`) for the new evidence shape.
- New abstain reasons: `incomplete_evidence` (half of a required pair), `ineligible_author` (the right act by the wrong role).

### Phase 2: synthetic population v2 (2026-10-11)
- First v2 held-out run of record (`20261011T062519Z-v2-heldout`, 96 gaps): zero wrong values, numerator 24/24, all 24 traps refused, 31/31 secondary-source evidence found. Two abstains on hospice exclusions turned out to be a generator defect: the hospice date was drawn independently of the visit date, so two notes recorded an enrollment in the future. The model refused those documents as internally inconsistent, which is the intended behavior. Fixed in the scenario library (hospice dates now precede the visit); the seven affected held-out rows were regenerated and re-reviewed; the run of record is the rerun after the fix: `20261011T070021Z-v2-heldout`, 96/96 correct, zero wrong values, gate passed.
- The held-out review sheet now shows each document's type and date beside the evidence, so date consistency is checkable.
- Generator v2 (`scripts/generate_data_v2.py`): business-unit tranches, fragmented sources with their own member identifiers, duplicate and cross-provider charts, chase-only gaps, a fixed stratified eval block (72 held-out, 144 dev), bulk to any size (100k gaps: 11.5 MB gzipped).
- `ATTEST_DATASET` selects the population; importing a dataset replaces the one in the store and clears the state that referenced it.
- Queue pagination; document source shown on review tabs; eval and seed accept any label-set name.
- Fixed: the `main`-branch eval gate broke when Phase 1 moved the dataset into the store (a fresh checkout had nothing to evaluate). Data access now self-imports on an empty store.
- Fixed: a local SQLite store created by older code (missing columns) crashed every page; the store now detects schema drift and rebuilds a SQLite dev store (Postgres refuses to start and asks for a migration). The store records which dataset it holds, so changing `ATTEST_DATASET` re-imports instead of serving the old population.
- Fixed: dataset replacement on Postgres took 5 minutes (per-row FK checks without an index on `gap_documents.doc_id`); now 0.1 s.

### Phase 1: store (2026-10-10)
- Postgres via SQLAlchemy Core (SQLite for tests); the dataset, findings, decisions, records, and leases all in the store with `schema_version`.
- Tranches on members and gaps; `member_identity` across sources; queue filter by tranche.
- Review leases: opening a gap takes a 15-minute lease; others see it read-only; a decision releases it.
- **Fixed v1 defect: duplicate Accept.** v1 had no lock and no check for an existing record, so two reviewers could both accept the same gap and create two records. v2 refuses a second decision once a record exists (`AlreadyDecided`) and refuses decisions from anyone but the lease holder (`Locked`).
- Import of a v1 state file (`seed import-v1`), rows kept at schema version 1.

### Phase 0: freeze v1 (2026-10-11)
- Tag `v1.0-demo`; the v1 service pinned; deploys retargeted to the v2 service.

## v1.0-demo (2026-10-08)
The first complete build (2026-10-08). Three measures (CBP, GSD, COL-E) plus exclusions on 68 synthetic gaps; review queue, eval panel, insight view; held-out eval gate in CI; hosted behind a shared password.
