"""Keyless evals, all computed by code.

- flags: labelled cases (built tenders + expected verdict) -> precision and recall per flag kind;
- normalize: spellings of regions and CPV words -> accuracy;
- reconcile: buyer and CPV aggregates from the analytics vs an independent recount over raw JSON.
"""
import json
import math
from importlib import resources
from statistics import fmean

from .analytics import flags as flags_mod
from .analytics import market
from .models import COMPETITIVE
from .normalize import UnknownInput, resolve_cpv, resolve_region
from .store import Store

REGION = "Львівська область"
PARTIES = {"A": "20000001", "B": "20000002", "C": "20000003", "Z": "20000099"}


def load_cases(name: str) -> list[dict]:
    text = resources.files("prozorro_mcp").joinpath("data", "evals", name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _party(letter: str) -> dict:
    return {"name": f"ТОВ {letter}", "identifier": {"scheme": "UA-EDR", "id": PARTIES[letter]}}


def build_tender(spec: dict, n: int) -> dict:
    days = spec.get("days", 10)
    t = {
        "id": f"eval{n:028d}", "tenderID": f"UA-EVAL-{n:06d}", "title": "eval",
        "status": spec.get("status", "complete"),
        "procurementMethodType": spec.get("method", "aboveThreshold"),
        "dateCreated": "2026-10-01T10:00:00+03:00", "dateModified": "2026-10-01T10:00:00+03:00",
        "procuringEntity": {"name": "Замовник", "identifier": {"scheme": "UA-EDR", "id": "10000001"},
                            "address": {"region": REGION}},
        "value": {"amount": 1000.0, "currency": "UAH"},
        "tenderPeriod": {"startDate": "2026-10-01T10:00:00+03:00",
                         "endDate": f"2026-10-{1 + int(days):02d}T{10 + int((days % 1) * 24):02d}:00:00+03:00"},
        "items": [{"description": "товар", "classification": {"id": "15612100-2"}, "quantity": 100,
                   "unit": {"code": spec.get("unit", "KGM")}}],
    }
    bids = spec.get("bids")
    winner = spec.get("winner", 0)
    if "unit_price" in spec:
        bids, winner = [["A", spec["unit_price"] * 100]], 0
        if spec.get("two_items"):
            t["items"].append({"description": "ще товар", "classification": {"id": "15612100-2"},
                               "quantity": 50, "unit": {"code": spec.get("unit", "KGM")}})
    if bids:
        t["bids"] = [{"id": f"b{k}", "status": "active", "tenderers": [_party(p)], "value": {"amount": a}}
                     for k, (p, a) in enumerate(bids)]
    if bids and winner is not None:
        p, a = bids[winner]
        t["awards"] = [{"id": "a0", "bid_id": f"b{winner}", "status": "active", "suppliers": [_party(p)],
                        "value": {"amount": a}}]
    elif not bids and spec.get("award") is not None:
        t["awards"] = [{"id": "a0", "status": "active", "suppliers": [_party(spec.get("supplier", "Z"))],
                        "value": {"amount": spec["award"]}}]
    return t


def _metrics(rows: list[tuple[bool, bool]]) -> dict:
    tp = sum(1 for e, p in rows if e and p)
    fp = sum(1 for e, p in rows if not e and p)
    fn = sum(1 for e, p in rows if e and not p)
    tn = sum(1 for e, p in rows if not e and not p)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else 1.0, "recall": tp / (tp + fn) if tp + fn else 1.0}


def run_flags() -> dict:
    cases = load_cases("flags.jsonl")
    per_kind: dict[str, list[tuple[bool, bool]]] = {}
    failures = []
    for case in cases:
        store = Store(":memory:")
        docs = [build_tender(spec, n) for n, spec in enumerate(case["tenders"])]
        for d in docs:
            store.upsert_tender(d)
        res = flags_mod.red_flags(store, region=REGION, kinds=[case["kind"]], limit=100)
        if "target" in case:
            predicted = any(f["tender_id"] == docs[case["target"]]["tenderID"] for f in res["flags"])
        else:
            predicted = res["counts"][case["kind"]] > 0
        store.close()
        per_kind.setdefault(case["kind"], []).append((case["expect"], predicted))
        if predicted != case["expect"]:
            failures.append(case["name"])
    metrics = {k: _metrics(v) for k, v in per_kind.items()}
    passed = all(m["precision"] == 1 and m["recall"] == 1 for m in metrics.values())
    return {"cases": len(cases), "per_kind": metrics, "failures": failures, "passed": passed}


def run_normalize() -> dict:
    cases = load_cases("normalize.jsonl")
    failures = []
    for c in cases:
        try:
            got = resolve_region(c["input"]) if "region" in c else resolve_cpv(c["input"])[0]
        except UnknownInput:
            got = None
        want = c.get("region") or c.get("cpv_first")
        if got != want:
            failures.append({"input": c["input"], "expected": want, "got": got})
    accuracy = 1 - len(failures) / len(cases)
    return {"cases": len(cases), "accuracy": accuracy, "failures": failures, "passed": accuracy >= 0.95}


def _recount(store: Store) -> list[dict]:
    """Raw JSON only: no SQL aggregates, no analytics code."""
    rows = []
    for (raw,) in store.conn.execute("SELECT raw FROM tenders WHERE missing = 0"):
        t = json.loads(raw)
        awards = [a for a in t.get("awards") or [] if a.get("status") == "active"]
        bidders = {((b.get("tenderers") or [{}])[0].get("identifier") or {}).get("id") for b in t.get("bids") or []}
        rows.append({
            "buyer": ((t.get("procuringEntity") or {}).get("identifier") or {}).get("id"),
            "cpvs": [((it.get("classification") or {}).get("id") or "") for it in t.get("items") or []],
            "expected": float((t.get("value") or {}).get("amount") or 0.0),
            "awarded": sum(float((a.get("value") or {}).get("amount") or 0.0) for a in awards),
            "competitive": t.get("procurementMethodType") in COMPETITIVE,
            "bidders": len(bidders - {None}),
        })
    return rows


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=0.01)


def run_reconcile(store: Store | None = None, top: int = 5) -> dict:
    if store is None:
        from .demo import demo_store
        store = demo_store()
    rows = _recount(store)
    mismatches, checked = [], 0
    buyers: dict[str, list[dict]] = {}
    for r in rows:
        buyers.setdefault(r["buyer"], []).append(r)
    for edrpou, ts in sorted(buyers.items(), key=lambda kv: -len(kv[1]))[:top]:
        with_bids = [t for t in ts if t["competitive"] and t["bidders"]]
        want = {"tenders": len(ts), "expected_sum": sum(t["expected"] for t in ts),
                "spend": sum(t["awarded"] for t in ts),
                "single_bidder_share": fmean(t["bidders"] == 1 for t in with_bids) if with_bids else None}
        got = market.buyer_profile(store, edrpou)
        for key, value in want.items():
            checked += 1
            if not _close(got[key], value):
                mismatches.append({"buyer": edrpou, "field": key, "analytics": got[key], "recount": value})
    prefixes: dict[str, int] = {}
    for r in rows:
        for p in {c[:2] for c in r["cpvs"] if c}:
            prefixes[p] = prefixes.get(p, 0) + 1
    for p, _ in sorted(prefixes.items(), key=lambda kv: -kv[1])[:top]:
        ts = [r for r in rows if any(c.startswith(p) for c in r["cpvs"])]
        want = {"count": len(ts), "expected_sum": sum(t["expected"] for t in ts),
                "awarded_sum": sum(t["awarded"] for t in ts)}
        got = market.market_overview(store, cpv=[p])
        for key, value in want.items():
            checked += 1
            if not _close(got[key], value):
                mismatches.append({"cpv": p, "field": key, "analytics": got[key], "recount": value})
    return {"checked": checked, "mismatches": mismatches, "passed": not mismatches and checked > 0}


SUITES = {"flags": run_flags, "normalize": run_normalize, "reconcile": run_reconcile}


def run_suite(name: str) -> dict:
    if name == "all":
        return {n: fn() for n, fn in SUITES.items()}
    if name not in SUITES:
        raise ValueError(f"Unknown suite '{name}'. Known: {', '.join(SUITES)}, all.")
    return {name: SUITES[name]()}
