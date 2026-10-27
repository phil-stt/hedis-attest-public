"""Phase 6: stratified metrics with minimum-n rules, the per-tranche gate, and the panels that show them."""

import importlib

import pytest
from conftest import _fresh_db
from fastapi.testclient import TestClient

from attest import eval as ev


def row(tranche, expected, predicted, wrong=False, skipped=False, frag=None, src="primary"):
    return {"gap_id": f"{tranche}-{expected}-{predicted}", "tranche": tranche, "lob": tranche[:3], "measure": "CBP",
            "expected": expected, "predicted": predicted, "wrong_value": wrong, "router_skipped": skipped,
            "fragmentation": frag or ["none"], "evidence_source": src, "doc_types": ["progress_note"]}


def test_stratified_rates_min_n_and_router_losses():
    rows = [row("A", "numerator", "numerator") for _ in range(9)] + [row("A", "abstain", "abstain")]
    rows += [row("B", "numerator", "numerator") for _ in range(3)] + [row("B", "numerator", "exclusion", wrong=True)]
    rows += [row("B", "numerator", "none", skipped=True), row("B", "none", "none", skipped=True)]
    by = {s["key"]: s for s in ev.stratified(rows, "tranche")}
    a, b = by["A"], by["B"]
    assert a["n"] == 10 and not a["below_min_n"] and a["wrong_values"] == 0 and a["numerator_precision"]["k"] == 9
    assert a["accuracy"]["value"] == 1.0 and a["accuracy"]["lb"] == ev.lower_bound_95(10, 10)
    assert b["n"] == 6 and b["below_min_n"] and b["wrong_values"] == 1
    assert b["router_skipped"] == 2 and b["router_lost"] == 1
    assert b["actionable_recall"]["k"] == 3 and b["actionable_recall"]["n"] == 5
    # Multi-valued strata count a row in each of its values.
    rows2 = [row("A", "none", "none", frag=["cross_provider", "duplicate_document"]), row("A", "none", "none")]
    keys = {s["key"]: s["n"] for s in ev.stratified(rows2, "fragmentation")}
    assert keys == {"cross_provider": 1, "duplicate_document": 1, "none": 1}
    m = ev.metrics([{**r, "category": "positive", "field_mismatches": [], "span_overlaps_label": None,
                     "downgraded_by_rules": None, "expected_reason": None, "predicted_reason": None} for r in rows])
    assert m["max_wrong_values_in_a_tranche"] == 1 and set(m["strata"]) == set(ev.STRATA)


def fake_run(strata_tranche):
    def run(split, workers, only=None, router=None):
        return {"run_id": "t", "timestamp": "t", "split": split, "model": "m", "effort": "high", "prompt_versions": {},
                "rows": [], "usage": {"tokens": {}, "models_served": {}, "estimated_cost_usd": 0},
                "metrics": {"wrong_value_count": sum(s["wrong_values"] for s in strata_tranche),
                            "per_type": {"numerator": {"precision": 1.0}}, "strata": {"tranche": strata_tranche}}}
    return run


def test_per_stratum_gate_fails_on_a_wrong_value_in_any_tranche(monkeypatch):
    monkeypatch.setattr(ev, "report", lambda r: "")
    clean = [{"key": "A", "wrong_values": 0}, {"key": "B", "wrong_values": 0}]
    monkeypatch.setattr(ev, "run", fake_run(clean))
    assert ev.main(["--split", "heldout", "--no-save", "--gate-max-wrong-values-per-stratum", "0"]) == 0
    monkeypatch.setattr(ev, "run", fake_run([{"key": "A", "wrong_values": 0}, {"key": "B", "wrong_values": 1}]))
    # Even with a loose global cap, the tranche check fails the gate.
    assert ev.main(["--split", "heldout", "--no-save", "--gate-max-wrong-values", "5",
                    "--gate-max-wrong-values-per-stratum", "0"]) == 1


def test_ci_workflow_gates_per_stratum():
    from pathlib import Path
    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml").read_text()
    assert f"--gate-max-wrong-values-per-stratum {ev.GATE_MAX_WRONG_VALUES_PER_STRATUM}" in wf


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_PASSWORD", "pw")
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import app as app_mod
    from attest import data, seed, store
    for m in (store, data, seed, app_mod):
        importlib.reload(m)
    monkeypatch.setattr(app_mod, "HELDOUT_SPLIT", "v2-heldout")  # config reads the dataset at first import
    with TestClient(app_mod.app) as c:
        yield c


def test_eval_panel_shows_strata_for_a_run_saved_before_phase_6(client):
    # The committed run of record predates stratified rows; the panel rebuilds strata from the labels.
    page = client.get("/eval", auth=("nurse.a", "pw")).text
    assert "By stratum" in page and "Evidence source" in page and "Line of business" in page
    assert "Medicaid" in page and "secondary" in page
    assert "n&lt;10" in page  # some strata are small, and say so


def test_insights_yield_by_tranche(client):
    page = client.get("/insights", auth=("nurse.a", "pw")).text
    assert "Yield by tranche" in page and "Medicaid" in page
    page = client.get("/insights?tranche=TR-MCD-OON", auth=("nurse.a", "pw")).text
    assert "Yield by tranche" in page  # the all-tranche table stays while the rest is filtered
