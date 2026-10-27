"""Load the dataset and Attest's findings into the store; import v1 state.

    python -m attest.seed import-data               # data/*.json -> store (tranches derived from clinics)
    python -m attest.seed import-data --bundle data/v2/eval-block.jsonl.gz   # a v2 bundle
    python -m attest.seed load                      # import-data if needed, then seed/findings.json
    python -m attest.seed extract --split dev       # live calls; updates seed/findings.json
    python -m attest.seed import-v1 state/attest.db # findings, decisions, records from a v1 SQLite file
    python -m attest.seed reset                     # clears reviewer decisions, records, and leases

The findings snapshot is committed so a container starts in a known demo state
without an API call.
"""

import argparse
import gzip
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import data, service, store
from .config import DATA_DIR, ROOT

V1_SOURCE = "src-synthetic-ehr"  # v1 data had one source, which used the member id directly


def dataset_bundle() -> Path | None:
    """The v2 bundle selected by ATTEST_DATASET, or None for the v1 data/*.json."""
    v = os.environ.get("ATTEST_DATASET", "v1")
    if v == "v1":
        return None
    return Path(v) if Path(v).is_absolute() else ROOT / v


def dataset_tag(bundle: Path | None = None) -> str:
    """Identity of the selected dataset, stored in the store's meta table: 'v1' or the bundle path."""
    bundle = bundle or dataset_bundle()
    if bundle is None:
        return "v1"
    try:
        name = str(bundle.resolve().relative_to(ROOT))
    except ValueError:
        name = str(bundle)
    st = bundle.stat()  # a regenerated bundle is a different dataset: re-import on next start
    return f"{name}@{st.st_mtime_ns}:{st.st_size}"


def snapshot_tag(bundle: Path | None = None) -> str:
    """Identity of the findings snapshot for the selected dataset; a changed file reloads on next start."""
    p = snapshot_path(bundle)
    if not p.exists():
        return "none"
    st = p.stat()
    return f"{p.name}@{st.st_mtime_ns}:{st.st_size}"


def snapshot_path(bundle: Path | None = None) -> Path:
    """Findings snapshots belong to a dataset: seed/findings.json for v1, seed/<bundle>.findings.json for a bundle."""
    bundle = bundle or dataset_bundle()
    if bundle is None:
        return ROOT / "seed" / "findings.json"
    return ROOT / "seed" / f"{bundle.name.split('.')[0]}.findings.json"


SNAPSHOT = snapshot_path()  # evaluated at import for the selected dataset


def dataset_from_json() -> dict:
    """Translate data/*.json into store rows. Tranches: one provider_group per clinic, derived from
    the data (v1 had no tranche field). Generator v2 writes tranches explicitly."""
    base = DATA_DIR
    members = json.loads((base / "members.json").read_text())
    providers = json.loads((base / "providers.json").read_text())
    gaps = json.loads((base / "gaps.json").read_text())
    documents = json.loads((base / "documents.json").read_text())
    tranches_file = base / "tranches.json"
    if tranches_file.exists():
        tranches = json.loads(tranches_file.read_text())
    else:
        clinics = sorted({p["clinic"] for p in providers})
        tranches = [{"tranche_id": f"TR-PG-{i + 1:02d}", "name": c, "kind": "provider_group", "parent_id": None,
                     "excluded_reason": None} for i, c in enumerate(clinics)]
    clinic_tranche = {t["name"]: t["tranche_id"] for t in tranches if t["kind"] == "provider_group"}
    provider_tranche = {p["provider_id"]: clinic_tranche.get(p["clinic"]) for p in providers}

    out_members = [{"member_id": m["member_id"], "name": m["name"], "sex": m.get("sex"), "dob": m["dob"],
                    "age_at_my_end": m["age_at_my_end"], "pcp_provider_id": m.get("pcp_provider_id"),
                    "tranche_id": m.get("tranche_id") or provider_tranche.get(m.get("pcp_provider_id")),
                    "region": m.get("region")}
                   for m in members]
    out_gaps = [{"gap_id": g["gap_id"], "member_id": g["member_id"], "measure": g["measure"],
                 "indicator": g.get("indicator"), "provider_id": g["provider_id"], "due_date": g["due_date"],
                 "tranche_id": g.get("tranche_id") or provider_tranche.get(g["provider_id"])} for g in gaps]
    out_docs, identities = [], {}
    for d in documents:
        src = d.get("source_id", V1_SOURCE)
        ref = d.get("source_member_ref", d["member_id"])
        out_docs.append({"doc_id": d["doc_id"], "member_id": d["member_id"], "doc_type": d["doc_type"],
                         "doc_date": d["doc_date"], "source_id": src, "source_member_ref": ref,
                         "text": (ROOT / d["path"]).read_text()})
        identities[(src, ref)] = d["member_id"]
    for m in members:  # every member is known to the primary source under their own id
        identities.setdefault((V1_SOURCE, m["member_id"]), m["member_id"])
    gap_docs = [{"gap_id": g["gap_id"], "doc_id": doc_id, "position": i}
                for g in gaps for i, doc_id in enumerate(g["document_ids"])]
    return {"tranches": tranches, "providers": providers, "members": out_members,
            "identities": [{"source_id": s, "source_member_ref": r, "member_id": m} for (s, r), m in identities.items()],
            "gaps": out_gaps, "documents": out_docs, "gap_documents": gap_docs}


def import_bundle(path: Path, batch: int = 2000) -> dict:
    """Load a v2 bundle (gzipped JSONL, {"t": table, "r": row}, table-major) in batches. Replaces the
    dataset and clears the state that referenced the old one."""
    store.init()
    counts: dict = {}
    with store.tx() as conn, gzip.open(path, "rt", encoding="utf-8") as fh:
        store.clear_dataset(conn)
        pending_table, pending = None, []
        for line in fh:
            rec = json.loads(line)
            if rec["t"] != pending_table or len(pending) >= batch:
                store.insert_rows(conn, pending_table, pending) if pending_table else None
                pending_table, pending = rec["t"], []
            pending.append(rec["r"])
            counts[rec["t"]] = counts.get(rec["t"], 0) + 1
        if pending_table:
            store.insert_rows(conn, pending_table, pending)
        store.set_meta("dataset", dataset_tag(path), conn=conn)
    data.clear_cache()
    return counts


def import_data(bundle: Path | None = None) -> dict:
    """Dataset into the store. `bundle` (or ATTEST_DATASET env, a bundle path) selects a v2 bundle;
    otherwise the v1 data/*.json. Explicit and reversible: the v1 demo and v2 never share a store."""
    bundle = bundle or dataset_bundle()
    if bundle:
        return import_bundle(bundle if bundle.is_absolute() else ROOT / bundle)
    store.init()
    d = dataset_from_json()
    store.replace_dataset(d)
    store.set_meta("dataset", "v1")
    data.clear_cache()
    return {k: len(v) for k, v in d.items()}


def _gap_ids(split: str) -> list:
    if split == "all":
        return sorted(g for g, v in data.gaps().items() if v["document_ids"])
    return sorted(lb["gap_id"] for lb in data.labels(split))


def extract_to_snapshot(split: str, workers: int = 8) -> dict:
    ids = _gap_ids(split)
    snapshot = snapshot_path()
    snap = json.loads(snapshot.read_text()) if snapshot.exists() else {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for gap_id, result in zip(ids, pool.map(service.find_evidence, ids)):
            snap[gap_id] = result
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(dict(sorted(snap.items())), indent=1) + "\n")
    return snap


def export_snapshot(only_bundle: Path | None = None, out: Path | None = None) -> int:
    """Write the store's findings to a snapshot file (the selected dataset's by default). With `only_bundle`,
    keep just the gaps of that bundle and write that bundle's snapshot: the way a population store's findings
    become the eval block's committed snapshot."""
    store.init()
    findings = store.findings()
    if only_bundle:
        keep = set()
        with gzip.open(only_bundle, "rt", encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec["t"] == "gaps":
                    keep.add(rec["r"]["gap_id"])
        findings = {g: f for g, f in findings.items() if g in keep}
    out = out or snapshot_path(only_bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(sorted(findings.items())), indent=1) + "\n")
    return len(findings)


def load_snapshot() -> int:
    """Make sure the dataset is in the store, then load the findings snapshot (v1 output, schema 1)."""
    store.init()
    if not store.dataset_loaded():
        import_data()
    snapshot = snapshot_path()
    if not snapshot.exists():
        return 0
    snap = json.loads(snapshot.read_text())
    known = set(data.gaps())
    version = 1 if dataset_bundle() is None else store.SCHEMA_VERSION  # v1's snapshot is schema 1; a bundle's is current
    n = 0
    for gap_id, finding in snap.items():
        if gap_id in known:  # a snapshot never crosses datasets
            store.put_finding(gap_id, finding, schema_version=finding.get("schema_version") or version)
            n += 1
    store.set_meta("snapshot", snapshot_tag())
    return n


V1_TABLES = {
    # The v1 SQLite schema, kept here as the import contract.
    "findings": "gap_id, finding_type, confidence, model, prompt_version, created_at, body",
    "decisions": "id, gap_id, action, reason, note, reviewer, decided_at",
    "records": "record_id, gap_id, decision_id, created_at, body",
}


def import_v1_db(path: Path) -> dict:
    """Import findings, decisions, and records from a v1 state file (SQLite). Rows keep schema_version 1;
    decision ids are remapped. Existing v2 rows for the same gaps are left in place."""
    store.init()
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    counts = {"findings": 0, "decisions": 0, "records": 0}
    with store.tx() as conn:
        for r in src.execute(f"SELECT {V1_TABLES['findings']} FROM findings"):
            body = json.loads(r["body"])
            conn.execute(store.delete(store.t_findings).where(store.t_findings.c.gap_id == r["gap_id"]))
            conn.execute(store.insert(store.t_findings), {
                "gap_id": r["gap_id"], "finding_type": r["finding_type"], "confidence": r["confidence"],
                "model": r["model"], "prompt_version": r["prompt_version"], "created_at": r["created_at"],
                "schema_version": 1, "body": body})
            counts["findings"] += 1
        id_map = {}
        for r in src.execute(f"SELECT {V1_TABLES['decisions']} FROM decisions ORDER BY id"):
            new_id = conn.execute(store.insert(store.t_decisions), {
                "gap_id": r["gap_id"], "action": r["action"], "reason": r["reason"], "note": r["note"],
                "reviewer": r["reviewer"], "decided_at": r["decided_at"], "schema_version": 1,
            }).inserted_primary_key[0]
            id_map[r["id"]] = new_id
            counts["decisions"] += 1
        for r in src.execute(f"SELECT {V1_TABLES['records']} FROM records ORDER BY created_at"):
            conn.execute(store.insert(store.t_records), {
                "record_id": r["record_id"], "gap_id": r["gap_id"], "decision_id": id_map[r["decision_id"]],
                "created_at": r["created_at"], "schema_version": 1, "body": json.loads(r["body"])})
            counts["records"] += 1
    src.close()
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import-data")
    imp.add_argument("--bundle", type=Path, help="v2 bundle (gzipped JSONL); default: v1 data/*.json or $ATTEST_DATASET")
    sub.add_parser("load")
    ex = sub.add_parser("extract")
    ex.add_argument("--split", required=True, help="a label set name, or 'all' for every charted gap")
    exp = sub.add_parser("export-snapshot")
    exp.add_argument("--only-bundle", type=Path, help="keep only this bundle's gaps and write its snapshot")
    v1 = sub.add_parser("import-v1")
    v1.add_argument("path", type=Path)
    sub.add_parser("reset")
    args = ap.parse_args(argv)
    if args.cmd == "import-data":
        print("imported", import_data(args.bundle), "->", store.DATABASE_URL)
    elif args.cmd == "load":
        print(f"loaded {load_snapshot()} findings into {store.DATABASE_URL}")
    elif args.cmd == "extract":
        load_snapshot()
        snap = extract_to_snapshot(args.split)
        print(f"snapshot has {len(snap)} findings -> {snapshot_path()}")
    elif args.cmd == "export-snapshot":
        n = export_snapshot(args.only_bundle)
        print(f"exported {n} findings -> {snapshot_path(args.only_bundle)}")
    elif args.cmd == "import-v1":
        print("imported from v1:", import_v1_db(args.path))
    else:
        store.init()
        store.reset_reviews()
        print("cleared decisions, records, and leases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
