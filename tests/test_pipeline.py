"""Batch pipeline: idempotent resume, cost accounting, router skip + QA sampling, per-tranche report."""

import importlib

import pytest
from conftest import _fresh_db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import data, pipeline, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    data.gaps()
    return pipeline, store, data


def fake_extract_factory(calls: list, finding_type="numerator"):
    def extract(gap_id, model=None):
        calls.append(gap_id)
        return {"finding_type": finding_type, "abstain_reason": None, "fields": {}, "evidence": [],
                "evidence_spans": [{"doc_id": "D", "start": 0, "end": 1}], "confidence": 0.9, "rationale": "",
                "candidates_considered": [], "rule_checks": [],
                "call": {"model_served": "claude-opus-5-5", "prompt_version": "x",
                         "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 500,
                                   "cache_creation_input_tokens": 0}}}
    return extract


def fake_route_factory(skip_ids: set, calls: list):
    def route(gap_id, model):
        calls.append(gap_id)
        return {"worth_reading": gap_id not in skip_ids, "reason": "test",
                "call": {"model_served": model, "usage": {"input_tokens": 200, "output_tokens": 20,
                                                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
    return route


def test_run_is_idempotent_and_resumes(env):
    pipeline, store, data = env
    calls = []
    rep = pipeline.run(tranche="TR-IMA-OWN", limit=5, workers=2, rpm=100000, extract=fake_extract_factory(calls),
                       progress=lambda *_: None)
    assert rep["total"]["done"] == 5 and len(calls) == 5
    assert len(store.findings()) == 5 and all(f.get("variant_id") for f in store.findings().values())
    # Same variant again: nothing left to do for those gaps; the next five are picked up.
    calls2 = []
    pipeline.run(tranche="TR-IMA-OWN", limit=5, workers=2, rpm=100000, extract=fake_extract_factory(calls2),
                 progress=lambda *_: None)
    assert len(calls2) == 5 and not set(calls2) & set(calls)


def test_cost_and_cache_accounting(env):
    pipeline, store, _ = env
    rep = pipeline.run(tranche="TR-IMA-OWN", limit=2, workers=1, rpm=100000, extract=fake_extract_factory([]),
                       progress=lambda *_: None)
    t = rep["by_tranche"]["TR-IMA-OWN"]
    # 1000 input @ $4 + 100 output @ $20 + 500 cache read @ $0.20 = 0.004 + 0.002 + 0.0001 per gap
    assert abs(t["cost_usd"] - 2 * 0.0061) < 1e-6 and t["cache_hit_share"] == round(1000 / 3000, 3)
    assert t["cost_per_gap"] == 0.0061


def test_router_skips_are_qa_sampled_and_recall_reported(env, monkeypatch):
    pipeline, store, data = env
    monkeypatch.setattr(pipeline, "QA_SAMPLE_RATE", 0.5)
    ids = pipeline.select_gaps(pipeline.variant_id("claude-haiku-4-5"), "TR-IMA-OWN", None, 20)
    skip = set(ids[:10])
    ext_calls, route_calls = [], []
    rep = pipeline.run(tranche="TR-IMA-OWN", limit=20, workers=2, rpm=100000, router="claude-haiku-4-5",
                       extract=fake_extract_factory(ext_calls, "numerator"), route=fake_route_factory(skip, route_calls),
                       progress=lambda *_: None, seed=1)
    t = rep["by_tranche"]["TR-IMA-OWN"]
    assert len(route_calls) == 20 and t["done"] == 10
    assert t["skipped"] + t["qa_samples"] == 10 and t["qa_samples"] >= 1
    # Every QA-sampled skip found a numerator (the fake extractor always does): recall on the unread bucket is 0.
    assert t["qa_missed"] == t["qa_samples"] and t["unread_bucket_recall"] == 0.0
    items = store.job_items()
    assert {r["status"] for r in items} <= {"done", "skipped_by_router", "qa_sample"}
    sampled = [r for r in items if r["status"] == "qa_sample"][0]
    assert store.finding(sampled["gap_id"])["router"] == {"model": "claude-haiku-4-5", "decision": "skip", "qa_sample": True}
    assert all(store.finding(r["gap_id"]) is None for r in items if r["status"] == "skipped_by_router")


def test_failed_items_are_recorded_and_retried_on_resume(env):
    pipeline, store, _ = env
    seen = []

    def flaky(gap_id, model=None):
        seen.append(gap_id)
        if len(seen) == 1:
            raise RuntimeError("boom")
        return fake_extract_factory([])(gap_id)

    rep = pipeline.run(tranche="TR-IMA-OWN", limit=3, workers=1, rpm=100000, extract=flaky, progress=lambda *_: None)
    assert rep["total"]["failed"] == 1 and rep["total"]["done"] == 2
    failed = [r for r in store.job_items() if r["status"] == "failed"][0]
    assert failed["error"].startswith("RuntimeError")
    rep2 = pipeline.run(job_id=rep["job_id"], workers=1, rpm=100000, extract=fake_extract_factory([]), progress=lambda *_: None)
    assert rep2["total"]["failed"] == 0 and rep2["total"]["done"] == 3


def test_router_errors_are_counted_not_hidden(env):
    pipeline, store, _ = env

    def erroring_route(gap_id, model):
        return {"worth_reading": True, "reason": "router error: BadRequestError", "error": "boom",
                "call": {"model_served": model, "usage": {}}}

    rep = pipeline.run(tranche="TR-IMA-OWN", limit=3, workers=1, rpm=100000, router="claude-haiku-4-5",
                       extract=fake_extract_factory([]), route=erroring_route, progress=lambda *_: None)
    assert rep["total"]["done"] == 3 and rep["total"]["router_errors"] == 3
    assert "ROUTER ERRORS: 3" in pipeline.format_report(rep)


def test_compare_two_variants_on_common_gaps(env, monkeypatch):
    pipeline, store, _ = env
    monkeypatch.setattr(pipeline, "QA_SAMPLE_RATE", 0.0)
    ids = pipeline.select_gaps(pipeline.variant_id(None), "TR-IMA-OWN", None, 6)
    pipeline.run(tranche="TR-IMA-OWN", limit=6, workers=1, rpm=100000, extract=fake_extract_factory([], "numerator"),
                 progress=lambda *_: None)
    skip = set(ids[:3])
    pipeline.run(tranche="TR-IMA-OWN", limit=6, workers=1, rpm=100000, router="claude-haiku-4-5",
                 extract=fake_extract_factory([], "numerator"), route=fake_route_factory(skip, []), progress=lambda *_: None)
    c = pipeline.compare(pipeline.variant_id(None), pipeline.variant_id("claude-haiku-4-5"))
    t = c["by_tranche"]["TR-IMA-OWN"]
    assert c["gaps"] == 6 and t["skipped_b"] == 3 and t["b_missed_closure"] == 3 and t["agreement"] == 0.5
    assert "B missed" in pipeline.format_compare(c)


def test_prices_tolerate_served_model_suffix_and_refuse_unknown_models():
    from attest import pipeline
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    assert pipeline.cost_usd("claude-haiku-4-5-20251001", usage) == pipeline.PRICES["claude-haiku-4-5"]["input"]
    assert pipeline.cost_usd("claude-opus-5-5", usage) == pipeline.PRICES["claude-opus-5-5"]["input"]
    with pytest.raises(KeyError):
        pipeline.cost_usd("claude-made-up-9", usage)


def test_router_cost_is_accounted_at_the_router_models_price(env, monkeypatch):
    pipeline, store, _ = env
    monkeypatch.setattr(pipeline, "QA_SAMPLE_RATE", 0.0)
    rep = pipeline.run(tranche="TR-IMA-OWN", limit=2, workers=1, rpm=100000, router="claude-haiku-4-5-20251001",
                       extract=fake_extract_factory([]), route=fake_route_factory(set(), []), progress=lambda *_: None)
    t = rep["by_tranche"]["TR-IMA-OWN"]
    # router: 200 in @ $1 + 20 out @ $5 = 0.0003 per gap; extractor as before 0.0061 per gap
    assert abs(t["router_cost_usd"] - 2 * 0.0003) < 1e-6 and abs(t["cost_usd"] - 2 * 0.0064) < 1e-6
