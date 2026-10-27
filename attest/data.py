"""Read access to the dataset: tranches, members, providers, gaps, documents.

Backed by the store (Postgres or SQLite). The dataset is imported from data/*.json
by `python -m attest.seed import-data`. Labels stay as files; they are eval artifacts.

Results are cached per process; `clear_cache()` after an import.
"""

import json
import threading
from functools import cache
from typing import Dict, List

from . import store
from .config import DATA_DIR

_ensured = False
_ensure_lock = threading.Lock()


def _ensure() -> None:
    """A fresh store (clean clone, CI) gets the dataset imported on first use, so the eval and the
    app work without a separate import step. Serialized: the eval's worker threads all arrive here at
    once on a clean checkout, and create_all is not safe to run concurrently."""
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        store.init()
        from . import seed  # local import: seed imports this module

        if not store.dataset_loaded() or store.get_meta("dataset") != seed.dataset_tag():
            # Empty store, or ATTEST_DATASET points at a different population than the one loaded.
            seed.import_data()
        _ensured = True


@cache
def members() -> Dict[str, dict]:
    _ensure()
    return {m["member_id"]: m for m in store.all_rows("members")}


@cache
def providers() -> Dict[str, dict]:
    _ensure()
    return {p["provider_id"]: p for p in store.all_rows("providers")}


@cache
def tranches() -> Dict[str, dict]:
    _ensure()
    return {t["tranche_id"]: t for t in store.all_rows("tranches")}


@cache
def gaps() -> Dict[str, dict]:
    _ensure()
    links: Dict[str, list] = {}
    for gd in sorted(store.all_rows("gap_documents"), key=lambda r: (r["gap_id"], r["position"])):
        links.setdefault(gd["gap_id"], []).append(gd["doc_id"])
    return {g["gap_id"]: {**g, "document_ids": links.get(g["gap_id"], [])} for g in store.all_rows("gaps")}


@cache
def documents() -> Dict[str, dict]:
    _ensure()
    return {d["doc_id"]: {k: v for k, v in d.items() if k != "text"} for d in store.all_rows("documents")}


@cache
def identities() -> Dict[str, list]:
    """member_id -> [{source_id, source_member_ref}], the member's identity across sources."""
    _ensure()
    out: Dict[str, list] = {}
    for r in sorted(store.all_rows("member_identity"), key=lambda r: (r["member_id"], r["source_id"])):
        out.setdefault(r["member_id"], []).append({"source_id": r["source_id"], "source_member_ref": r["source_member_ref"]})
    return out


@cache
def _texts() -> Dict[str, str]:
    _ensure()
    return {d["doc_id"]: d["text"] for d in store.all_rows("documents")}


def document_text(doc_id: str) -> str:
    return _texts()[doc_id]


def clear_cache() -> None:
    global _ensured
    _ensured = False
    for f in (members, providers, tranches, gaps, documents, identities, _texts):
        f.cache_clear()


def labels(split: str) -> List[dict]:
    path = DATA_DIR / "labels" / f"{split}.json"
    if not path.exists():
        raise ValueError(f"unknown split {split!r}: no {path}")
    return json.loads(path.read_text())
