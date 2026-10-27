import importlib
import json
import sqlite3

import pytest
from conftest import _fresh_db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    from attest import data, seed, service, store
    importlib.reload(store)
    importlib.reload(data)
    seed.load_snapshot()
    return seed, service, store, data, tmp_path


def test_dataset_import_and_tranches(env):
    seed, service, store, data, _ = env
    assert len(data.gaps()) == 68 and len(data.members()) == 56
    assert {t["kind"] for t in data.tranches().values()} == {"provider_group"}
    assert all(g["tranche_id"] for g in data.gaps().values())
    assert len(data.gaps()["GAP-0001"]["document_ids"]) == 2
    assert data.document_text("DOC-100200-2").startswith("PROGRESS NOTE")


def test_v1_snapshot_loads_as_schema_1(env):
    _, _, store, _, _ = env
    rows = store.all_rows("findings")
    assert len(rows) == 60 and {r["schema_version"] for r in rows} == {1}


def test_second_accept_is_refused(env):
    _, service, store, _, _ = env
    gap = next(g for g, f in sorted(store.findings().items()) if f["finding_type"] == "numerator")
    service.record_decision(gap, "accept", "nurse.a")
    with pytest.raises(service.AlreadyDecided):
        service.record_decision(gap, "accept", "nurse.b")
    with pytest.raises(service.AlreadyDecided):
        service.record_decision(gap, "reject", "nurse.a", reason="Other")
    assert len(store.records(gap)) == 1
    assert store.records(gap)[0]["schema_version"] == 2


def test_lease_blocks_other_reviewer_until_released(env):
    _, service, store, _, _ = env
    gap = next(g for g, f in sorted(store.findings().items()) if f["finding_type"] == "numerator")
    service.claim(gap, "nurse.a")
    with pytest.raises(service.Locked):
        service.claim(gap, "nurse.b")
    with pytest.raises(service.Locked):
        service.record_decision(gap, "accept", "nurse.b")
    service.claim(gap, "nurse.a")  # renewing your own lease is fine
    service.record_decision(gap, "needs_more", "nurse.a", note="chase")
    assert store.active_assignment(gap) is None  # a decision releases the lease
    service.claim(gap, "nurse.b")  # now free


def test_expired_lease_is_free(env):
    _, service, store, _, _ = env
    gap = "GAP-0001"
    store.add_assignment(gap, "nurse.a", "2000-01-01T00:00:00+00:00")
    service.claim(gap, "nurse.b")


def test_import_v1_sqlite_state(env):
    seed, _, store, _, tmp_path = env
    v1 = tmp_path / "v1.db"
    c = sqlite3.connect(v1)
    c.executescript("""
    CREATE TABLE findings (gap_id TEXT PRIMARY KEY, finding_type TEXT, confidence REAL, model TEXT,
        prompt_version TEXT, created_at TEXT, body TEXT);
    CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, gap_id TEXT, action TEXT, reason TEXT,
        note TEXT, reviewer TEXT, decided_at TEXT);
    CREATE TABLE records (record_id TEXT PRIMARY KEY, gap_id TEXT, decision_id INTEGER, created_at TEXT, body TEXT);
    """)
    body = json.dumps({"finding_type": "numerator", "fields": {"systolic": 120}, "call": {"prompt_version": "cbp.v1"}})
    c.execute("INSERT INTO findings VALUES ('GAP-0001','numerator',0.9,'claude-opus-5-5','cbp.v1','2026-10-05T00:00:00+00:00',?)",
              (body,))
    c.execute("INSERT INTO decisions (id, gap_id, action, reason, note, reviewer, decided_at) VALUES (7,'GAP-0001','accept',NULL,NULL,'nurse.v1','2026-10-08T20:00:00+00:00')")
    c.execute("INSERT INTO records VALUES ('REC-V1','GAP-0001',7,'2026-10-08T20:00:00+00:00',?)",
              (json.dumps({"record_id": "REC-V1", "gap_id": "GAP-0001", "reviewer": "nurse.v1"}),))
    c.commit()
    c.close()
    counts = seed.import_v1_db(v1)
    assert counts == {"findings": 1, "decisions": 1, "records": 1}
    assert store.finding("GAP-0001")["fields"] == {"systolic": 120}
    assert store.records("GAP-0001")[0]["reviewer"] == "nurse.v1"
    rec_row = next(r for r in store.all_rows("records") if r["record_id"] == "REC-V1")
    dec_row = next(r for r in store.all_rows("decisions") if r["reviewer"] == "nurse.v1")
    assert rec_row["schema_version"] == 1 and rec_row["decision_id"] == dec_row["id"]
    assert store.latest_decisions()["GAP-0001"]["action"] == "accept"


def test_fresh_store_imports_dataset_on_first_use(tmp_path, monkeypatch):
    """The CI eval gate runs on a clean checkout with no import step; data access must self-seed."""
    _fresh_db(tmp_path, monkeypatch)
    from attest import data, store
    importlib.reload(store)
    importlib.reload(data)
    assert len(data.gaps()) == 68


def test_stale_sqlite_schema_is_rebuilt(tmp_path, monkeypatch):
    """A local store created by older code (missing columns) must not crash the app."""
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE members (member_id TEXT PRIMARY KEY, name TEXT, sex TEXT, dob TEXT, "
              "age_at_my_end INTEGER, pcp_provider_id TEXT, tranche_id TEXT)")  # no region column
    c.commit()
    c.close()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ATTEST_DB", str(db))
    from attest import data, store
    importlib.reload(store)
    importlib.reload(data)
    assert store.schema_drift() == {"members": ["region"]}
    store.init()
    assert store.schema_drift() == {}
    assert len(data.gaps()) == 68  # and the dataset imports


def test_missing_columns_are_added_in_place_and_data_survives(tmp_path, monkeypatch):
    """A code update that adds columns must not wipe a store (the population store holds paid-for findings)."""
    import sqlite3

    db = tmp_path / "old.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ATTEST_DB", str(db))
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import data, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    data.gaps()
    gap = sorted(data.gaps())[0]
    store.put_finding(gap, {"finding_type": "none", "fields": {}, "evidence": [], "call": {}})
    store.upsert_job("j", "v", {})
    store.put_job_item({"gap_id": gap, "variant_id": "v", "job_id": "j", "status": "done", "cost_usd": 0.5})
    # Simulate an older schema: drop the router_* columns from job_items.
    c = sqlite3.connect(db)
    for col in ("router_input_tokens", "router_output_tokens", "router_cache_read_tokens", "router_cost_usd"):
        c.execute(f"ALTER TABLE job_items DROP COLUMN {col}")
    c.commit()
    c.close()
    importlib.reload(store)
    assert set(store.schema_drift()) == {"job_items"}
    store.init()
    assert store.schema_drift() == {}
    assert store.finding(gap) is not None and store.job_items()[0]["cost_usd"] == 0.5  # nothing lost


def test_switching_attest_dataset_reimports(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.delenv("ATTEST_DATASET", raising=False)
    from attest import data, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    assert len(data.gaps()) == 68 and store.get_meta("dataset") == "v1"
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    data.clear_cache()
    assert len(data.gaps()) == 408 and store.get_meta("dataset").startswith("data/v2/eval-block.jsonl.gz@")


def test_export_snapshot_filters_to_a_bundle(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from pathlib import Path

    from attest import data, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    gaps = sorted(data.gaps())
    for g in gaps[:3]:
        store.put_finding(g, {"finding_type": "none", "fields": {}, "evidence": [], "call": {}})
    out = tmp_path / "snap.json"
    n = seed.export_snapshot(Path("data/v2/eval-block.jsonl.gz"), out)
    assert n == 3 and set(json.loads(out.read_text())) == set(gaps[:3])


def test_concurrent_first_use_of_a_fresh_store(tmp_path, monkeypatch):
    """The CI gate's worker threads all touch an empty store at once; initialization must be serialized."""
    from concurrent.futures import ThreadPoolExecutor

    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ATTEST_DATASET", "data/v2/eval-block.jsonl.gz")
    from attest import data, seed, store
    importlib.reload(store)
    importlib.reload(data)
    importlib.reload(seed)
    with ThreadPoolExecutor(max_workers=8) as pool:
        counts = list(pool.map(lambda _: len(data.gaps()), range(16)))
    assert counts == [408] * 16
