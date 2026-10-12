"""Red flags: a fixed set of documented rules, each returned with its evidence.

A flag is a reason to look closer at a tender, not proof of wrongdoing.
"""
from datetime import datetime

from ..models import COMPETITIVE, tender_url
from ..store import Store
from .common import clamp_limit, money_text
from .market import _COMP, _DECIDED, _scope, percentile

KINDS = ("single_bidder", "supplier_concentration", "price_above_median", "short_tender_period")

# Heuristic minimum tendering periods in days; not a legal check (martial-law rules change often).
MIN_DAYS = {"aboveThreshold": 7, "aboveThresholdUA": 7, "aboveThresholdEU": 15,
            "belowThreshold": 3, "priceQuotation": 3}

NOTE = "A flag is a reason to look closer, not proof of wrongdoing. Check the tender on prozorro.gov.ua."


def _row(t, kind: str, evidence: dict) -> dict:
    return {"kind": kind, "tender_id": t["tender_id"], "url": tender_url(t["tender_id"]), "title": t["title"],
            "buyer": t["buyer_name"], "buyer_edrpou": t["buyer_edrpou"], "amount": t["amount"],
            "amount_text": money_text(t["amount"]), "evidence": evidence}


def _single_bidder(store, where, params) -> list[dict]:
    rows = store.conn.execute(f"""
        SELECT t.*, (SELECT count(DISTINCT b.edrpou) FROM bids b WHERE b.tender_id = t.id) AS bidders
        FROM tenders t
        WHERE {where} AND t.method_type IN ({_COMP}) AND ({_DECIDED} OR t.status = 'complete')
        ORDER BY t.amount DESC""", params).fetchall()
    return [_row(t, "single_bidder", {"bidders": 1, "method": t["method_type"]})
            for t in rows if t["bidders"] == 1]


def _concentration(store, where, params, share, min_awards) -> list[dict]:
    rows = store.conn.execute(f"""
        SELECT t.buyer_edrpou, max(t.buyer_name) AS buyer_name, a.edrpou, max(a.name) AS name,
               count(*) AS n, sum(a.amount) AS total
        FROM awards a JOIN tenders t ON t.id = a.tender_id
        WHERE {where} AND a.status = 'active' GROUP BY t.buyer_edrpou, a.edrpou""", params).fetchall()
    per_buyer: dict[str, list] = {}
    for r in rows:
        per_buyer.setdefault(r["buyer_edrpou"], []).append(r)
    out = []
    for buyer, sups in per_buyer.items():
        awards = sum(s["n"] for s in sups)
        spend = sum(s["total"] or 0.0 for s in sups)
        if awards < min_awards or spend <= 0:
            continue
        for s in sups:
            sh = (s["total"] or 0.0) / spend
            if sh > share:
                out.append({"kind": "supplier_concentration", "tender_id": None, "url": None, "title": None,
                            "buyer": s["buyer_name"], "buyer_edrpou": buyer, "amount": s["total"],
                            "amount_text": money_text(s["total"]),
                            "evidence": {"supplier": s["name"], "supplier_edrpou": s["edrpou"],
                                         "share": sh, "supplier_sum": s["total"], "buyer_sum": spend,
                                         "awards": awards, "supplier_awards": s["n"]}})
    return sorted(out, key=lambda f: -f["evidence"]["share"])


def _unit_price_rows(store, where, params):
    return store.conn.execute(f"""
        SELECT t.*, i.cpv, i.unit_code, i.quantity,
               (SELECT sum(a.amount) FROM awards a WHERE a.tender_id = t.id AND a.status = 'active') AS award
        FROM tenders t JOIN items i ON i.tender_id = t.id
        WHERE {where} AND {_DECIDED}
          AND (SELECT count(*) FROM items i2 WHERE i2.tender_id = t.id) = 1
          AND (SELECT count(*) FROM awards a3 WHERE a3.tender_id = t.id AND a3.status = 'active') = 1
          AND i.quantity > 0""", params).fetchall()


def _price_above_median(store, where, params, since, pct, min_peers) -> list[dict]:
    # peers: every comparable tender in the local data (same CPV and unit), not only the scope
    peer_where, peer_params = _scope(None, None, since)
    groups: dict[tuple, list[float]] = {}
    for r in _unit_price_rows(store, peer_where, peer_params):
        groups.setdefault((r["cpv"], r["unit_code"]), []).append(r["award"] / r["quantity"])
    out = []
    for t in _unit_price_rows(store, where, params):
        peers = groups.get((t["cpv"], t["unit_code"]), [])
        if len(peers) < min_peers:
            continue
        med = percentile(peers, 0.5)
        price = t["award"] / t["quantity"]
        if med > 0 and round(price / med - 1, 6) >= pct:
            out.append(_row(t, "price_above_median", {
                "unit_price": round(price, 2), "median": round(med, 2), "peers": len(peers),
                "pct_above": round(price / med - 1, 4), "cpv": t["cpv"], "unit": t["unit_code"]}))
    return sorted(out, key=lambda f: -f["evidence"]["pct_above"])


def _short_period(store, where, params) -> list[dict]:
    rows = store.conn.execute(f"""
        SELECT * FROM tenders t WHERE {where} AND t.tender_start IS NOT NULL AND t.tender_end IS NOT NULL
        AND t.method_type IN ({",".join("?" * len(MIN_DAYS))})""", [*params, *MIN_DAYS]).fetchall()
    out = []
    for t in rows:
        days = (datetime.fromisoformat(t["tender_end"]) - datetime.fromisoformat(t["tender_start"])
                ).total_seconds() / 86400
        min_days = MIN_DAYS[t["method_type"]]
        if days < min_days:
            out.append(_row(t, "short_tender_period", {"days": round(days, 1), "min_days": min_days,
                                                       "method": t["method_type"]}))
    return sorted(out, key=lambda f: f["evidence"]["days"])


def red_flags(store: Store, buyer: str | None = None, cpv: list[str] | None = None, region: str | None = None,
              since: str | None = None, kinds: list[str] | None = None, share: float = 0.5,
              min_awards: int = 5, pct: float = 0.30, min_peers: int = 5, limit: int | None = None) -> dict:
    if not (buyer or cpv or region):
        raise ValueError("Red flags need a scope: a buyer EDRPOU, a CPV or a region.")
    chosen = list(kinds or KINDS)
    unknown = [k for k in chosen if k not in KINDS]
    if unknown:
        raise ValueError(f"Unknown flag kind(s): {', '.join(unknown)}. Known: {', '.join(KINDS)}.")
    where, params = _scope(cpv, region, since, buyer=buyer)
    found: dict[str, list[dict]] = {}
    if "single_bidder" in chosen:
        found["single_bidder"] = _single_bidder(store, where, params)
    if "supplier_concentration" in chosen:
        found["supplier_concentration"] = _concentration(store, where, params, share, min_awards)
    if "price_above_median" in chosen:
        found["price_above_median"] = _price_above_median(store, where, params, since, pct, min_peers)
    if "short_tender_period" in chosen:
        found["short_tender_period"] = _short_period(store, where, params)
    limit = clamp_limit(limit)
    flags = [f for k in chosen for f in found[k]][:limit]
    return {"counts": {k: len(v) for k, v in found.items()}, "flags": flags, "note": NOTE,
            "thresholds": {"share": share, "min_awards": min_awards, "pct": pct, "min_peers": min_peers,
                           "min_days": MIN_DAYS},
            "competitive_methods": sorted(COMPETITIVE)}
