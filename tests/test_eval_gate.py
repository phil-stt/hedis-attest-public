from attest import eval as ev


def fake_run(numerator_precision, wrong_values):
    def run(split, workers, only=None, router=None):
        return {"run_id": "t", "timestamp": "t", "split": split, "model": "m", "effort": "high",
                "prompt_versions": {}, "rows": [],
                "usage": {"tokens": {}, "models_served": {}, "estimated_cost_usd": 0},
                "metrics": {"wrong_value_count": wrong_values,
                            "per_type": {"numerator": {"precision": numerator_precision}}}}
    return run


def gate(monkeypatch, precision, wrong):
    monkeypatch.setattr(ev, "run", fake_run(precision, wrong))
    monkeypatch.setattr(ev, "report", lambda r: "")
    return ev.main(["--split", "heldout", "--no-save",
                    "--gate-min-numerator-precision", "1.0", "--gate-max-wrong-values", "0"])


def test_gate_passes_on_clean_run(monkeypatch):
    assert gate(monkeypatch, 1.0, 0) == 0


def test_gate_fails_on_any_wrong_value(monkeypatch):
    assert gate(monkeypatch, 1.0, 1) == 1


def test_gate_fails_on_precision_drop(monkeypatch):
    assert gate(monkeypatch, 0.857, 0) == 1


def test_gate_fails_when_no_numerators_predicted(monkeypatch):
    assert gate(monkeypatch, None, 0) == 1


def test_lower_bound_matches_known_values():
    assert ev.lower_bound_95(7, 7) == round(0.025 ** (1 / 7), 3)   # ~0.59
    assert ev.lower_bound_95(18, 18) == round(0.025 ** (1 / 18), 3)  # ~0.815
    assert 0.47 < ev.lower_bound_95(9, 10) < 0.57                  # Clopper-Pearson 9/10: 0.555
    assert ev.lower_bound_95(0, 5) == 0.0 and ev.lower_bound_95(0, 0) is None


def test_ci_workflow_uses_the_same_gate_as_the_panel():
    from pathlib import Path
    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml").read_text()
    assert f"--gate-min-numerator-precision {ev.GATE_MIN_NUMERATOR_PRECISION}" in wf
    assert f"--gate-max-wrong-values {ev.GATE_MAX_WRONG_VALUES}" in wf
