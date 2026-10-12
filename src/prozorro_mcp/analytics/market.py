"""Market overview, unit prices, supplier and buyer profiles, head-to-head competitors.

Definitions (also in the README):
- competitive: procurementMethodType in models.COMPETITIVE;
- decided tender: has at least one active award;
- unit price: active award amount / quantity, only for tenders with exactly one item, grouped by full
  CPV code and unit code; other awarded tenders are counted as `excluded`;
- win rate: decided tenders the supplier won / decided tenders it bid in (bids are public only after
  the auction, so reporting and undecided tenders do not count);
- discount: (expected value - award) / expected value, competitive tenders, awards without lots.
"""
from statistics import median

from ..models import COMPETITIVE
from ..store import Store
from .common import NotFound, money_text

_COMP = ",".join(f"'{m}'" for m in sorted(COMPETITIVE))
_DECIDED = "EXISTS (SELECT 1 FROM awards a2 WHERE a2.tender_id = t.id AND a2.status = 'active')"


def percentile(values: list[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _scope(cpv: list[str] | None, region: str | None, since: str | None,
           buyer: str | None = None) -> tuple[str, list]:
    where, params = ["t.missing = 0"], []
    if cpv:
        where.append("EXISTS (SELECT 1 FROM items i WHERE i.tender_id = t.id AND ("
                     + " OR ".join("i.cpv LIKE ?" for _ in cpv) + "))")
        params.extend(f"{p}%" for p in cpv)
    if region:
        where.append("t.region = ?")
        params.append(region)
    if since:
        where.append("julianday(t.date_created) >= julianday(?)")
        params.append(since)
    if buyer:
        where.append("t.buyer_edrpou = ?")
        params.append(buyer)
    return " AND ".join(where), params


def _rows(store, sql, params):
    return store.conn.execute(sql, params).fetchall()


def _ranked(rows, n=5) -> list[dict]:
    out = []
    for r in rows[:n]:
        cols = set(r.keys())  # sqlite3.Row: `in` tests values, not column names
        item = {"edrpou": r["edrpou"], "name": r["name"]}
        if "wins" in cols:
            item["wins"] = r["wins"]
        if "cnt" in cols:
            item["count"] = r["cnt"]
        item.update({"sum": r["total"], "sum_text": money_text(r["total"])})
        out.append(item)
    return out


def unit_prices(store: Store, cpv: list[str] | None = None, region: str | None = None,
                since: str | None = None) -> dict:
    where, params = _scope(cpv, region, since)
    rows = _rows(store, f"""
        SELECT t.id, (SELECT count(*) FROM items i WHERE i.tender_id = t.id) AS n_items,
               (SELECT count(*) FROM awards a WHERE a.tender_id = t.id AND a.status = 'active') AS n_awards,
               (SELECT sum(a.amount) FROM awards a WHERE a.tender_id = t.id AND a.status = 'active') AS award,
               i.cpv, i.unit_code, i.quantity
        FROM tenders t LEFT JOIN items i ON i.tender_id = t.id
        WHERE {where} AND {_DECIDED}
        GROUP BY t.id""", params)
    groups: dict[tuple[str, str], list[float]] = {}
    excluded = 0
    for r in rows:
        if r["n_items"] != 1 or r["n_awards"] != 1 or not r["quantity"] or r["award"] is None:
            excluded += 1
            continue
        groups.setdefault((r["cpv"], r["unit_code"]), []).append(r["award"] / r["quantity"])
    out = [{"cpv": c, "unit": u, "n": len(v), "p25": round(percentile(v, 0.25), 2),
            "median": round(percentile(v, 0.5), 2), "p75": round(percentile(v, 0.75), 2)}
           for (c, u), v in groups.items()]
    out.sort(key=lambda g: -g["n"])
    return {"groups": out, "excluded": excluded,
            "note": "price per unit = award / quantity, single-item tenders only, same CPV and unit"}


def market_overview(store: Store, cpv: list[str] | None = None, region: str | None = None,
                    since: str | None = None, top: int = 5) -> dict:
    where, params = _scope(cpv, region, since)
    agg = _rows(store, f"""
        SELECT count(*) AS cnt, sum(t.amount) AS expected,
               sum(CASE WHEN t.method_type IN ({_COMP}) THEN 1 ELSE 0 END) AS comp
        FROM tenders t WHERE {where}""", params)[0]
    awarded = _rows(store, f"""
        SELECT sum(a.amount) FROM awards a JOIN tenders t ON t.id = a.tender_id
        WHERE {where} AND a.status = 'active'""", params)[0][0]
    bidders = _rows(store, f"""
        SELECT (SELECT count(DISTINCT b.edrpou) FROM bids b WHERE b.tender_id = t.id) AS n
        FROM tenders t WHERE {where} AND t.method_type IN ({_COMP})""", params)
    counts = [r["n"] for r in bidders if r["n"]]
    winners = _rows(store, f"""
        SELECT a.edrpou, max(a.name) AS name, count(*) AS wins, sum(a.amount) AS total
        FROM awards a JOIN tenders t ON t.id = a.tender_id
        WHERE {where} AND a.status = 'active' GROUP BY a.edrpou ORDER BY total DESC""", params)
    buyers = _rows(store, f"""
        SELECT t.buyer_edrpou AS edrpou, max(t.buyer_name) AS name, count(*) AS cnt, sum(t.amount) AS total
        FROM tenders t WHERE {where} GROUP BY t.buyer_edrpou ORDER BY cnt DESC, total DESC""", params)
    count = agg["cnt"] or 0
    return {
        "count": count,
        "expected_sum": agg["expected"] or 0.0, "expected_sum_text": money_text(agg["expected"] or 0.0),
        "awarded_sum": awarded or 0.0, "awarded_sum_text": money_text(awarded or 0.0),
        "competitive_share": (agg["comp"] / count) if count else None,
        "avg_bidders": (sum(counts) / len(counts)) if counts else None,
        "unit_prices": unit_prices(store, cpv, region, since),
        "top_buyers": _ranked(buyers, top),
        "top_winners": _ranked(winners, top),
    }


def _entity_name(store: Store, edrpou: str) -> str:
    row = store.conn.execute("SELECT name FROM entities WHERE edrpou = ?", (edrpou,)).fetchone()
    if row:
        return row["name"]
    row = store.conn.execute("SELECT name FROM bids WHERE edrpou = ? LIMIT 1", (edrpou,)).fetchone()
    if row:
        return row["name"]
    raise NotFound(f"EDRPOU {edrpou} does not appear in the local data. Use find_entity to look a company "
                   f"up by name, or sync more regions/days.")


def supplier_profile(store: Store, edrpou: str, since: str | None = None) -> dict:
    name = _entity_name(store, edrpou)
    where, params = _scope(None, None, since)
    wins = _rows(store, f"""
        SELECT a.amount, a.lot_id, t.amount AS expected, t.method_type, t.buyer_edrpou, t.buyer_name, t.id
        FROM awards a JOIN tenders t ON t.id = a.tender_id
        WHERE {where} AND a.status = 'active' AND a.edrpou = ?""", [*params, edrpou])
    bid_in = _rows(store, f"""
        SELECT DISTINCT t.id,
            EXISTS (SELECT 1 FROM awards a WHERE a.tender_id = t.id AND a.status = 'active'
                    AND a.edrpou = ?) AS won
        FROM tenders t JOIN bids b ON b.tender_id = t.id
        WHERE {where} AND b.edrpou = ? AND {_DECIDED}""", [edrpou, *params, edrpou])
    discounts = [(w["expected"] - w["amount"]) / w["expected"] for w in wins
                 if w["method_type"] in COMPETITIVE and w["lot_id"] is None and w["expected"]
                 and w["amount"] is not None]
    buyers: dict[str, dict] = {}
    for w in wins:
        b = buyers.setdefault(w["buyer_edrpou"], {"edrpou": w["buyer_edrpou"], "name": w["buyer_name"],
                                                   "wins": 0, "sum": 0.0})
        b["wins"] += 1
        b["sum"] += w["amount"] or 0.0
    won_ids = list({w["id"] for w in wins})
    cpvs = []
    if won_ids:
        cpvs = [{"cpv": r["cpv"], "name": r["cpv_name"], "tenders": r["n"]} for r in _rows(store, f"""
            SELECT cpv, max(cpv_name) AS cpv_name, count(DISTINCT tender_id) AS n FROM items
            WHERE tender_id IN ({",".join("?" * len(won_ids))}) GROUP BY cpv ORDER BY n DESC LIMIT 5""",
            won_ids)]
    wins_sum = sum(w["amount"] or 0.0 for w in wins)
    return {
        "edrpou": edrpou, "name": name,
        "wins": len(wins), "wins_sum": wins_sum, "wins_sum_text": money_text(wins_sum),
        "bids_in": len(bid_in),
        "win_rate": (sum(1 for r in bid_in if r["won"]) / len(bid_in)) if bid_in else None,
        "median_discount": median(discounts) if discounts else None,
        "buyers": sorted(buyers.values(), key=lambda b: -b["sum"])[:5],
        "cpvs": cpvs,
    }


def buyer_profile(store: Store, edrpou: str, since: str | None = None) -> dict:
    where, params = _scope(None, None, since, buyer=edrpou)
    tenders = _rows(store, f"""
        SELECT t.id, t.method_type, t.amount,
               (SELECT count(DISTINCT b.edrpou) FROM bids b WHERE b.tender_id = t.id) AS bidders
        FROM tenders t WHERE {where}""", params)
    name = _entity_name(store, edrpou)  # raises NotFound for unknown codes
    methods: dict[str, int] = {}
    for t in tenders:
        methods[t["method_type"]] = methods.get(t["method_type"], 0) + 1
    with_bids = [t for t in tenders if t["method_type"] in COMPETITIVE and t["bidders"]]
    suppliers = _rows(store, f"""
        SELECT a.edrpou, max(a.name) AS name, count(*) AS wins, sum(a.amount) AS total
        FROM awards a JOIN tenders t ON t.id = a.tender_id
        WHERE {where} AND a.status = 'active' GROUP BY a.edrpou ORDER BY total DESC""", params)
    cpvs = _rows(store, f"""
        SELECT i.cpv, max(i.cpv_name) AS cpv_name, count(DISTINCT t.id) AS n
        FROM items i JOIN tenders t ON t.id = i.tender_id WHERE {where}
        GROUP BY i.cpv ORDER BY n DESC LIMIT 5""", params)
    spend = sum(r["total"] or 0.0 for r in suppliers)
    expected = sum(t["amount"] or 0.0 for t in tenders)
    return {
        "edrpou": edrpou, "name": name, "tenders": len(tenders),
        "expected_sum": expected, "expected_sum_text": money_text(expected),
        "spend": spend, "spend_text": money_text(spend),
        "methods": methods,
        "single_bidder_share": (sum(1 for t in with_bids if t["bidders"] == 1) / len(with_bids))
        if with_bids else None,
        "suppliers": _ranked(suppliers, 5),
        "cpvs": [{"cpv": r["cpv"], "name": r["cpv_name"], "tenders": r["n"]} for r in cpvs],
    }


def competitors(store: Store, edrpou: str, since: str | None = None, top: int = 10) -> dict:
    name = _entity_name(store, edrpou)
    where, params = _scope(None, None, since)
    rows = _rows(store, f"""
        SELECT o.edrpou, max(o.name) AS name, count(DISTINCT t.id) AS shared,
            count(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM awards a WHERE a.tender_id = t.id
                  AND a.status = 'active' AND a.edrpou = o.edrpou) THEN t.id END) AS they_won,
            count(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM awards a WHERE a.tender_id = t.id
                  AND a.status = 'active' AND a.edrpou = ?) THEN t.id END) AS we_won
        FROM tenders t
        JOIN bids me ON me.tender_id = t.id AND me.edrpou = ?
        JOIN bids o ON o.tender_id = t.id AND o.edrpou != ?
        WHERE {where} GROUP BY o.edrpou ORDER BY shared DESC, they_won DESC LIMIT ?""",
        [edrpou, edrpou, edrpou, *params, top])
    return {"edrpou": edrpou, "name": name,
            "rows": [{"edrpou": r["edrpou"], "name": r["name"], "shared_tenders": r["shared"],
                      "they_won": r["they_won"], "we_won": r["we_won"]} for r in rows]}
