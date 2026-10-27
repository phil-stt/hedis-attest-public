# Screenshots

Taken against the Phase 6 build on a local server: 1440×960 CSS px at 2× (2880×1920). Every record shown is
synthetic; no real member, provider, or claims data exists anywhere in this project.

| File | Page | Shows |
|---|---|---|
| 0-queue | `/queue?tranche=…` | the review queue filtered to one tranche, in due-date order |
| 1-review-identities | `/gap/<id>` | a chart from an HIE source with the evidence highlighted; the member's identities across sources; an exclusion finding |
| 2-review-lease | `/gap/<id>` | the read-only state when another reviewer holds the lease |
| 3-measures | `/measures` | the measure registry: 14 measures, 5 active, 4 without a chart lever |
| 4-eval-by-stratum | `/eval` | the held-out run cut by tranche, line of business, evidence source, fragmentation |
| 5-insights-yield | `/insights` | yield by tranche, ranked by lower bound |
| 6-experiments | `/experiments` | the three review arms and the extraction variants |
| 7-today-daily-set | `/today` | a dealt set 14 of 40 in, with one catch |
| 8-qa | `/qa` | pending audits: a sampled accept and a rejected proposal |
| 9-supervise | `/supervise?tranche=…` | confirmed catches beside throughput, with n on every rate |
