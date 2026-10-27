"""Synthetic population v2: tranches, fragmented sources, scale.

Reuses the v1 scenario library (scripts/generate_data.py): every chart is still
built from a hand-written scenario with its expected finding, so labels stay
authored, not inferred. v2 adds the layer the demo lacked:

- A two-level tranche hierarchy: line of business (parent) and provider group
  (child). The provider group is the unit that owns closing the gap and the place
  evidence differs: an owned group has an EHR feed, an independent practice sends
  faxes. Region is a member attribute, not a tranche. A military line of business
  exists with members but no gaps: the gap engine excludes it (not rated under
  Stars or HEDIS), and the exclusion is visible.
- Fragmented sources: a member is known to a primary EHR source and sometimes an
  HIE or fax source under a different identifier. The identity table joins them.
  Some evidence lives only in a secondary source; some notes are duplicated across
  sources with header differences; some charts span providers.
- A fixed eval block: one held-out and two dev charts per (tranche, measure,
  category), generated first and identically regardless of population size, so
  the held-out set is the same in every bundle.
- Bulk gaps after the eval block, unlabeled, up to the requested size.

Run:
  python3 scripts/generate_data_v2.py                      # eval block only (committed)
  python3 scripts/generate_data_v2.py --gaps 100000        # eval block + bulk (gitignored bundle)

Writes data/v2/eval-block.jsonl.gz, data/v2/population-<N>.jsonl.gz (with --gaps),
data/labels/v2-dev.json, data/labels/v2-heldout.json, data/labels/v2-heldout_review.md.
Label files that differ from the generated ones are not overwritten without --force-labels.
Everything here is invented. No real member, provider, plan, or claims data.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate_data as gd  # noqa: E402  (the v1 scenario library)
import scenarios_v2  # noqa: E402  (TRC and KED)

from attest import registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "v2"
LABELS = ROOT / "data" / "labels"
MY = gd.MY
SEED = 20261011

# ---------------------------------------------------------------------------
# Tranches: line of business (parent) > provider group (child). All invented.
# ---------------------------------------------------------------------------

LOBS = {
    "LOB-IMA": {"name": "Individual MA", "excluded_reason": None},
    "LOB-GMA": {"name": "Group MA", "excluded_reason": None},
    "LOB-DSNP": {"name": "D-SNP", "excluded_reason": None},
    "LOB-MCD": {"name": "Medicaid", "excluded_reason": None},
    "LOB-MIL": {"name": "Military plan", "excluded_reason": "not rated under Stars or HEDIS; the gap engine skips it"},
}
REGIONS = ["Southeast", "Midwest", "West", "Northeast", "South"]

# Provider-group types decide the source profile. Source entries are kinds; "ehr" becomes a feed per
# tranche, "hie" a regional exchange per member region, "fax" one shared fax inbox. The first is primary.
GROUP_TYPES = {
    "OWN": {"label": "owned primary care", "sources": ["ehr"], "chart_rate": 0.92, "yield": 0.50,
            "frag_secondary": 0.05, "frag_duplicate": 0.10, "frag_cross_provider": 0.10},
    "IPA": {"label": "large IPA", "sources": ["ehr", "hie"], "chart_rate": 0.80, "yield": 0.42,
            "frag_secondary": 0.30, "frag_duplicate": 0.15, "frag_cross_provider": 0.25},
    "IND": {"label": "independent practices", "sources": ["fax", "hie"], "chart_rate": 0.60, "yield": 0.38,
            "frag_secondary": 0.35, "frag_duplicate": 0.25, "frag_cross_provider": 0.20},
    "OON": {"label": "out-of-network", "sources": ["hie", "fax"], "chart_rate": 0.45, "yield": 0.30,
            "frag_secondary": 0.45, "frag_duplicate": 0.10, "frag_cross_provider": 0.40},
}

# The eight provider-group tranches (LOB x group type) that carry gaps, with bulk weight and member ages.
TRANCHE_SPECS = [
    ("TR-IMA-OWN", "LOB-IMA", "OWN", 0.22, (65, 88)),
    ("TR-IMA-IPA", "LOB-IMA", "IPA", 0.18, (65, 88)),
    ("TR-IMA-IND", "LOB-IMA", "IND", 0.14, (65, 88)),
    ("TR-GMA-OWN", "LOB-GMA", "OWN", 0.10, (65, 85)),
    ("TR-GMA-OON", "LOB-GMA", "OON", 0.06, (65, 85)),
    ("TR-DSNP-IND", "LOB-DSNP", "IND", 0.10, (55, 88)),
    ("TR-MCD-IND", "LOB-MCD", "IND", 0.12, (40, 64)),
    ("TR-MCD-OON", "LOB-MCD", "OON", 0.08, (40, 64)),
]
BUSINESS_UNITS = []
for _tid, _lob, _gt, _w, _age in TRANCHE_SPECS:
    _g = GROUP_TYPES[_gt]
    BUSINESS_UNITS.append({"tranche_id": _tid, "parent_id": _lob, "group_type": _gt,
                           "name": f"{LOBS[_lob]['name']} · {_g['label']}", "weight": _w, "age": _age,
                           "chart_rate": _g["chart_rate"], "yield": _g["yield"],
                           "frag_secondary": _g["frag_secondary"], "frag_duplicate": _g["frag_duplicate"],
                           "frag_cross_provider": _g["frag_cross_provider"], "source_kinds": _g["sources"]})
BU_BY_ID = {b["tranche_id"]: b for b in BUSINESS_UNITS}
MILITARY_TRANCHE = {"tranche_id": "TR-MIL-ALL", "parent_id": "LOB-MIL", "name": "Military plan · all groups",
                    "age": (30, 70)}
MILITARY_MEMBER_SHARE = 0.02  # of bulk members: exist, carry no gaps

def _slug(x: str) -> str:
    return x.lower().replace(" ", "-")


SOURCES = {"src-fax": ("fax", "FAX")}
for _r in REGIONS:
    SOURCES[f"src-hie-{_slug(_r)}"] = ("hie", "HIE")
for _b in BUSINESS_UNITS:
    SOURCES[f"src-ehr-{_slug(_b['tranche_id'][3:])}"] = ("ehr", "MRN")


def sources_for(bu: dict, region: str) -> list:
    """Concrete source ids for a member of this tranche, primary first."""
    out = []
    for kind in bu["source_kinds"]:
        if kind == "ehr":
            out.append(f"src-ehr-{_slug(bu['tranche_id'][3:])}")
        elif kind == "hie":
            out.append(f"src-hie-{_slug(region)}")
        else:
            out.append("src-fax")
    return out


CLINIC_NAMES = ["Riverbend Family Medicine", "Northgate Primary Care", "Lakeshore Internal Medicine",
                "Pinecrest Medical Group", "Harbor View Clinic", "Cedar Ridge Health", "Summit Family Practice",
                "Oakfield Community Health", "Bayside Internal Medicine", "Meadowlark Primary Care",
                "Granite Hill Medical", "Willow Creek Family Health"]
PROVIDER_FIRST = ["Dana", "Marcus", "Priya", "Samuel", "Helena", "Theo", "Imani", "Lucas", "Noor", "Ravi", "Greta",
                  "Mateo", "Yuki", "Oren", "Lena", "Kofi", "Sofia", "Elias", "Mira", "Jonas"]
PROVIDER_LAST = ["Okonjo-Lind", "Tellefsen", "Vantongeren", "Achterberg", "Quarshie", "Brannigan", "Castellanos",
                 "Dvorak", "Esposito", "Farahani", "Gutierrez", "Halvorsen", "Ibarra", "Jorgensen", "Kaplan",
                 "Lindgren", "Moreau", "Nakagawa", "Oyelowo", "Petrov"]
CREDS = ["MD", "MD", "DO", "NP", "PA-C"]

# Measure keys: a measure code, or measure:indicator for measures with indicators (TRC).
MEASURES = ["CBP", "GSD", "COL-E"]                                   # v1 measures: every tranche in the eval block
NEW_MEASURES = ["TRC:NIA", "TRC:RDI", "TRC:PED", "TRC:MRP", "KED"]  # Phase 3: two tranches per stratum
ALL_MEASURES = MEASURES + NEW_MEASURES
CATEGORIES = ["positive", "negative", "trap", "exclusion"]
MEASURE_WEIGHTS = {"CBP": 0.30, "GSD": 0.25, "COL-E": 0.20, "TRC:NIA": 0.03, "TRC:RDI": 0.03, "TRC:PED": 0.04,
                   "TRC:MRP": 0.05, "KED": 0.10}
EVAL_TRANCHES_PER_NEW_STRATUM = 2


def age_range(key: str) -> tuple:
    measure, _, indicator = key.partition(":")
    return tuple(registry.spec(measure, indicator or None)["synthetic_age"])
CATEGORY_MIX = {"positive": 0.40, "negative": 0.25, "trap": 0.25, "exclusion": 0.10}  # v1 mix, scaled by BU yield

EVAL_HELDOUT_PER_STRATUM = 1
EVAL_DEV_PER_STRATUM = 2
S_ATTRS_DEFAULT = {"indicator": None}


def evidence_list(o) -> list:
    """Normalize a scenario's evidence to [(doc_ref, text)]. doc_ref is 'note' or an extra_docs index."""
    ev = o.evidence
    if not ev:
        return []
    if isinstance(ev, str):
        return [(o.doc, ev)]
    if isinstance(ev, tuple):
        return [ev]
    return list(ev)


# ---------------------------------------------------------------------------
# Scenario library: learn each scenario's category by a dry run
# ---------------------------------------------------------------------------

def _dummy_ctx():
    m = {"member_id": "SYN-0000000", "name": "Test Person", "sex": "F", "dob": "1960-01-01", "age_at_my_end": 66}
    p = {"provider_id": "PRV-00", "name": "Test Provider, MD", "clinic": "Test Clinic"}
    return {"m": m, "p": p, "d": date(MY, 6, 1), "He": "She", "his": "her"}


def scenario_table() -> dict:
    """{measure_key: {category: [(fn, monday, pairable, weight)]}} from the v1 plan plus the v2 additions.
    A TRC scenario is keyed by its indicator; the TRC hospice exclusion is listed under every indicator."""
    gd.rng.seed(0)
    table: dict = {m: {c: [] for c in CATEGORIES} for m in ALL_MEASURES}
    for measure, fn, n, monday, pairable in gd.PLAN + scenarios_v2.PLAN_V2:
        ctx = _dummy_ctx()
        out = fn(ctx)
        indicator = getattr(out, "indicator", None)
        keys = [f"{measure}:{indicator}"] if indicator else [measure]
        if measure == "TRC" and out.category == "exclusion":
            keys = [k for k in ALL_MEASURES if k.startswith("TRC:")]
        for k in keys:
            table[k][out.category].append((fn, monday, pairable, n))
    return table


SCENARIOS = scenario_table()


# ---------------------------------------------------------------------------
# Rosters and identities
# ---------------------------------------------------------------------------

def build_providers(rng: random.Random) -> dict:
    """Each business unit gets 2-3 clinics with 2-3 providers; yields vary by provider so the
    insight view has clusters. Returns {tranche_id: [provider]} and fills PROVIDERS list."""
    roster, providers, n = {}, [], 0
    clinics = list(CLINIC_NAMES)
    rng.shuffle(clinics)
    ci = 0
    for bu in BUSINESS_UNITS:
        roster[bu["tranche_id"]] = []
        for _ in range(rng.randint(2, 3)):
            clinic = clinics[ci % len(clinics)]
            ci += 1
            for _ in range(rng.randint(2, 3)):
                n += 1
                p = {"provider_id": f"PRV-{n:03d}",
                     "name": f"{rng.choice(PROVIDER_FIRST)} {rng.choice(PROVIDER_LAST)}, {rng.choice(CREDS)}",
                     "clinic": f"{clinic} ({GROUP_TYPES[bu['group_type']]['label']})",
                     "pos_weight": round(rng.choice([0.4, 0.8, 1.0, 1.6, 2.4, 3.0]), 1)}
                roster[bu["tranche_id"]].append(p)
                providers.append(p)
    return roster, providers


def make_member(rng: random.Random, idx: int, bu: dict, lo: int, hi: int) -> dict:
    sex = rng.choice(["F", "M"])
    first = rng.choice(gd.FIRST_F if sex == "F" else gd.FIRST_M)
    initial = rng.choice("ABCDEFGHJKLMNPRSTW")
    age = rng.randint(max(lo, bu["age"][0]), min(hi, bu["age"][1])) if max(lo, bu["age"][0]) <= min(hi, bu["age"][1]) \
        else rng.randint(lo, hi)
    dob = date(MY - age, rng.randint(1, 12), rng.randint(1, 28))
    return {"member_id": f"SYN-2{idx:06d}", "name": f"{first} {initial}. {rng.choice(gd.LAST)}", "sex": sex,
            "dob": dob.isoformat(), "age_at_my_end": MY - dob.year, "tranche_id": bu["tranche_id"],
            "region": rng.choice(REGIONS)}


def source_ref(rng: random.Random, source_id: str, member: dict) -> str:
    kind, prefix = SOURCES[source_id]
    if kind == "ehr":
        return member["member_id"]  # the primary EHR knows the plan id
    return f"{prefix}-{rng.randint(10_000_000, 99_999_999)}"


def retag_document(text: str, source_id: str, ref: str, duplicate: bool, rng: random.Random, doc_date: date) -> str:
    """Make a document look like it came from a secondary source: swap the member id line for the
    source's own identifier. Duplicates also get a routing header and whitespace drift."""
    kind, _ = SOURCES[source_id]
    out = text
    for marker in ("Member ID: ", "MEMBER ID: "):
        if marker in out:
            head, _, tail = out.partition(marker)
            rest = tail.split("\n", 1)
            label = "MRN" if kind == "hie" else "Patient ref"
            out = f"{head}{label}: {ref}" + ("\n" + rest[1] if len(rest) > 1 else "")
            break
    if kind == "hie":
        received = doc_date + timedelta(days=rng.randint(1, 30))  # the exchange delivers after the visit
        out = f"[HIE DOCUMENT EXCHANGE] received {gd.us(received)}\n" + out
    if duplicate:
        lines = out.split("\n")
        out = "\n".join(line.rstrip() + ("  " if rng.random() < 0.1 else "") for line in lines)
        out = f"*** RESENT COPY ***\n{out}"
    return out


# ---------------------------------------------------------------------------
# Chart assembly (v1 logic, plus sources and fragmentation)
# ---------------------------------------------------------------------------

class Builder:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.roster, self.providers = build_providers(self.rng)
        self.members: list = []
        self.identities: dict = {}
        self.gaps: list = []
        self.documents: list = []
        self.gap_documents: list = []
        self.labels: list = []
        self.member_n = 0
        self.gap_n = 0

    def pick_provider(self, bu: dict, category: str, rng: random.Random) -> dict:
        ps = self.roster[bu["tranche_id"]]
        weights = [p["pos_weight"] if category == "positive" else 1.0 for p in ps]
        return rng.choices(ps, weights=weights)[0]

    def chart(self, bu: dict, specs: list, split: str | None, force_frag: dict | None = None) -> list:
        """Build one chart with one gap per spec. Returns the gap ids. specs: [(measure, fn, monday)].
        split: 'heldout' | 'dev' | None (bulk, unlabeled)."""
        self.member_n += 1
        seed = f"{SEED}-{self.member_n}"
        rng = random.Random(seed)
        gd.rng.seed(seed)  # scenario functions draw from the v1 module rng
        lo = max(age_range(k)[0] for k, _, _ in specs)
        hi = min(age_range(k)[1] for k, _, _ in specs)
        if any(fn is gd.gsd_excl_frailty for _, fn, _ in specs):
            lo = max(lo, 68)
        member = make_member(rng, self.member_n, bu, lo, hi)
        self.members.append(member)
        sources = sources_for(bu, member["region"])
        primary = sources[0]
        refs: dict = {}

        def ref_for(src: str) -> str:
            if src not in refs:
                refs[src] = source_ref(rng, src, member)
                self.identities[(src, refs[src])] = member["member_id"]
            return refs[src]

        ref_for(primary)

        categories = [fn.__name__.split("_")[1] for _, fn, _ in specs]
        is_pos = any(c == "pos" for c in categories)
        pcp = self.pick_provider(bu, "positive" if is_pos else "other", rng)
        member["pcp_provider_id"] = pcp["provider_id"]
        frag = force_frag or {}
        cross = frag.get("cross_provider", rng.random() < bu["frag_cross_provider"])
        note_provider = pcp
        if cross:
            others = [p for p in self.roster[bu["tranche_id"]] if p is not pcp]
            note_provider = rng.choice(others) if others else pcp

        nd = gd.rand_date(date(MY, 3, 2), date(MY, 9, 25))
        if any(monday for _, _, monday in specs):
            nd = gd.to_monday(nd)
        he = "She" if member["sex"] == "F" else "He"
        outs = []
        for k, fn, _ in specs:
            measure, _, indicator = k.partition(":")
            ctx = {"m": member, "p": note_provider, "d": nd, "He": he, "his": "her" if he == "She" else "his",
                   "indicator": indicator or None}
            o = fn(ctx)
            o.indicator = indicator or None  # the stratum decides the indicator, including for the shared exclusion
            outs.append(o)

        hpi = gd.rng.sample(gd.HPI_FILLER, 2)
        for o in outs:
            hpi += o.hpi
        bp = next((o.vitals_bp for o in outs if o.vitals_bp), None)
        vitals = f"{bp}, {gd.base_vitals(member)}" if bp else gd.base_vitals(member)
        visit_type = next((o.visit_type for o in outs if o.visit_type), "Office visit (in person)")
        if visit_type.startswith(("Telehealth", "Telephone")):
            vitals = "not obtained (remote visit)"
        elif visit_type.startswith(("Chart review", "Portal")):
            vitals = "not obtained (no encounter)"
        results = [r for o in outs for r in o.results]
        ap = []
        for o in outs:
            ap += o.ap
        ap.append("Follow up in 3 months.")
        note_text = gd.progress_note(member, note_provider, nd, hpi, vitals, results, ap, visit_type)

        chart_docs = []  # (doc_type, date, text, source_id)
        extra_index = {}
        for oi, o in enumerate(outs):
            for ei, (dtype, dd, text) in enumerate(o.extra_docs):
                extra_index[(oi, ei)] = len(chart_docs)
                chart_docs.append([dtype, dd, text, primary])
        note_pos = len(chart_docs)
        chart_docs.append(["progress_note", nd, note_text, primary])

        # Fragmentation 1: the evidence-bearing document lives only in a secondary source.
        evidence_docs = set()
        for oi, o in enumerate(outs):
            for ref, _ in evidence_list(o):
                evidence_docs.add(note_pos if ref == "note" else extra_index[(oi, ref)])
        secondary = sources[1:]
        moved = set()
        if secondary and evidence_docs and frag.get("secondary", rng.random() < bu["frag_secondary"]):
            src = rng.choice(secondary)
            for i in evidence_docs:
                chart_docs[i][3] = src
                moved.add(i)
        # Fragmentation 2: a duplicate of the note arrives from another source.
        dup = None
        if secondary and frag.get("duplicate", rng.random() < bu["frag_duplicate"]):
            src = rng.choice(secondary)
            dup = ["progress_note", nd + timedelta(days=rng.randint(1, 20)), note_text, src]
            chart_docs.append(dup)
        # Every document from a non-EHR source shows that source's own identifier, never the plan id.
        # (For an independent practice the primary source is the fax inbox, so even primary docs are retagged.)
        for doc in chart_docs:
            if SOURCES[doc[3]][0] != "ehr":
                doc[2] = retag_document(doc[2], doc[3], ref_for(doc[3]), doc is dup, rng, doc[1])

        order = sorted(range(len(chart_docs)), key=lambda i: (chart_docs[i][1], i))
        id_of = {}
        for rank, i in enumerate(order):
            dtype, dd, text, src = chart_docs[i]
            doc_id = f"DOC-{member['member_id'][4:]}-{rank + 1}"
            id_of[i] = doc_id
            ref = ref_for(src)
            self.documents.append({"doc_id": doc_id, "member_id": member["member_id"], "doc_type": dtype,
                                   "doc_date": dd.isoformat(), "source_id": src, "source_member_ref": ref, "text": text})

        frag_tags = []
        if moved:
            frag_tags.append("evidence_in_secondary_source")
        if dup is not None:
            frag_tags.append("duplicate_document")
        if cross:
            frag_tags.append("cross_provider")

        gap_ids = []
        for (k, fn, _), o in zip(specs, outs):
            measure, _, indicator = k.partition(":")
            indicator = indicator or None
            self.gap_n += 1
            gap_id = f"G2-{self.gap_n:06d}"
            gap_ids.append(gap_id)
            self.gaps.append({"gap_id": gap_id, "member_id": member["member_id"], "measure": measure,
                              "indicator": indicator,
                              "provider_id": pcp["provider_id"], "tranche_id": bu["tranche_id"],
                              "due_date": gd.rand_date(date(MY, 11, 1), date(MY, 12, 31)).isoformat()})
            for pos, i in enumerate(order):
                self.gap_documents.append({"gap_id": gap_id, "doc_id": id_of[i], "position": pos})
            if split:
                label = {"gap_id": gap_id, "member_id": member["member_id"], "measure": measure,
                         "indicator": indicator, "tranche_id": bu["tranche_id"], "split": split,
                         "scenario": fn.__name__, "category": o.category,
                         "expected_finding": o.finding, "expected_fields": o.fields,
                         "abstain_reason": o.reason, "evidence": None, "evidence_source": "none",
                         "fragmentation": frag_tags, "note": o.note}
                evs = []
                for ref, quote in evidence_list(o):
                    src_i = note_pos if ref == "note" else extra_index[(outs.index(o), ref)]
                    text = chart_docs[src_i][2]
                    start = text.find(quote)
                    assert start >= 0, f"evidence not found for {gap_id} {fn.__name__}: {quote!r}"
                    evs.append({"doc_id": id_of[src_i], "start": start, "end": start + len(quote), "text": quote,
                                "_moved": src_i in moved})
                if evs:
                    label["evidence_source"] = "secondary" if any(e["_moved"] for e in evs) else "primary"
                    for e in evs:
                        del e["_moved"]
                    label["evidence"] = evs[0] if len(evs) == 1 else evs
                self.labels.append(label)
        return gap_ids

    def chase_only(self, bu: dict, measure: str) -> str:
        self.member_n += 1
        rng = random.Random(f"{SEED}-{self.member_n}")
        measure_code, _, indicator = measure.partition(":")
        member = make_member(rng, self.member_n, bu, *age_range(measure))
        pcp = self.pick_provider(bu, "other", rng)
        member["pcp_provider_id"] = pcp["provider_id"]
        self.members.append(member)
        primary = sources_for(bu, member["region"])[0]
        self.identities[(primary, source_ref(rng, primary, member))] = member["member_id"]
        self.gap_n += 1
        gap_id = f"G2-{self.gap_n:06d}"
        self.gaps.append({"gap_id": gap_id, "member_id": member["member_id"], "measure": measure_code,
                          "indicator": indicator or None,
                          "provider_id": pcp["provider_id"], "tranche_id": bu["tranche_id"],
                          "due_date": gd.rand_date(date(MY, 11, 1), date(MY, 12, 31)).isoformat()})
        return gap_id


def pick_scenario(rng: random.Random, measure: str, category: str):
    options = SCENARIOS[measure][category]
    fn, monday, pairable, _ = rng.choices(options, weights=[w for *_, w in options])[0]
    return fn, monday, pairable


def eval_block(b: Builder) -> None:
    """Held-out and dev charts for every (tranche, measure, category) stratum; fragmentation forced in
    a fixed rotation so each kind has an n. Single-gap charts, for clean strata."""
    rng = random.Random(f"{SEED}-eval")
    rotation = [{"secondary": True}, {"duplicate": True}, {"cross_provider": True}, {}]
    k = 0
    for split, per in (("heldout", EVAL_HELDOUT_PER_STRATUM), ("dev", EVAL_DEV_PER_STRATUM)):
        for bu in BUSINESS_UNITS:
            for measure in MEASURES:
                for category in CATEGORIES:
                    for _ in range(per):
                        fn, monday, _ = pick_scenario(rng, measure, category)
                        force = dict(rotation[k % len(rotation)])
                        k += 1
                        b.chart(bu, [(measure, fn, monday)], split, force_frag=force)
        # New measures: each (measure, category) stratum lands in a rotating pair of tranches, so the review
        # stays reviewable and the per-tranche claims for these measures are explicitly small-n.
        t = 0
        for measure in NEW_MEASURES:
            for category in CATEGORIES:
                for _ in range(per * EVAL_TRANCHES_PER_NEW_STRATUM):
                    bu = BUSINESS_UNITS[t % len(BUSINESS_UNITS)]
                    t += 1
                    fn, monday, _ = pick_scenario(rng, measure, category)
                    force = dict(rotation[k % len(rotation)])
                    k += 1
                    b.chart(bu, [(measure, fn, monday)], split, force_frag=force)


def military_member(b: Builder, rng: random.Random) -> None:
    """A member of the excluded line of business: exists, has a region and a PCP, carries no gaps."""
    b.member_n += 1
    member = make_member(rng, b.member_n, MILITARY_TRANCHE, *MILITARY_TRANCHE["age"])
    member["pcp_provider_id"] = None
    b.members.append(member)


def bulk(b: Builder, n_gaps: int) -> None:
    rng = random.Random(f"{SEED}-bulk")
    bus = BUSINESS_UNITS
    bu_w = [x["weight"] for x in bus]
    while b.gap_n < n_gaps:
        if rng.random() < MILITARY_MEMBER_SHARE:
            military_member(b, rng)
        bu = rng.choices(bus, weights=bu_w)[0]
        measure = rng.choices(ALL_MEASURES, weights=[MEASURE_WEIGHTS[m] for m in ALL_MEASURES])[0]
        if rng.random() > bu["chart_rate"]:
            b.chase_only(bu, measure)
            continue
        # Category mix scaled by the unit's yield: positives+exclusions = yield, the rest split v1-style.
        y = bu["yield"]
        mix = {"positive": y * 0.8, "exclusion": y * 0.2, "negative": (1 - y) * 0.5, "trap": (1 - y) * 0.5}
        category = rng.choices(CATEGORIES, weights=[mix[c] for c in CATEGORIES])[0]
        fn, monday, pairable = pick_scenario(rng, measure, category)
        specs = [(measure, fn, monday)]
        if pairable and measure in ("CBP", "GSD", "KED") and rng.random() < 0.25:
            other = {"CBP": "GSD", "GSD": "CBP", "KED": "GSD"}[measure]
            oc = rng.choices(CATEGORIES, weights=[mix[c] for c in CATEGORIES])[0]
            ofn, omonday, opair = pick_scenario(rng, other, oc)
            if opair:
                specs.append((other, ofn, omonday))
        b.chart(bu, specs, None)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def tranche_rows() -> list:
    rows = [{"tranche_id": tid, "name": v["name"], "kind": "line_of_business", "parent_id": None,
             "excluded_reason": v["excluded_reason"]} for tid, v in LOBS.items()]
    rows += [{"tranche_id": bu["tranche_id"], "name": bu["name"], "kind": "provider_group",
              "parent_id": bu["parent_id"], "excluded_reason": None} for bu in BUSINESS_UNITS]
    rows.append({"tranche_id": MILITARY_TRANCHE["tranche_id"], "name": MILITARY_TRANCHE["name"], "kind": "provider_group",
                 "parent_id": MILITARY_TRANCHE["parent_id"], "excluded_reason": LOBS["LOB-MIL"]["excluded_reason"]})
    return rows


def write_bundle(path: Path, b: Builder, member_ids: set | None = None) -> dict:
    """Stream a bundle: one JSON object per line, {"t": table, "r": row}, table-major."""
    keep = (lambda m: True) if member_ids is None else (lambda m: m in member_ids)
    counts = Counter()
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        def emit(table, rows):
            for r in rows:
                fh.write(json.dumps({"t": table, "r": r}) + "\n")
                counts[table] += 1
        emit("tranches", tranche_rows())
        emit("providers", [{k: v for k, v in p.items() if k != "pos_weight"} for p in b.providers])
        emit("members", [m for m in b.members if keep(m["member_id"])])
        emit("identities", [{"source_id": s, "source_member_ref": r, "member_id": m}
                            for (s, r), m in b.identities.items() if keep(m)])
        gap_ids = {g["gap_id"] for g in b.gaps if keep(g["member_id"])}
        emit("gaps", [g for g in b.gaps if g["gap_id"] in gap_ids])
        emit("documents", [d for d in b.documents if keep(d["member_id"])])
        emit("gap_documents", [gd_ for gd_ in b.gap_documents if gd_["gap_id"] in gap_ids])
    return dict(counts)


def write_labels(b: Builder, force: bool) -> None:
    LABELS.mkdir(parents=True, exist_ok=True)
    for split in ("dev", "heldout"):
        rows = [lb for lb in b.labels if lb["split"] == split]
        path = LABELS / f"v2-{split}.json"
        if path.exists() and json.loads(path.read_text()) != rows and not force:
            sys.exit(f"{path} differs from the generated labels (hand edits?). Re-run with --force-labels.")
        path.write_text(json.dumps(rows, indent=2) + "\n")
    ho = [lb for lb in b.labels if lb["split"] == "heldout"]
    docs_of = {}
    for gd_ in b.gap_documents:
        docs_of.setdefault(gd_["gap_id"], []).append(gd_["doc_id"])
    src_of = {d["doc_id"]: d["source_id"] for d in b.documents}
    date_of = {d["doc_id"]: (d["doc_type"].replace("_", " "), d["doc_date"]) for d in b.documents}
    lines = ["# v2 held-out labels: review sheet", "",
             "Generated from the scenario libraries by `scripts/generate_data_v2.py`. One chart per (provider-group tranche, "
             "measure, category) for CBP, GSD, COL-E; for TRC (per indicator) and KED, one chart per (measure, category) in "
             "each of two rotating tranches. Review each row against its documents before the first v2 eval run; edit "
             "`v2-heldout.json` directly if a label is wrong. Documents are in the store after "
             "`python -m attest.seed import-data --bundle data/v2/eval-block.jsonl.gz`; the app shows them at /gap/<id>.", "",
             "For `none` rows there is no evidence passage: check that the documents contain nothing that closes the gap. "
             "`secondary` means the evidence document came from a source other than the tranche's primary one, under that "
             "source's own identifier. For independent practices the primary source is the fax inbox, so most documents "
             "carry a FAX reference even when primary.", ""]
    for bu in BUSINESS_UNITS:
        lines += [f"## {bu['name']} (`{bu['tranche_id']}`, sources: {', '.join(bu['source_kinds'])})", "",
                  "| Gap | Measure | Documents (type, date, source) | Expected | Fields / reason | Evidence | Fragmentation | Note |",
                  "|---|---|---|---|---|---|---|---|"]
        for lb in ho:
            if lb["tranche_id"] != bu["tranche_id"]:
                continue
            detail = lb["abstain_reason"] or ", ".join(f"{k}={v}" for k, v in lb["expected_fields"].items()) or "-"
            evs = lb["evidence"] if isinstance(lb["evidence"], list) else ([lb["evidence"]] if lb["evidence"] else [])
            ev = "<br>".join(f"`{e['doc_id']}`: \"{e['text']}\"" for e in evs) + (f" ({lb['evidence_source']})" if evs else "") if evs else "-"
            docs = "<br>".join(f"`{d}` {date_of[d][0]} {date_of[d][1]} ({src_of[d].replace('src-', '')})"
                               for d in docs_of[lb["gap_id"]])
            frag = ", ".join(lb["fragmentation"]) or "-"
            mkey = f"{lb['measure']}:{lb['indicator']}" if lb.get("indicator") else lb["measure"]
            lines.append(f"| {lb['gap_id']} | {mkey} | {docs} | {lb['expected_finding']} | {detail} | {ev} | "
                         f"{frag} | {lb['note'] or ''} |")
        lines.append("")
    (LABELS / "v2-heldout_review.md").write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gaps", type=int, default=0, help="total gaps including the eval block (0 = eval block only)")
    ap.add_argument("--force-labels", action="store_true")
    args = ap.parse_args(argv)

    b = Builder(SEED)
    eval_block(b)
    eval_members = {m["member_id"] for m in b.members}
    n_eval = b.gap_n
    counts = write_bundle(OUT / "eval-block.jsonl.gz", b, eval_members)
    write_labels(b, args.force_labels)
    print(f"eval block: {n_eval} gaps ({len([x for x in b.labels if x['split']=='heldout'])} held-out, "
          f"{len([x for x in b.labels if x['split']=='dev'])} dev) -> {OUT / 'eval-block.jsonl.gz'} {counts}")
    print("held-out fragmentation:", dict(Counter(t for lb in b.labels if lb["split"] == "heldout" for t in lb["fragmentation"])),
          "| evidence source:", dict(Counter(lb["evidence_source"] for lb in b.labels if lb["split"] == "heldout")))

    if args.gaps:
        if args.gaps <= n_eval:
            sys.exit(f"--gaps must exceed the eval block ({n_eval})")
        bulk(b, args.gaps)
        path = OUT / f"population-{args.gaps}.jsonl.gz"
        counts = write_bundle(path, b)
        by_bu = Counter(g["tranche_id"] for g in b.gaps)
        linked = {gd_["gap_id"] for gd_ in b.gap_documents}
        print(f"population: {b.gap_n} gaps, {len(b.members)} members, {len(b.documents)} documents, "
              f"{len(b.identities)} identities -> {path} ({path.stat().st_size / 1e6:.1f} MB) {counts}")
        print("gaps by tranche:", dict(by_bu), "| without a chart:", b.gap_n - len(linked),
              "| non-EHR docs:", sum(1 for d in b.documents if SOURCES[d['source_id']][0] != 'ehr'),
              "| military members (no gaps):", sum(1 for m in b.members if m['tranche_id'] == MILITARY_TRANCHE['tranche_id']))
    return 0


if __name__ == "__main__":
    sys.exit(main())
