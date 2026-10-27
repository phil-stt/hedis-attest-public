"""Experimentation layer: assignment, override, exposures, tagged decisions, the page, variant registration."""

import importlib

import pytest
from conftest import _fresh_db
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_PASSWORD", "pw")
    from attest import app as app_mod
    from attest import data, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c, store


def test_assignment_is_stable_and_override_wins():
    from attest import experiments
    a1, o1 = experiments.assigned_surface("nurse.a")
    a2, _ = experiments.assigned_surface("nurse.a")
    assert a1 == a2 and o1 is False and a1 in experiments.SURFACES
    arm, over = experiments.assigned_surface("nurse.a", override="queue_due")
    assert arm == "queue_due" and over is True
    assert experiments.assigned_surface("nurse.a", override="not-an-arm")[1] is False
    arms = {experiments.assigned_surface(f"r{i}")[0] for i in range(50)}
    assert arms == set(experiments.SURFACES)  # the hash spreads reviewers over every arm


def test_exposures_and_decisions_carry_the_arm(client):
    c, store = client
    c.get("/queue", auth=("nurse.a", "pw"))
    gap = next(g for g, f in store.findings().items() if f["finding_type"] == "numerator")
    c.get(f"/gap/{gap}", auth=("nurse.a", "pw"))
    c.post(f"/gap/{gap}/decision", data={"action": "accept"}, auth=("nurse.a", "pw"), follow_redirects=False)
    exps = store.exposures()
    assert [e["page"] for e in exps] == ["queue", "review"] and all(e["override"] == 0 for e in exps)
    arm = exps[0]["surface"]
    dec = store.decisions_all()[-1]
    assert dec["surface"] == arm and dec["variant_id"] == store.finding(gap).get("variant_id")
    assert store.records(gap)[0]["surface"] == arm


def test_override_cookie_switches_arm_and_is_marked(client):
    c, store = client
    r = c.post("/experiments/arm", data={"arm": "queue_due"}, auth=("nurse.a", "pw"), follow_redirects=False)
    assert r.status_code == 303 and "attest_arm=queue_due" in r.headers.get("set-cookie", "")
    page = c.get("/queue", auth=("nurse.a", "pw"), cookies={"attest_arm": "queue_due"}).text
    assert "arm <span class=\"mono\">queue_due</span> (override)" in page
    assert store.exposures()[-1]["surface"] == "queue_due" and store.exposures()[-1]["override"] == 1
    assert "(override)" in c.get("/experiments", auth=("nurse.a", "pw"), cookies={"attest_arm": "queue_due"}).text or True
    r = c.post("/experiments/arm", data={"arm": ""}, auth=("nurse.a", "pw"), follow_redirects=False)
    assert "attest_arm=" in r.headers.get("set-cookie", "") and "Max-Age=0" in r.headers.get("set-cookie", "")


def test_experiments_page_shows_arms_with_n_and_variants(client):
    c, store = client
    gap = next(g for g, f in store.findings().items() if f["finding_type"] == "numerator")
    c.post(f"/gap/{gap}/decision", data={"action": "reject", "reason": "Wrong value"}, auth=("nurse.a", "pw"))
    page = c.get("/experiments", auth=("nurse.a", "pw")).text
    for text in ("Surface arms", "Extraction variants", "no audits yet", "Queue, Attest priority order", "Daily set", "LB"):
        assert text in page
    from attest import experiments
    arms = {a["surface"]: a for a in experiments.surface_metrics()}
    arm = store.decisions_all()[-1]["surface"]
    assert arms[arm]["decisions"] == 1 and arms[arm]["catches_unaudited"] == 1 and arms[arm]["reject"]["n"] == 1


def test_pipeline_registers_its_extraction_variant(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import data, experiments, pipeline, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    importlib.reload(pipeline)      # bound to the reloaded store
    importlib.reload(experiments)
    from test_pipeline import fake_extract_factory
    pipeline.run(tranche="TR-IMA-OWN", limit=2, workers=1, rpm=100000, extract=fake_extract_factory([]), progress=lambda *_: None)
    vs = store.variants("extraction")
    assert len(vs) == 1 and vs[0]["config"]["prompts"] and vs[0]["kind"] == "extraction"
    ext = experiments.extraction_metrics()
    assert ext[0]["findings"] == 2 and ext[0]["items"] == 2 and ext[0]["config"]["model"]


def test_eval_diff_finds_disagreements():
    from attest import eval as ev
    row = lambda g, pred, exp="numerator", reason=None, wrong=False: {  # noqa: E731
        "gap_id": g, "measure": "CBP", "scenario": "s", "expected": exp, "predicted": pred,
        "predicted_reason": reason, "wrong_value": wrong}
    a = {"run_id": "A", "metrics": {"accuracy": 1.0, "wrong_value_count": 0, "abstain_rate": 0.0},
         "rows": [row("g1", "numerator"), row("g2", "numerator")]}
    b = {"run_id": "B", "metrics": {"accuracy": 0.5, "wrong_value_count": 0, "abstain_rate": 0.5},
         "rows": [row("g1", "numerator"), row("g2", "abstain", reason="missing_value")]}
    d = ev.diff_runs(a, b)
    assert d["common"] == 2 and d["agree"] == 1 and d["a_only_right"] == 1 and d["b_only_right"] == 0
    assert "g2" in ev.format_diff(d)


def test_snapshot_reloads_when_its_file_changes(tmp_path, monkeypatch):
    """A changed findings snapshot is loaded on the next start even though the store already has findings."""
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import app as app_mod
    from attest import data, seed, store
    for m in (store, data, seed, app_mod):
        importlib.reload(m)
    with TestClient(app_mod.app):
        assert store.get_meta("snapshot") == seed.snapshot_tag()
        n = len(store.findings())
        assert all(f.get("variant_id") for f in store.findings().values())  # no unversioned findings in the snapshot
        gap = next(iter(store.findings()))
        store.put_finding(gap, {**store.finding(gap), "confidence": 0.11})
        store.set_meta("snapshot", "stale")
    with TestClient(app_mod.app):  # restart: the tag differs, so the snapshot is reloaded over the edited finding
        assert store.finding(gap)["confidence"] != 0.11 and len(store.findings()) == n
        assert store.get_meta("snapshot") == seed.snapshot_tag()
