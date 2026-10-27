"""State and dataset storage: SQLAlchemy Core over Postgres (prod, docker compose) or SQLite (tests, quick local).

Schema version 2. Every finding, decision, and record carries `schema_version`;
rows imported from v1 keep version 1. The dataset (tranches, members, providers,
gaps, documents) lives here too, imported from data/*.json by `attest.seed`.

`DATABASE_URL` selects the backend, e.g. `postgresql+psycopg://user:pw@host/db`.
With no URL, a SQLite file at `ATTEST_DB` (default state/attest.db) is used.
"""

import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB

from .config import ROOT
from .schema import normalize_finding

SCHEMA_VERSION = 2
DB_PATH = Path(os.environ.get("ATTEST_DB", ROOT / "state" / "attest.db"))
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{DB_PATH}"

metadata = MetaData()
Json = JSON().with_variant(JSONB, "postgresql")

t_tranches = Table(
    "tranches", metadata,
    Column("tranche_id", String, primary_key=True),
    Column("name", String, nullable=False),
    # business_unit | line_of_business | provider_group | region
    Column("kind", String, nullable=False),
    Column("parent_id", String, ForeignKey("tranches.tranche_id"), nullable=True),
    # Set on a line of business the gap engine skips (e.g. a military plan: not rated under Stars/HEDIS).
    Column("excluded_reason", String, nullable=True),
)
t_providers = Table(
    "providers", metadata,
    Column("provider_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("clinic", String, nullable=False),
)
t_members = Table(
    "members", metadata,
    Column("member_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("sex", String),
    Column("dob", String, nullable=False),
    Column("age_at_my_end", Integer, nullable=False),
    Column("pcp_provider_id", String, ForeignKey("providers.provider_id"), index=True),
    Column("tranche_id", String, ForeignKey("tranches.tranche_id"), index=True),
    Column("region", String, nullable=True, index=True),  # a member attribute, not a tranche
)
# A member as known to one source. Fragmented sources use their own identifiers; this is the join.
t_member_identity = Table(
    "member_identity", metadata,
    Column("source_id", String, primary_key=True),
    Column("source_member_ref", String, primary_key=True),
    Column("member_id", String, ForeignKey("members.member_id"), nullable=False, index=True),
)
t_gaps = Table(
    "gaps", metadata,
    Column("gap_id", String, primary_key=True),
    Column("member_id", String, ForeignKey("members.member_id"), nullable=False, index=True),
    Column("measure", String, nullable=False, index=True),
    Column("indicator", String, nullable=True),  # sub-indicator for measures that have them (TRC)
    Column("provider_id", String, ForeignKey("providers.provider_id"), index=True),
    Column("due_date", String, nullable=False),
    Column("tranche_id", String, ForeignKey("tranches.tranche_id"), index=True),
)
t_documents = Table(
    "documents", metadata,
    Column("doc_id", String, primary_key=True),
    Column("member_id", String, ForeignKey("members.member_id"), nullable=False, index=True),
    Column("doc_type", String, nullable=False),
    Column("doc_date", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("source_member_ref", String, nullable=False),
    Column("text", Text, nullable=False),
)
t_gap_documents = Table(
    "gap_documents", metadata,
    Column("gap_id", String, ForeignKey("gaps.gap_id"), primary_key=True),
    Column("doc_id", String, ForeignKey("documents.doc_id"), primary_key=True, index=True),
    Column("position", Integer, nullable=False),
)
t_findings = Table(
    "findings", metadata,
    Column("gap_id", String, ForeignKey("gaps.gap_id"), primary_key=True),
    Column("finding_type", String, nullable=False),
    Column("confidence", Float),
    Column("model", String),
    Column("prompt_version", String),
    Column("created_at", String, nullable=False),
    Column("schema_version", Integer, nullable=False, default=SCHEMA_VERSION),
    Column("body", Json, nullable=False),
)
t_decisions = Table(
    "decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("gap_id", String, ForeignKey("gaps.gap_id"), nullable=False, index=True),
    Column("action", String, nullable=False),  # accept | reject | needs_more
    Column("reason", String),
    Column("note", Text),
    Column("reviewer", String, nullable=False),
    Column("decided_at", String, nullable=False),
    Column("schema_version", Integer, nullable=False, default=SCHEMA_VERSION),
    Column("variant_id", String, nullable=True),   # the extraction variant that produced the finding decided on
    Column("surface", String, nullable=True),      # the UI arm the reviewer was in
)
t_records = Table(
    "records", metadata,
    Column("record_id", String, primary_key=True),
    Column("gap_id", String, ForeignKey("gaps.gap_id"), nullable=False, index=True),
    Column("decision_id", Integer, ForeignKey("decisions.id"), nullable=False),
    Column("created_at", String, nullable=False),
    Column("schema_version", Integer, nullable=False, default=SCHEMA_VERSION),
    Column("body", Json, nullable=False),
)
# Experimentation: variants are the configurations under comparison (extraction: model, effort, prompts,
# router; surface: a UI arm). Exposures record which arm a reviewer saw and when; decisions carry both.
t_variants = Table(
    "variants", metadata,
    Column("variant_id", String, primary_key=True),
    Column("kind", String, nullable=False),        # extraction | surface
    Column("label", String, nullable=False),
    Column("config", Json, nullable=False),
    Column("active", Integer, nullable=False, default=1),
    Column("created_at", String, nullable=False),
)
t_exposures = Table(
    "exposures", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reviewer", String, nullable=False, index=True),
    Column("surface", String, nullable=False, index=True),
    Column("page", String, nullable=False),        # queue | review | ...
    Column("gap_id", String, nullable=True),
    Column("override", Integer, nullable=False, default=0),  # 1 when the arm came from the override cookie
    Column("seen_at", String, nullable=False, index=True),
)
# Batch extraction: a job is one run of one variant over a set of gaps; items are idempotent per
# (gap, variant). Re-running a job continues from the items that aren't done.
t_jobs = Table(
    "jobs", metadata,
    Column("job_id", String, primary_key=True),
    Column("variant_id", String, nullable=False, index=True),
    Column("created_at", String, nullable=False),
    Column("params", Json, nullable=False),   # tranche, limit, router, workers, prices, ...
)
t_job_items = Table(
    "job_items", metadata,
    Column("gap_id", String, ForeignKey("gaps.gap_id"), primary_key=True),
    Column("variant_id", String, primary_key=True),
    Column("job_id", String, ForeignKey("jobs.job_id"), nullable=False, index=True),
    # pending | done | failed | skipped_by_router | qa_sample
    Column("status", String, nullable=False, index=True),
    Column("tranche_id", String, index=True),
    Column("measure", String),
    Column("model_served", String),
    Column("router_model", String),
    Column("router_decision", String),       # read | skip | None
    Column("qa_router_missed", Integer),     # 1 if a QA sample of a skipped gap found a numerator/exclusion
    Column("finding_type", String),
    Column("input_tokens", Integer, default=0),
    Column("output_tokens", Integer, default=0),
    Column("cache_read_tokens", Integer, default=0),
    Column("cache_write_tokens", Integer, default=0),
    Column("cost_usd", Float, default=0.0),
    Column("router_input_tokens", Integer, default=0),
    Column("router_output_tokens", Integer, default=0),
    Column("router_cache_read_tokens", Integer, default=0),
    Column("router_cost_usd", Float, default=0.0),
    Column("latency_ms", Integer),
    Column("error", Text),
    Column("updated_at", String, nullable=False),
)
# Store-level facts: which dataset is loaded, so switching ATTEST_DATASET re-imports.
t_meta = Table(
    "meta", metadata,
    Column("key", String, primary_key=True),
    Column("value", String),
)
# Daily sets (Phase 5): a dealt set is a fixed list of gaps for one reviewer. Progress is read from
# decisions, so a set never holds state that could disagree with the review record.
t_sets = Table(
    "sets", metadata,
    Column("set_id", String, primary_key=True),
    Column("reviewer", String, nullable=False, index=True),
    Column("tranche_id", String, nullable=True),
    Column("kind", String, nullable=False),        # daily | stretch
    Column("size", Integer, nullable=False),
    Column("dealt_on", String, nullable=False),     # YYYY-MM-DD (UTC)
    Column("dealt_at", String, nullable=False),
    Column("completed_at", String),
)
t_set_items = Table(
    "set_items", metadata,
    Column("set_id", String, ForeignKey("sets.set_id"), primary_key=True),
    Column("gap_id", String, ForeignKey("gaps.gap_id"), primary_key=True),
    Column("position", Integer, nullable=False),
)
# QA audits: a sample of decisions re-reviewed by a second person. The verdict is what the audited
# error rate and the confirmed catches are computed from.
t_qa_audits = Table(
    "qa_audits", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("decision_id", Integer, ForeignKey("decisions.id"), nullable=False, unique=True),
    Column("gap_id", String, ForeignKey("gaps.gap_id"), nullable=False, index=True),
    Column("sampled_at", String, nullable=False),
    Column("auditor", String),
    Column("verdict", String),                     # null (pending) | upheld | overturned
    Column("note", Text),
    Column("audited_at", String),
)
# A gap under review by one person. The lease expires on its own; a decision releases it.
t_assignments = Table(
    "assignments", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("gap_id", String, ForeignKey("gaps.gap_id"), nullable=False, index=True),
    Column("reviewer", String, nullable=False),
    Column("set_id", String),  # Phase 5: the dealt set this assignment belongs to
    Column("assigned_at", String, nullable=False),
    Column("lease_expires_at", String, nullable=False),
    Column("released_at", String),
)

_is_sqlite = DATABASE_URL.startswith("sqlite")
engine = create_engine(
    DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)
if _is_sqlite:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_fks(dbapi_conn, _record):  # SQLite ignores foreign keys unless told; behave like Postgres
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
_write_lock = threading.Lock()  # SQLite allows one writer; Postgres doesn't need this


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def schema_drift() -> dict:
    """{table: [missing columns]} for tables that exist but lack columns the code expects."""
    insp = inspect(engine)
    drift = {}
    for t in metadata.tables.values():
        if insp.has_table(t.name):
            have = {c["name"] for c in insp.get_columns(t.name)}
            missing = [c.name for c in t.columns if c.name not in have]
            if missing:
                drift[t.name] = missing
    return drift


def init() -> None:
    if _is_sqlite:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    drift = schema_drift()
    if drift:
        # Additive migration: missing columns are added in place (works on SQLite and Postgres for nullable
        # or defaulted columns), so a store keeps its data across a code update. Anything ALTER can't do
        # rebuilds a SQLite dev store and refuses to start on Postgres.
        failed = {}
        with engine.begin() as conn:
            for table_name, missing in drift.items():
                t = metadata.tables[table_name]
                for col_name in missing:
                    col = t.c[col_name]
                    ddl = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col.type.compile(engine.dialect)}"
                    if col.default is not None and getattr(col.default, "arg", None) is not None and not callable(col.default.arg):
                        ddl += f" DEFAULT {col.default.arg!r}"
                    try:
                        conn.execute(text(ddl))
                    except Exception as e:  # noqa: BLE001
                        failed.setdefault(table_name, []).append(f"{col_name}: {type(e).__name__}")
        if failed:
            if _is_sqlite:
                print(f"[attest.store] schema out of date and not migratable in place ({failed}); "
                      f"rebuilding the SQLite store at {DB_PATH}")
                metadata.drop_all(engine)
            else:
                raise RuntimeError(f"store schema out of date and not migratable in place: {failed}.")
        else:
            print(f"[attest.store] added columns in place: {drift}")
    metadata.create_all(engine)


def get_meta(key: str):
    r = _rows(select(t_meta.c.value).where(t_meta.c.key == key))
    return r[0]["value"] if r else None


def set_meta(key: str, value: str, conn=None) -> None:
    def go(c):
        c.execute(delete(t_meta).where(t_meta.c.key == key))
        c.execute(insert(t_meta), {"key": key, "value": value})
    if conn is not None:
        go(conn)
        return
    with tx() as c:
        go(c)


@contextmanager
def tx():
    """A write transaction."""
    if _is_sqlite:
        with _write_lock, engine.begin() as conn:
            yield conn
    else:
        with engine.begin() as conn:
            yield conn


def _rows(stmt) -> list:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt)]


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def dataset_loaded() -> bool:
    return bool(_rows(select(t_gaps.c.gap_id).limit(1)))


def replace_dataset(d: dict) -> None:
    """Load a whole dataset (keys: tranches, providers, members, identities, gaps, documents,
    gap_documents). Replaces what's there, including any state that referenced the old dataset."""
    with tx() as conn:
        clear_dataset(conn)
        for key in DATASET_TABLES:
            insert_rows(conn, key, d.get(key) or [])


DATASET_TABLES = {"tranches": t_tranches, "providers": t_providers, "members": t_members,
                  "identities": t_member_identity, "gaps": t_gaps, "documents": t_documents,
                  "gap_documents": t_gap_documents}


def clear_dataset(conn) -> None:
    """Remove the dataset AND the state that references it (findings, decisions, records, assignments).
    State rows are meaningless against a different population; the v1 demo and v2 never share a store."""
    tables = (t_qa_audits, t_set_items, t_sets, t_records, t_decisions, t_assignments, t_findings, t_job_items,
              t_jobs, t_exposures, t_gap_documents, t_documents, t_member_identity, t_gaps, t_members, t_providers, t_tranches)
    if _is_sqlite:
        for t in tables:
            conn.execute(delete(t))
    else:  # one statement, no per-row FK checks: a 100k population clears in well under a second
        conn.execute(text("TRUNCATE " + ", ".join(t.name for t in tables) + " RESTART IDENTITY"))


def insert_rows(conn, table_key: str, rows: list) -> None:
    if rows:
        conn.execute(insert(DATASET_TABLES[table_key]), rows)


def all_rows(table_name: str) -> list:
    return _rows(select(metadata.tables[table_name]))


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

def put_finding(gap_id: str, finding: dict, schema_version: int = SCHEMA_VERSION) -> None:
    call = finding.get("call") or {}
    row = {"gap_id": gap_id, "finding_type": finding["finding_type"], "confidence": finding.get("confidence"),
           "model": call.get("model_served"), "prompt_version": call.get("prompt_version"),
           "created_at": call.get("timestamp") or now(), "schema_version": schema_version, "body": finding}
    with tx() as conn:
        conn.execute(delete(t_findings).where(t_findings.c.gap_id == gap_id))
        conn.execute(insert(t_findings), row)


def findings() -> dict:
    return {r["gap_id"]: normalize_finding(r["body"]) for r in _rows(select(t_findings.c.gap_id, t_findings.c.body))}


def finding(gap_id: str):
    r = _rows(select(t_findings.c.body).where(t_findings.c.gap_id == gap_id))
    return normalize_finding(r[0]["body"]) if r else None


# ---------------------------------------------------------------------------
# Decisions and records
# ---------------------------------------------------------------------------

def add_decision(gap_id: str, action: str, reviewer: str, reason=None, note=None, conn=None,
                 variant_id=None, surface=None) -> int:
    row = {"gap_id": gap_id, "action": action, "reason": reason, "note": note, "reviewer": reviewer,
           "decided_at": now(), "schema_version": SCHEMA_VERSION, "variant_id": variant_id, "surface": surface}
    if conn is not None:
        return conn.execute(insert(t_decisions), row).inserted_primary_key[0]
    with tx() as c:
        return c.execute(insert(t_decisions), row).inserted_primary_key[0]


def latest_decisions() -> dict:
    out = {}
    for r in _rows(select(t_decisions).order_by(t_decisions.c.id)):
        out[r["gap_id"]] = r
    return out


def add_record(record: dict, decision_id: int, conn=None) -> None:
    row = {"record_id": record["record_id"], "gap_id": record["gap_id"], "decision_id": decision_id,
           "created_at": record["created_at"], "schema_version": SCHEMA_VERSION, "body": record}
    if conn is not None:
        conn.execute(insert(t_records), row)
        return
    with tx() as c:
        c.execute(insert(t_records), row)


def records(gap_id=None) -> list:
    stmt = select(t_records.c.body).order_by(t_records.c.created_at)
    if gap_id:
        stmt = stmt.where(t_records.c.gap_id == gap_id)
    return [r["body"] for r in _rows(stmt)]


def upsert_variant(variant_id: str, kind: str, label: str, config: dict) -> None:
    with tx() as conn:
        if not _rows(select(t_variants.c.variant_id).where(t_variants.c.variant_id == variant_id)):
            conn.execute(insert(t_variants), {"variant_id": variant_id, "kind": kind, "label": label, "config": config,
                                              "active": 1, "created_at": now()})


def variants(kind: str | None = None) -> list:
    stmt = select(t_variants).order_by(t_variants.c.created_at)
    if kind:
        stmt = stmt.where(t_variants.c.kind == kind)
    return _rows(stmt)


def add_exposure(reviewer: str, surface: str, page: str, gap_id=None, override=False) -> None:
    with tx() as conn:
        conn.execute(insert(t_exposures), {"reviewer": reviewer, "surface": surface, "page": page, "gap_id": gap_id,
                                           "override": int(override), "seen_at": now()})


def exposures() -> list:
    return _rows(select(t_exposures).order_by(t_exposures.c.id))


def decisions_all() -> list:
    return _rows(select(t_decisions).order_by(t_decisions.c.id))


def upsert_job(job_id: str, variant_id: str, params: dict) -> None:
    with tx() as conn:
        if not _rows(select(t_jobs.c.job_id).where(t_jobs.c.job_id == job_id)):
            conn.execute(insert(t_jobs), {"job_id": job_id, "variant_id": variant_id, "created_at": now(), "params": params})


def job_item(gap_id: str, variant_id: str):
    r = _rows(select(t_job_items).where(t_job_items.c.gap_id == gap_id, t_job_items.c.variant_id == variant_id))
    return r[0] if r else None


def put_job_item(row: dict) -> None:
    row = {**row, "updated_at": now()}
    with tx() as conn:
        conn.execute(delete(t_job_items).where(t_job_items.c.gap_id == row["gap_id"],
                                               t_job_items.c.variant_id == row["variant_id"]))
        conn.execute(insert(t_job_items), row)


def job_items(variant_id: str | None = None, job_id: str | None = None) -> list:
    stmt = select(t_job_items)
    if variant_id:
        stmt = stmt.where(t_job_items.c.variant_id == variant_id)
    if job_id:
        stmt = stmt.where(t_job_items.c.job_id == job_id)
    return _rows(stmt.order_by(t_job_items.c.updated_at))


def reset_reviews() -> None:
    """Clear decisions, records, and assignments; keep findings. Puts the demo in a known state."""
    with tx() as conn:
        conn.execute(delete(t_qa_audits))
        conn.execute(delete(t_set_items))
        conn.execute(delete(t_sets))
        conn.execute(delete(t_records))
        conn.execute(delete(t_decisions))
        conn.execute(delete(t_assignments))
        conn.execute(delete(t_exposures))


# ---------------------------------------------------------------------------
# Daily sets and QA audits
# ---------------------------------------------------------------------------

def add_set(set_id: str, reviewer: str, tranche_id, kind: str, gap_ids: list, dealt_on: str) -> None:
    with tx() as conn:
        conn.execute(insert(t_sets), {"set_id": set_id, "reviewer": reviewer, "tranche_id": tranche_id, "kind": kind,
                                      "size": len(gap_ids), "dealt_on": dealt_on, "dealt_at": now(), "completed_at": None})
        if gap_ids:
            conn.execute(insert(t_set_items), [{"set_id": set_id, "gap_id": g, "position": i} for i, g in enumerate(gap_ids)])


def sets(reviewer: str | None = None) -> list:
    stmt = select(t_sets).order_by(t_sets.c.dealt_at)
    if reviewer:
        stmt = stmt.where(t_sets.c.reviewer == reviewer)
    return _rows(stmt)


def set_items(set_id: str) -> list:
    return [r["gap_id"] for r in _rows(select(t_set_items.c.gap_id).where(t_set_items.c.set_id == set_id)
                                      .order_by(t_set_items.c.position))]


def set_items_all() -> dict:
    """{set_id: [gap_id, ...]} for every set."""
    out = {}
    for r in _rows(select(t_set_items).order_by(t_set_items.c.set_id, t_set_items.c.position)):
        out.setdefault(r["set_id"], []).append(r["gap_id"])
    return out


def complete_set(set_id: str) -> None:
    with tx() as conn:
        conn.execute(update(t_sets).where(t_sets.c.set_id == set_id, t_sets.c.completed_at.is_(None))
                     .values(completed_at=now()))


def add_qa_audit(decision_id: int, gap_id: str, conn=None) -> None:
    row = {"decision_id": decision_id, "gap_id": gap_id, "sampled_at": now(), "auditor": None, "verdict": None,
           "note": None, "audited_at": None}
    if conn is not None:
        conn.execute(insert(t_qa_audits), row)
        return
    with tx() as c:
        c.execute(insert(t_qa_audits), row)


def qa_audits(pending_only: bool = False) -> list:
    """Audits joined with the decision they sample (action, reviewer, surface)."""
    j = t_qa_audits.join(t_decisions, t_qa_audits.c.decision_id == t_decisions.c.id)
    stmt = select(t_qa_audits, t_decisions.c.action, t_decisions.c.reviewer, t_decisions.c.surface,
                  t_decisions.c.reason, t_decisions.c.decided_at).select_from(j).order_by(t_qa_audits.c.id)
    if pending_only:
        stmt = stmt.where(t_qa_audits.c.verdict.is_(None))
    return _rows(stmt)


def record_qa_verdict(audit_id: int, auditor: str, verdict: str, note=None) -> None:
    with tx() as conn:
        conn.execute(update(t_qa_audits).where(t_qa_audits.c.id == audit_id, t_qa_audits.c.verdict.is_(None))
                     .values(auditor=auditor, verdict=verdict, note=note, audited_at=now()))


# ---------------------------------------------------------------------------
# Assignments (leases)
# ---------------------------------------------------------------------------

def active_assignment(gap_id: str, at: str | None = None):
    at = at or now()
    r = _rows(select(t_assignments).where(t_assignments.c.gap_id == gap_id, t_assignments.c.released_at.is_(None),
                                        t_assignments.c.lease_expires_at > at).order_by(t_assignments.c.id.desc()).limit(1))
    return r[0] if r else None


def active_assignments(at: str | None = None) -> dict:
    at = at or now()
    out = {}
    for r in _rows(select(t_assignments).where(t_assignments.c.released_at.is_(None), t_assignments.c.lease_expires_at > at)
                   .order_by(t_assignments.c.id)):
        out[r["gap_id"]] = r
    return out


def add_assignment(gap_id: str, reviewer: str, lease_expires_at: str, set_id=None, conn=None) -> int:
    row = {"gap_id": gap_id, "reviewer": reviewer, "set_id": set_id, "assigned_at": now(),
           "lease_expires_at": lease_expires_at, "released_at": None}
    if conn is not None:
        return conn.execute(insert(t_assignments), row).inserted_primary_key[0]
    with tx() as c:
        return c.execute(insert(t_assignments), row).inserted_primary_key[0]


def release_assignments(gap_id: str, conn=None) -> None:
    stmt = update(t_assignments).where(t_assignments.c.gap_id == gap_id, t_assignments.c.released_at.is_(None)) \
        .values(released_at=now())
    if conn is not None:
        conn.execute(stmt)
        return
    with tx() as c:
        c.execute(stmt)
