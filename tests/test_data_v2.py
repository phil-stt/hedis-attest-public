"""The v2 population: bundle import, label integrity, strata, determinism."""

import importlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from conftest import _fresh_db

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "data" / "v2" / "eval-block.jsonl.gz"


@pytest.fixture()
def v2(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import data, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    counts = seed.import_bundle(BUNDLE)
    return data, store, counts


def test_eval_block_imports_whole(v2):
    data, store, counts = v2
    assert counts["gaps"] == 408 and len(data.gaps()) == 408
    assert counts["identities"] > counts["members"]  # some members are known to a second source
    assert {t["kind"] for t in data.tranches().values()} == {"line_of_business", "provider_group"}
    children = [t for t in data.tranches().values() if t["kind"] == "provider_group"]
    assert all(t["parent_id"] in data.tranches() for t in children)  # every provider group has a line of business
    assert all(g["tranche_id"].startswith("TR-") for g in data.gaps().values())
    assert all(m["region"] for m in data.members().values())
    excluded = [t for t in data.tranches().values() if t["excluded_reason"]]
    assert {t["tranche_id"] for t in excluded} == {"LOB-MIL", "TR-MIL-ALL"}


def test_held_out_strata_are_complete(v2):
    data, *_ = v2
    ho = data.labels("v2-heldout")
    v1 = [lb for lb in ho if lb["measure"] in ("CBP", "GSD", "COL-E")]
    strata = Counter((lb["tranche_id"], lb["measure"], lb["category"]) for lb in v1)
    assert len(strata) == 8 * 3 * 4 and set(strata.values()) == {1}
    new = [lb for lb in ho if lb["measure"] in ("TRC", "KED")]
    key = lambda lb: (lb["measure"] + (":" + lb["indicator"] if lb["indicator"] else ""), lb["category"])  # noqa: E731
    assert len(new) == 40 and set(Counter(key(lb) for lb in new).values()) == {2}
    assert all(lb["indicator"] in ("NIA", "RDI", "PED", "MRP") for lb in new if lb["measure"] == "TRC")
    dev = data.labels("v2-dev")
    assert Counter((lb["tranche_id"], lb["measure"], lb["category"]) for lb in dev if lb["measure"] in ("CBP", "GSD", "COL-E")).most_common(1)[0][1] == 2
    assert not {lb["member_id"] for lb in ho} & {lb["member_id"] for lb in dev}


def test_labels_match_stored_documents(v2):
    data, *_ = v2
    for split in ("v2-dev", "v2-heldout"):
        for lb in data.labels(split):
            gap = data.gaps()[lb["gap_id"]]
            assert gap["document_ids"], lb["gap_id"]
            ev = lb["evidence"]
            evs = ev if isinstance(ev, list) else ([ev] if ev else [])
            for e in evs:
                assert e["doc_id"] in gap["document_ids"]
                assert data.document_text(e["doc_id"])[e["start"]:e["end"]] == e["text"]
            if lb["expected_finding"] in ("numerator", "exclusion"):
                assert lb["expected_fields"] and evs


def test_secondary_source_documents_use_their_own_identifier(v2):
    data, store, _ = v2
    secondary = [d for d in data.documents().values() if not d["source_id"].startswith("src-ehr")]
    assert secondary
    identities = {(r["source_id"], r["source_member_ref"]): r["member_id"] for r in store.all_rows("member_identity")}
    for d in secondary:
        assert d["source_member_ref"] != d["member_id"]
        assert identities[(d["source_id"], d["source_member_ref"])] == d["member_id"]
        assert d["member_id"] not in data.document_text(d["doc_id"])  # the plan id is not on the page
    # For an independent practice the fax inbox is the primary source: even primary docs carry a FAX reference.
    ind_gap = next(g for g in data.gaps().values() if g["tranche_id"] == "TR-MCD-IND" and g["document_ids"])
    assert all(not data.documents()[d]["source_id"].startswith("src-ehr") for d in ind_gap["document_ids"])
    assert data.identities()[ind_gap["member_id"]]


def test_generator_is_deterministic(tmp_path):
    env = {"PYTHONHASHSEED": "0"}
    out = tmp_path / "gen"
    out.mkdir()
    code = ("import sys, json; sys.argv=['x']; sys.path.insert(0, 'scripts'); import generate_data_v2 as g; "
            "b = g.Builder(g.SEED); g.eval_block(b); "
            "print(json.dumps([len(b.gaps), len(b.documents), b.gaps[0]['gap_id'], b.documents[-1]['text'][:60]]))")
    runs = [subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**env, "PATH": ""}, capture_output=True,
                           text=True, check=True).stdout for _ in range(2)]
    assert runs[0] == runs[1]
    assert json.loads(runs[0])[0] == 408


def test_bundle_and_committed_labels_are_in_sync(v2):
    """Regenerating must reproduce the committed held-out labels exactly (nobody tunes them)."""
    data, *_ = v2
    committed = json.loads((ROOT / "data" / "labels" / "v2-heldout.json").read_text())
    assert committed == data.labels("v2-heldout")


def test_eval_labels_mark_secondary_relative_to_the_tranche_primary(v2):
    data, *_ = v2
    primary = {"OWN": "src-ehr", "IPA": "src-ehr", "IND": "src-fax", "OON": "src-hie"}
    for lb in data.labels("v2-heldout"):
        if lb["evidence"]:
            first = lb["evidence"][0] if isinstance(lb["evidence"], list) else lb["evidence"]
            src = data.documents()[first["doc_id"]]["source_id"]
            kind = lb["tranche_id"].split("-")[-1]
            assert (lb["evidence_source"] == "primary") == src.startswith(primary[kind]), lb["gap_id"]


def test_findings_snapshot_never_crosses_datasets(tmp_path, monkeypatch):
    """The v1 snapshot must not load into a v2 population (its gap ids don't exist there)."""
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import data, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    assert seed.snapshot_path().name == "eval-block.findings.json"
    n = seed.load_snapshot()
    assert n == 408 and len(data.gaps()) == 408
    assert set(store.findings()) <= set(data.gaps())  # only this population's gaps; nothing from v1's snapshot
    assert not any(g.startswith("GAP-") for g in store.findings())


def test_labels_pass_the_rule_checks_as_findings(v2):
    """Every labeled numerator/exclusion, turned into a finding with its own fields and quotes, must pass
    the deterministic checks; every label the generator calls abstain/none must not be a passing finding.
    This catches generator-vs-rules disagreement with no model call."""
    from attest import registry, rules

    data, *_ = v2
    for split in ("v2-dev", "v2-heldout"):
        for lb in data.labels(split):
            gap = data.gaps()[lb["gap_id"]]
            evs = lb["evidence"] if isinstance(lb["evidence"], list) else ([lb["evidence"]] if lb["evidence"] else [])
            if lb["expected_finding"] not in ("numerator", "exclusion"):
                continue
            finding = {"finding_type": lb["expected_finding"], "abstain_reason": None, "fields": dict(lb["expected_fields"]),
                       "evidence": [{"doc_id": e["doc_id"], "quote": e["text"]} for e in evs],
                       "confidence": 1.0, "rationale": "", "candidates_considered": []}
            spec = registry.spec_for_gap(gap)
            docs = {d: data.documents()[d] for d in gap["document_ids"]}
            texts = {d: data.document_text(d) for d in gap["document_ids"]}
            out = rules.apply(spec, finding, data.members()[gap["member_id"]], docs, texts)
            failed = [c for c in out["rule_checks"] if not c["passed"]]
            assert out["finding_type"] == lb["expected_finding"], (lb["gap_id"], lb["scenario"], failed)
