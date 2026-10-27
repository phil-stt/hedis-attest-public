# Attest v2: test walkthroughs

One scripted path per operation: starting state, the clicks, and what should appear afterwards, on which page.
Written for a hosted instance with `DEMO_PASSWORD` set; the same paths work on a local
`make run` (no password there; any username works).

**Signing in as two reviewers.** The Basic-auth username is the reviewer identity; the password is the demo
password (not written here). Use `nurse.a` in one browser and `nurse.b` in a private/incognito window (or a second
browser): a browser keeps one set of credentials per site, so two windows of the same browser are the same reviewer.
The top bar shows who you are and which arm you're in.

**Clean start.** On the Review queue, the link at the bottom, *Reset reviews*, deletes every decision, record, lease,
dealt set, QA audit, and exposure, for every reviewer. Findings, the dataset, variants, and pipeline jobs stay. It
works the same on a hosted Postgres store as on local SQLite (one transaction, child tables first). Run it before
the rehearsal and again after.

---

## 1. Accept a finding

- Start: `nurse.a`, queue filtered to *Finding ready* (any tranche).
- Clicks: open a row whose status is *Finding ready* → read the highlighted evidence and the fields → **Accept**.
- After: the page shows *Record created* with the JSON record (quotes with offsets, fields, model and prompt version,
  variant, surface, reviewer). The decision card is gone. Queue: the row disappears from *Open* and shows under *All*
  as *Accepted*; *Accepted today* counter +1. `/records.json` has the record.
- Also: open the same gap again and try to Accept via the URL: nothing to click; a second Accept is impossible.

## 2. Reject a finding with a reason (and see it land in QA)

- Start: `nurse.a`, a different *Finding ready* row.
- Clicks: open it → in the Reject row pick a reason (e.g. *Wrong date*) → **Reject**.
- After: status *Rejected*; no record. **QA** page (`/qa`, as `nurse.b`): the gap is listed with *reject · Wrong date*
  by `nurse.a`, with Uphold / Overturn buttons. (Every rejection of a proposed finding is sampled; as `nurse.a` the
  row says "your decision; someone else audits it".) **Experiments**: the arm's *Catches (unaudited / confirmed)*
  shows 1 / 0.

## 3. Resolve an abstain

- Start: `nurse.a`, filter the queue to rows marked *Abstained*.
- Clicks: open one → the finding shows the abstain reason and what Attest considered → **Accept** is disabled
  ("Nothing to accept") → either Reject with *Evidence insufficient* (nothing usable) or Needs more.
- After: queue status *Rejected* (or *Needs more*); **Experiments** → *Abstains resolved* +1 for the arm;
  **Supervisor** → *Abstains resolved* +1 for `nurse.a`. No QA audit (abstains aren't proposals).

## 4. Mark needs-more

- Start: `nurse.a`, a *No evidence found* row.
- Clicks: open it → type a note ("request 2026 lab report from PCP") → **Needs more**.
- After: status *Needs more*; the decision card stays open (needs-more is not final: the gap can be decided later);
  the note is on the decision. Insights → *Chase worthiness* is unchanged (that table is for gaps with no chart at all).

## 5. As a second reviewer, uphold a rejection in QA (confirmed catch)

- Start: walkthrough 2 done. Sign in as `nurse.b`.
- Clicks: **QA** → the rejected proposal row → open the gap link in a new tab, read the chart → back on QA, optional
  note → **Uphold**.
- After: the row leaves *Pending* and shows under *All* as *upheld by nurse.b*. **Supervisor** (the gap's tranche or
  All): *Confirmed catches* +1 ("of 1 audited rejections"). **Experiments**: the arm's catches show 1 / 1.
- Also: as `nurse.a`, the Uphold/Overturn buttons never appear on your own decisions; posting anyway returns 409.

## 6. Overturn an accept in QA (audited error rate moves)

- Start: `nurse.a` accepts findings until one is sampled for QA (one in five, chosen by a stable hash of the gap, so
  keep accepting *Finding ready* rows; the QA page as `nurse.b` shows it when it lands).
- Clicks: as `nurse.b`, **QA** → the *accept* row → note ("date is outside the measurement year") → **Overturn**.
- After: **Supervisor** → *Audited error rate* becomes 100% (1 of 1 audited accepts) with its n; **Experiments** →
  the arm's *QA-audited error rate* shows 100% (1/1, LB 0.03). The record itself is not deleted: the audit is the
  signal, and the row carries the auditor, verdict, and note.

## 7. Deal a daily set, work two gaps, open the stretch set

- Start: `nurse.a`, **Experiments** → *Your arm* → choose *Daily set* → **Switch arm** (the top bar now says
  `daily_set (override)`). Or just go to `/today`: the page works in any arm.
- Clicks: **Today's set** → pick a tranche → **Deal 40**. The list is mixed: findings, abstains, nothing-found rows
  interleaved. **Continue →** opens the first undecided gap; the crumb reads "Today's set / … · 1 of 40 · 0 done" and
  the button says **Next in set →**. Decide it; **Next in set →**; decide the second.
- After: `/today` shows *2 of 40 decided*, the bar at 5%, tally tiles updated (an accept, a catch or a resolved
  abstain). The stretch set appears only when all 40 are decided: decide the rest (or reset and deal a set in a
  small tranche) → *Set complete* → **Deal a stretch set of 10** → a second set of 10 that shares no gap with the
  first; `/today` says *set 2 today*.
- Also: as `nurse.b`, **Deal 40** from the same tranche never includes a gap in `nurse.a`'s unfinished set.

## 8. Open the same gap as two usernames and see the lease

- Start: an undecided *Finding ready* gap G. `nurse.a` in one window, `nurse.b` in a private window.
- Clicks: as `nurse.a`, open `/gap/G` (this takes a 15-minute lease). As `nurse.b`, open `/gap/G`.
- After: `nurse.b` sees *In review: nurse.a is reviewing this gap (lease until …). Read-only until they decide or the
  lease expires.* and no decision card. The queue shows "· nurse.a" next to the status for that row. As `nurse.a`,
  decide the gap: the lease is released with the decision; `nurse.b` reloads and sees the decided state.
- Also: posting a decision as `nurse.b` while the lease is held returns 409 *Under review by nurse.a*.

## 9. Switch arm by override and confirm the exposure log marks it

- Start: any reviewer. Note the arm in the top bar (assigned by a hash of the username).
- Clicks: **Experiments** → *Your arm* → pick an arm you are not in → **Switch arm**. Visit **Review queue** once.
- After: top bar shows the new arm with *(override)*. **Experiments** → *Surface arms*: exposures for that arm +1.
  The exposure row carries `override = 1` (visible in the store; the page's footnote says overrides never count as
  assigned traffic). *Clear override (use assignment)* → **Switch arm** puts you back; the cookie is deleted.

## 10. Reset

- Start: anything above done.
- Clicks: **Review queue** → bottom link *Reset reviews (clears decisions, records, leases, sets, QA audits,
  exposures; keeps findings)* → confirm the dialog.
- After: queue counters back to the dealt state (0 accepted today, 0 decisions recorded); `/today` offers *Deal a
  set*; `/qa` says *Nothing to audit*; `/supervise` shows no decisions today; **Experiments** arms show 0 exposures
  and 0 decisions, and the variants table is unchanged. Findings and the Eval page are untouched.
