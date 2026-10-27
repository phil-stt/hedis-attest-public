import importlib
import json

import pytest
from conftest import _fresh_db
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("DEMO_PASSWORD", "pw")
    from attest import app as app_mod
    from attest import store
    importlib.reload(store)
    from attest import data
    importlib.reload(data)
    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        c.auth = ("nurse.a", "pw")
        yield c, store


def numerator_gap(store):
    return next(g for g, f in store.findings().items() if f["finding_type"] == "numerator")


def abstain_gap(store):
    return next(g for g, f in store.findings().items() if f["finding_type"] == "abstain")


def test_password_required(client):
    c, _ = client
    assert c.get("/queue", auth=("x", "wrong")).status_code == 401
    assert c.get("/health", auth=None).status_code == 200
    assert c.get("/queue").status_code == 200


def test_accept_numerator_creates_record(client):
    c, store = client
    gap = numerator_gap(store)
    r = c.post(f"/gap/{gap}/decision", data={"action": "accept"}, follow_redirects=False)
    assert r.status_code == 303
    recs = store.records(gap)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["reviewer"] == "nurse.a" and rec["source_text"] and rec["prompt_version"]
    assert "Record created" in c.get(f"/gap/{gap}").text


def test_cannot_accept_an_abstain(client):
    c, store = client
    gap = abstain_gap(store)
    assert c.post(f"/gap/{gap}/decision", data={"action": "accept"}).status_code == 409
    assert store.records(gap) == []


def test_reject_and_needs_more_create_no_record(client):
    c, store = client
    gap = numerator_gap(store)
    c.post(f"/gap/{gap}/decision", data={"action": "reject", "reason": "Wrong value"})
    c.post(f"/gap/{abstain_gap(store)}/decision", data={"action": "needs_more", "note": "chase"})
    assert store.records() == []


def test_queue_and_records_endpoints(client):
    c, _ = client
    assert "Review queue" in c.get("/queue?sort=confidence&measure=CBP").text
    assert json.loads(c.get("/records.json").text) == []


def test_eval_panel_shows_wrong_values_and_lower_bounds(client):
    c, _ = client
    page = c.get("/eval").text
    assert "Wrong values" in page and "95% LB" in page and "Clopper-Pearson" in page
    assert "Gate passed" in page


def test_insights_renders_measures_providers_and_chase(client):
    c, _ = client
    page = c.get("/insights").text
    for text in ("Open gaps by measure", "isn't coded", "Chase worthiness", "GAP-00"):
        assert text in page


def test_chase_ranking_only_covers_gaps_without_charts():
    from attest import app as app_mod
    from attest import data
    d = app_mod.insight_data()
    assert d["chase"] and all(not r["gap"]["document_ids"] for r in d["chase"])
    assert [r["score"] for r in d["chase"]] == sorted((r["score"] for r in d["chase"]), reverse=True)
    assert sum(r["total"] for r in d["by_measure"]) == len(data.gaps())


def test_reset_clears_decisions_but_keeps_findings(client):
    c, store = client
    gap = numerator_gap(store)
    c.post(f"/gap/{gap}/decision", data={"action": "accept"})
    n_findings = len(store.findings())
    assert "1 decision recorded" in c.get("/queue").text
    c.post("/admin/reset")
    assert store.records() == [] and store.latest_decisions() == {}
    assert len(store.findings()) == n_findings


def test_favicon_served(client):
    c, _ = client
    assert c.get("/static/favicon.svg").status_code == 200


def test_lease_shows_holder_and_blocks_other_reviewer(client):
    c, store = client
    gap = numerator_gap(store)
    assert c.get(f"/gap/{gap}", auth=("nurse.a", "pw")).status_code == 200
    page_b = c.get(f"/gap/{gap}", auth=("nurse.b", "pw")).text
    assert "nurse.a is reviewing this gap" in page_b and "Your decision" not in page_b
    assert c.post(f"/gap/{gap}/decision", data={"action": "accept"}, auth=("nurse.b", "pw")).status_code == 409
    assert c.post(f"/gap/{gap}/decision", data={"action": "accept"}, auth=("nurse.a", "pw"),
                  follow_redirects=False).status_code == 303
    assert c.post(f"/gap/{gap}/decision", data={"action": "accept"}, auth=("nurse.a", "pw")).status_code == 409
    assert len(store.records(gap)) == 1


def test_username_is_reviewer_even_without_password(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    from attest import app as app_mod
    from attest import data, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        gap = numerator_gap(store)
        c.post(f"/gap/{gap}/decision", data={"action": "accept"}, auth=("nurse.local", "anything"))
        assert store.records(gap)[0]["reviewer"] == "nurse.local"
        assert c.get("/queue").status_code == 200  # no auth header still works locally


def test_measures_page_lists_all_14_and_says_which_have_no_chart_lever(client):
    c, _ = client
    page = c.get("/measures").text
    for code in ("CBP", "TRC", "KED", "PCR", "PDC-STA"):
        assert code in page
    assert page.count("registry only") == 4 and "Medication Reconciliation Post-Discharge" in page
