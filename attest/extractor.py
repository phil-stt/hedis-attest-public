"""Evidence extraction: one Claude call per gap, then the deterministic rule checks.

Every call is logged (model requested and served, prompt version, input
document ids, output, latency, token counts) to logs/calls.jsonl.
"""

import json
import os
import threading
import time
from datetime import UTC, datetime
from functools import cache, lru_cache

import anthropic

from . import data, registry, rules
from .config import EFFORT, LOG_DIR, MAX_TOKENS, MEASUREMENT_YEAR, MODEL, PROMPT_DIR
from .schema import finding_schema

_log_lock = threading.Lock()
REFUSAL_RETRIES = 1
# Server-side fallback for safeguard refusals. The "default" routing never engaged for the
# reasoning_extraction category this task occasionally trips, so the fallback model is named.
# A finding produced by the fallback carries that model in model_served; the UI shows it.
FALLBACK_MODELS = [{"model": os.environ.get("ATTEST_FALLBACK_MODEL", "claude-opus-4-8")}]


@cache
def prompt(version: str) -> str:
    return (PROMPT_DIR / f"{version}.md").read_text()


@lru_cache(maxsize=1)
def client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=4)


def build_user_message(gap: dict) -> str:
    member = data.members()[gap["member_id"]]
    spec = registry.spec_for_gap(gap)
    parts = [
        f"Measurement year: {MEASUREMENT_YEAR}",
        f"Open gap: {gap['measure']}" + (f", indicator {spec['indicator']} ({spec['name']})" if spec["indicator"] else ""),
        f"Member age at end of measurement year: {member['age_at_my_end']}",
        f"Documents in chart: {len(gap['document_ids'])}",
        "",
    ]
    for doc_id in gap["document_ids"]:
        meta = data.documents()[doc_id]
        parts.append(f'<document id="{doc_id}" type="{meta["doc_type"]}" date="{meta["doc_date"]}" '
                     f'source="{meta.get("source_id", "")}">')
        parts.append(data.document_text(doc_id).rstrip("\n"))
        parts.append("</document>")
        parts.append("")
    return "\n".join(parts)


def _log(record: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _log_lock, open(LOG_DIR / "calls.jsonl", "a") as fh:
        fh.write(json.dumps(record) + "\n")


def _failed(gap: dict, reason: str, rationale: str) -> dict:
    return {"candidates_considered": [], "finding_type": "abstain", "abstain_reason": reason,
            "fields": {}, "evidence": [], "confidence": 0.0, "rationale": rationale}


ROUTER_PROMPT = "router.v3"
ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "content_kind": {"type": "string", "enum": ["result_or_event", "exclusion", "nothing"]},
        "worth_reading": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["content_kind", "worth_reading", "reason"],
    "additionalProperties": False,
}


def route(gap_id: str, model: str) -> dict:
    """Cheap first pass: does this chart contain anything that could close or exclude the gap?
    Returns {"worth_reading", "reason", "call"}. Never produces a finding; a skip is a routing decision
    the QA sample checks."""
    gap = data.gaps()[gap_id]
    started = time.monotonic()
    meta = {"gap_id": gap_id, "measure": gap["measure"], "indicator": gap.get("indicator"), "role": "router",
            "model_requested": model, "prompt_version": ROUTER_PROMPT, "document_ids": gap["document_ids"],
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds")}
    try:
        # No effort/thinking parameters: the router runs on Haiku 4.5, which takes neither.
        resp = client().beta.messages.create(
            model=model, max_tokens=512,
            system=[{"type": "text", "text": prompt(ROUTER_PROMPT), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": build_user_message(gap)}],
            output_config={"format": {"type": "json_schema", "schema": ROUTER_SCHEMA}},
        )
    except anthropic.APIError as e:
        meta.update(latency_ms=round((time.monotonic() - started) * 1000), error=f"{type(e).__name__}: {e}")
        _log(meta)
        # Fail open: the full extractor reads the chart. The error is reported, never hidden.
        return {"worth_reading": True, "reason": f"router error: {type(e).__name__}", "error": str(e)[:300], "call": meta}
    u = resp.usage
    meta.update(model_served=resp.model, request_id=resp._request_id, stop_reason=resp.stop_reason,
                latency_ms=round((time.monotonic() - started) * 1000),
                usage={"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                       "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                       "cache_creation_input_tokens": u.cache_creation_input_tokens or 0})
    if resp.stop_reason != "end_turn":
        out = {"worth_reading": True, "reason": f"router {resp.stop_reason}; read it", "error": resp.stop_reason}
    else:
        out = json.loads([b.text for b in resp.content if b.type == "text"][-1])
        out["worth_reading"] = out.get("content_kind") != "nothing"  # the kind decides; the boolean just restates it
    meta["output"] = out
    _log(meta)
    return {**out, "call": {k: v for k, v in meta.items() if k != "output"}}


def extract(gap_id: str, model: str | None = None) -> dict:
    """Run extraction for one gap. Returns the checked finding plus call metadata, tagged with the
    extraction variant (the pipeline overrides the tag for router runs)."""
    out = _extract(gap_id, model)
    out.setdefault("variant_id", registry.variant_id(None, model=model))
    return out


def _extract(gap_id: str, model: str | None = None) -> dict:
    gap = data.gaps()[gap_id]
    measure = gap["measure"]
    spec = registry.spec_for_gap(gap)
    version = spec["prompt"]
    model = model or MODEL
    if not gap["document_ids"]:
        raise ValueError(f"{gap_id} has no documents to read")

    started = time.monotonic()
    meta = {"gap_id": gap_id, "measure": measure, "indicator": spec["indicator"], "model_requested": model, "effort": EFFORT,
            "prompt_version": version, "document_ids": gap["document_ids"],
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"), "attempts": []}
    usage = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens",
                           "cache_creation_input_tokens"), 0)
    # A refusal on a clinical chart is a classifier false positive more often than not; retry once,
    # then hand the gap to the nurse as an abstain. Both attempts are logged.
    for _ in range(1 + REFUSAL_RETRIES):
        try:
            resp = client().beta.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                betas=["server-side-fallback-2026-06-01"],
                fallbacks=FALLBACK_MODELS,
                # Explicit cache breakpoint: the per-measure system prompt is identical across calls, so at
                # batch concurrency it should be served from cache (cache_read_input_tokens > 0).
                system=[{"type": "text", "text": prompt(version), "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": build_user_message(gap)}],
                output_config={"effort": EFFORT,
                               "format": {"type": "json_schema", "schema": finding_schema(spec)}},
            )
        except anthropic.APIError as e:
            meta.update(latency_ms=round((time.monotonic() - started) * 1000), error=f"{type(e).__name__}: {e}")
            _log(meta)
            raw = _failed(gap, "model_error", "The model call failed; route to manual review.")
            return {**rules.apply(spec, raw, data.members()[gap["member_id"]], {}, {}), "call": meta}
        for k in usage:
            usage[k] += getattr(resp.usage, k) or 0
        sd = resp.stop_details
        fallback_ran = any(getattr(it, "type", None) == "fallback_message" for it in (resp.usage.iterations or []))
        meta["attempts"].append({"request_id": resp._request_id, "model_served": resp.model,
                                 "stop_reason": resp.stop_reason, "fallback": fallback_ran,
                                 "refusal_category": getattr(sd, "category", None) if sd else None})
        if resp.stop_reason != "refusal":
            break

    meta.update(
        model_served=resp.model,
        request_id=resp._request_id,
        stop_reason=resp.stop_reason,
        latency_ms=round((time.monotonic() - started) * 1000),
        usage=usage,
    )

    if resp.stop_reason == "refusal":
        raw = _failed(gap, "model_refused", "The model declined this request; route to manual review.")
    elif resp.stop_reason == "max_tokens":
        raw = _failed(gap, "model_error", "The model ran out of output budget; route to manual review.")
    else:
        text = [b.text for b in resp.content if b.type == "text"][-1]
        raw = json.loads(text)
    meta["output"] = raw
    _log(meta)

    docs = {d: data.documents()[d] for d in gap["document_ids"]}
    texts = {d: data.document_text(d) for d in gap["document_ids"]}
    checked = rules.apply(spec, raw, data.members()[gap["member_id"]], docs, texts)
    return {**checked, "call": {k: v for k, v in meta.items() if k != "output"}}
