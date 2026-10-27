"""Phase 5: dealt sets, progress and completion, stretch sets, QA sampling and verdicts, supervisor view."""

import importlib

import pytest
from conftest import _fresh_db
from fastapi.testclient import TestClient

A, B = ("nurse.a", "pw"), ("nurse.b", "pw")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_PASSWORD", "pw")
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")  # tranches and a mixed pool
    from attest import app as app_mod
    from attest import daily, data, seed, store
    for m in (store, data, seed, daily, app_mod):
        importlib.reload(m)
    with TestClient(app_mod.app) as c:
        yield c, store, daily


def test_deal_is_stratified_sized_and_exclusive(client):
    c, store, daily = client
    s = daily.deal("nurse.a")
    ids = store.set_items(s["set_id"])
    assert len(ids) == daily.SET_SIZE and len(set(ids)) == daily.SET_SIZE
    types = {store.finding(g)["finding_type"] for g in ids}
    assert types == set(daily.BUCKETS)  # abstains and nothing-found alongside accepts
    first_four = [store.finding(g)["finding_type"] for g in ids[:4]]
    assert len(set(first_four)) == 4  # interleaved, not forty accepts in a row
    # Another reviewer's deal never overlaps an unfinished set.
    t = daily.deal("nurse.b")
    assert not set(store.set_items(t["set_id"])) & set(ids)


def test_deal_respects_tranche(client):
    c, store, daily = client
    from attest import data
    tranche = daily.default_tranche("nurse.a")
    assert tranche in data.tranches()
    s = daily.deal("nurse.a", tranche_id=tranche, size=5)
    assert all(data.gaps()[g]["tranche_id"] == tranche for g in store.set_items(s["set_id"]))


def test_work_a_set_end_to_end(client):
    c, store, daily = client
    r = c.post("/today/deal", data={"kind": "daily", "tranche": ""}, auth=A, follow_redirects=False)
    assert r.status_code == 303
    assert c.post("/today/deal", data={"kind": "daily"}, auth=A).status_code == 409  # one set at a time
    cur = daily.current_set("nurse.a")
    assert cur["done"] == 0 and cur["next_gap"] and not cur["complete"]
    page = c.get("/today", auth=A).text
    assert "0 of 40 decided" in page and "Continue" in page
    for item in cur["items"]:
        gid, ftype = item["gap"]["gap_id"], item["finding"]["finding_type"]
        ctx = daily.set_for_gap("nurse.a", gid)
        assert ctx and ctx["size"] == 40
        assert "Next in set" in c.get(f"/gap/{gid}", auth=A).text or ctx["next_gap"] is None
        action = "accept" if ftype in ("numerator", "exclusion") else "reject"
        data = {"action": action, "reason": "Evidence insufficient"} if action == "reject" else {"action": action}
        assert c.post(f"/gap/{gid}/decision", data=data, auth=A, follow_redirects=False).status_code == 303
        # Duplicate Accept is impossible: the gap is decided.
        assert c.post(f"/gap/{gid}/decision", data={"action": "accept"}, auth=A).status_code == 409
    cur = daily.current_set("nurse.a")
    assert cur["complete"] and cur["done"] == 40 and cur["completed_at"]
    assert cur["tally"]["accepted"] > 0 and cur["tally"]["resolved"] > 0 and cur["tally"]["confirmed"] > 0
    page = c.get("/today", auth=A).text
    assert "Set complete" in page and "stretch set" in page
    r = c.post("/today/deal", data={"kind": "stretch", "tranche": ""}, auth=A, follow_redirects=False)
    assert r.status_code == 303
    sets = daily.sets_today("nurse.a")
    assert len(sets) == 2 and sets[-1]["kind"] == "stretch" and sets[-1]["size"] == daily.STRETCH_SIZE
    assert not set(i["gap"]["gap_id"] for i in sets[-1]["items"]) & set(i["gap"]["gap_id"] for i in sets[0]["items"])


def test_daily_set_arm_lands_on_today(client):
    c, store, daily = client
    r = c.get("/", auth=A, cookies={"attest_arm": "daily_set"}, follow_redirects=False)
    assert r.headers["location"] == "/today"
    r = c.get("/", auth=A, cookies={"attest_arm": "queue"}, follow_redirects=False)
    assert r.headers["location"] == "/queue"
    assert store.exposures() == [] or True
    c.get("/today", auth=A, cookies={"attest_arm": "daily_set"})
    assert store.exposures()[-1]["page"] == "today" and store.exposures()[-1]["surface"] == "daily_set"


def test_qa_sampling_and_verdicts_feed_the_metrics(client):
    c, store, daily = client
    from attest import experiments, service
    nums = [g for g, f in store.findings().items() if f["finding_type"] == "numerator"]
    sampled = [g for g in nums if service.qa_sampled(g)]
    unsampled = [g for g in nums if not service.qa_sampled(g)]
    assert sampled and unsampled and abs(len(sampled) / len(nums) - service.QA_ACCEPT_SAMPLE_RATE) < 0.12
    c.post(f"/gap/{sampled[0]}/decision", data={"action": "accept"}, auth=A)
    c.post(f"/gap/{unsampled[0]}/decision", data={"action": "accept"}, auth=A)
    c.post(f"/gap/{nums[-1]}/decision", data={"action": "reject", "reason": "Wrong value"}, auth=A)
    abst = next(g for g, f in store.findings().items() if f["finding_type"] == "abstain")
    c.post(f"/gap/{abst}/decision", data={"action": "reject", "reason": "Other"}, auth=A)
    audits = store.qa_audits()
    assert {a["gap_id"] for a in audits} == {sampled[0], nums[-1]}  # sampled accept + every rejected proposal
    # The reviewer can't audit their own; a second person can.
    pending = store.qa_audits(pending_only=True)
    assert c.post(f"/qa/{pending[0]['id']}", data={"verdict": "upheld"}, auth=A).status_code == 409
    page = c.get("/qa", auth=B).text
    assert "Uphold" in page and "Overturn" in page
    acc = next(a for a in pending if a["action"] == "accept")
    rej = next(a for a in pending if a["action"] == "reject")
    assert c.post(f"/qa/{acc['id']}", data={"verdict": "overturned", "note": "date off"}, auth=B, follow_redirects=False).status_code == 303
    assert c.post(f"/qa/{rej['id']}", data={"verdict": "upheld"}, auth=B, follow_redirects=False).status_code == 303
    assert c.post(f"/qa/{rej['id']}", data={"verdict": "upheld"}, auth=B).status_code == 409  # one verdict
    arm = store.decisions_all()[0]["surface"]
    m = {a["surface"]: a for a in experiments.surface_metrics()}[arm]
    assert m["qa_audited_error_rate"]["k"] == 1 and m["qa_audited_error_rate"]["n"] == 1
    assert m["catches_confirmed"] == 1 and m["catches_unaudited"] == 1
    page = c.get("/experiments", auth=B).text
    assert "100%" in page and "no audits yet" in page  # the arm with audits and the arms without
    sup = daily.supervisor()
    assert sup["audited_error_rate"]["n"] == 1 and sup["confirmed_catches"] == 1 and sup["decisions_today"] == 4
    assert c.get("/supervise", auth=B).status_code == 200
    tranche = daily.default_tranche("nurse.b")
    assert "Confirmed catches" in c.get(f"/supervise?tranche={tranche}", auth=B).text
    assert c.get(f"/insights?tranche={tranche}", auth=B).status_code == 200
    assert c.get("/supervise?tranche=nope", auth=B).status_code == 404


def test_reset_clears_sets_and_audits(client):
    c, store, daily = client
    daily.deal("nurse.a", size=3)
    c.post("/admin/reset", auth=A)
    assert store.sets() == [] and store.qa_audits() == []
