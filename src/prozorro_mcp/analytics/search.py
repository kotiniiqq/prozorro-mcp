"""Finding things: tenders, one tender in full, upcoming plans, buyers and suppliers by name."""
from datetime import datetime, timedelta

from ..models import COMPETITIVE, OPEN_STATUSES, tender_url
from ..store import Store
from .common import NotFound, as_of, clamp_limit, money_text, paging


def _words(query: str | None) -> list[str]:
    return [w.casefold() for w in (query or "").split() if w.strip()]


def _cpv_clause(column: str, cpv: list[str] | None, params: list) -> str:
    if not cpv:
        return ""
    params.extend(f"{p}%" for p in cpv)
    return "(" + " OR ".join(f"{column} LIKE ?" for _ in cpv) + ")"


def tender_row(r) -> dict:
    return {
        "tender_id": r["tender_id"], "title": r["title"], "buyer": r["buyer_name"],
        "buyer_edrpou": r["buyer_edrpou"], "region": r["region"], "amount": r["amount"],
        "amount_text": money_text(r["amount"]), "status": r["status"], "method": r["method_type"],
        "deadline": r["tender_end"], "published": r["date_created"], "url": tender_url(r["tender_id"]),
    }


def search_tenders(store: Store, query: str | None = None, cpv: list[str] | None = None,
                   region: str | None = None, status: str = "open", amount_min: float | None = None,
                   amount_max: float | None = None, deadline_after: str | None = None,
                   published_since: str | None = None, limit: int | None = None) -> dict:
    where, params = ["t.missing = 0"], []
    moment = as_of(store)
    if status == "open":
        # Prozorro switches statuses with a delay: also require that the deadline has not passed
        where.append(f"t.status IN ({','.join('?' * len(OPEN_STATUSES))}) "
                     "AND julianday(t.tender_end) >= julianday(?)")
        params.extend([*OPEN_STATUSES, moment])
    elif status and status != "any":
        where.append("t.status = ?")
        params.append(status)
    for w in _words(query):
        where.append("(casefold(t.title) LIKE ? OR EXISTS (SELECT 1 FROM items i WHERE i.tender_id = t.id "
                     "AND casefold(i.description) LIKE ?))")
        params.extend([f"%{w}%", f"%{w}%"])
    if cpv:
        clause = _cpv_clause("i.cpv", cpv, cpv_params := [])
        where.append(f"EXISTS (SELECT 1 FROM items i WHERE i.tender_id = t.id AND {clause})")
        params.extend(cpv_params)
    if region:
        where.append("t.region = ?")
        params.append(region)
    if amount_min is not None:
        where.append("t.amount >= ?")
        params.append(amount_min)
    if amount_max is not None:
        where.append("t.amount <= ?")
        params.append(amount_max)
    if deadline_after:
        where.append("julianday(t.tender_end) >= julianday(?)")
        params.append(deadline_after)
    if published_since:
        where.append("julianday(t.date_created) >= julianday(?)")
        params.append(published_since)
    sql_where = " AND ".join(where)
    order = "julianday(t.tender_end) ASC" if status == "open" else "julianday(t.date_created) DESC"
    total = store.conn.execute(f"SELECT count(*) FROM tenders t WHERE {sql_where}", params).fetchone()[0]
    limit = clamp_limit(limit)
    rows = store.conn.execute(f"SELECT * FROM tenders t WHERE {sql_where} ORDER BY {order} LIMIT ?",
                              [*params, limit]).fetchall()
    return {**paging(total, len(rows)), "as_of": moment, "tenders": [tender_row(r) for r in rows]}


def get_tender(store: Store, key: str) -> dict:
    t = store.conn.execute("SELECT * FROM tenders WHERE id = ? OR tender_id = ?", (key, key)).fetchone()
    if not t:
        raise NotFound(f"Tender {key} is not in the local data. Check the id or sync its region "
                       f"(prozorro-mcp sync --region ...); it may also be outside the synced period.")
    tid = t["id"]
    q = lambda sql: store.conn.execute(sql, (tid,)).fetchall()
    out = tender_row(t)
    out.update({
        "competitive": t["method_type"] in COMPETITIVE,
        "tender_period": {"start": t["tender_start"], "end": t["tender_end"]},
        "missing_in_prozorro": bool(t["missing"]),
        "items": [{"description": r["description"], "cpv": r["cpv"], "cpv_name": r["cpv_name"],
                   "quantity": r["quantity"], "unit": r["unit_name"] or r["unit_code"], "lot_id": r["lot_id"]}
                  for r in q("SELECT * FROM items WHERE tender_id = ?")],
        "lots": [{"lot_id": r["lot_id"], "title": r["title"], "amount": r["amount"], "status": r["status"]}
                 for r in q("SELECT * FROM lots WHERE tender_id = ?")],
        "bids": [{"name": r["name"], "edrpou": r["edrpou"], "amount": r["amount"],
                  "amount_text": money_text(r["amount"]), "lot_id": r["lot_id"], "status": r["status"]}
                 for r in q("SELECT * FROM bids WHERE tender_id = ? ORDER BY amount IS NULL, amount")],
        "awards": [{"name": r["name"], "edrpou": r["edrpou"], "amount": r["amount"],
                    "amount_text": money_text(r["amount"]), "lot_id": r["lot_id"], "status": r["status"]}
                   for r in q("SELECT * FROM awards WHERE tender_id = ?")],
        "documents": [{"title": r["title"], "type": r["document_type"], "url": r["url"]}
                      for r in q("SELECT * FROM documents WHERE tender_id = ?")],
    })
    return out


def upcoming_plans(store: Store, query: str | None = None, cpv: list[str] | None = None,
                   region: str | None = None, budget_min: float | None = None,
                   budget_max: float | None = None, start_from: str | None = None,
                   limit: int | None = None) -> dict:
    where, params = ["tender_id IS NULL", "status = 'scheduled'"], []
    if start_from is None:
        # plans whose start passed long ago and never became tenders are noise
        start_from = (datetime.fromisoformat(as_of(store)) - timedelta(days=31)).isoformat()
    for w in _words(query):
        where.append("(casefold(description) LIKE ? OR casefold(cpv_name) LIKE ?)")
        params.extend([f"%{w}%", f"%{w}%"])
    if cpv:
        where.append(_cpv_clause("cpv", cpv, params))
    if region:
        where.append("region = ?")
        params.append(region)
    if budget_min is not None:
        where.append("budget >= ?")
        params.append(budget_min)
    if budget_max is not None:
        where.append("budget <= ?")
        params.append(budget_max)
    if start_from:
        where.append("julianday(tender_start) >= julianday(?)")
        params.append(start_from)
    sql_where = " AND ".join(where)
    total = store.conn.execute(f"SELECT count(*) FROM plans WHERE {sql_where}", params).fetchone()[0]
    limit = clamp_limit(limit)
    rows = store.conn.execute(
        f"SELECT * FROM plans WHERE {sql_where} ORDER BY julianday(tender_start) IS NULL, "
        f"julianday(tender_start) LIMIT ?", [*params, limit]).fetchall()
    plans = [{"plan_id": r["plan_id"], "description": r["description"], "cpv": r["cpv"],
              "cpv_name": r["cpv_name"], "buyer": r["buyer_name"], "buyer_edrpou": r["buyer_edrpou"],
              "region": r["region"], "budget": r["budget"], "budget_text": money_text(r["budget"]),
              "planned_tender_start": r["tender_start"],
              "url": f"https://prozorro.gov.ua/plan/{r['plan_id']}"} for r in rows]
    return {**paging(total, len(plans)), "plans": plans}


def find_entity(store: Store, name: str, role: str = "any", limit: int | None = 10) -> dict:
    where, params = [], []
    text = (name or "").strip()
    if text.isdigit():
        where.append("edrpou = ?")
        params.append(text)
    else:
        for w in _words(text):
            where.append("casefold(name) LIKE ?")
            params.append(f"%{w}%")
    if role == "buyer":
        where.append("is_buyer = 1")
    elif role == "supplier":
        where.append("is_supplier = 1")
    sql_where = " AND ".join(where) or "1"
    rows = store.conn.execute(f"SELECT * FROM entities WHERE {sql_where} ORDER BY name LIMIT ?",
                              [*params, clamp_limit(limit)]).fetchall()
    return {"entities": [{"edrpou": r["edrpou"], "name": r["name"], "is_buyer": bool(r["is_buyer"]),
                          "is_supplier": bool(r["is_supplier"])} for r in rows]}
