"""Saved searches and the digest of tenders published since a moment (default: the last 24 hours)."""
import sqlite3
from datetime import datetime, timedelta

from ..store import Store
from .common import NotFound
from .search import search_tenders


def watch_add(store: Store, name: str, query: str | None = None, cpv: list[str] | None = None,
              region: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("A saved search needs a name.")
    if not (query or cpv or region):
        raise ValueError("A saved search needs at least one of: words, CPV, region.")
    try:
        with store.transaction():
            store.conn.execute("INSERT INTO watches(name, query, cpv, region, created) VALUES (?,?,?,?,?)",
                               (name, query, ",".join(cpv or []) or None, region,
                                datetime.now().astimezone().isoformat()))
    except sqlite3.IntegrityError:
        raise ValueError(f"A saved search named '{name}' already exists.") from None
    return {"name": name, "query": query, "cpv": cpv or [], "region": region}


def watch_list(store: Store) -> list[dict]:
    rows = store.conn.execute("SELECT * FROM watches ORDER BY name").fetchall()
    return [{"name": r["name"], "query": r["query"], "cpv": r["cpv"].split(",") if r["cpv"] else [],
             "region": r["region"], "created": r["created"]} for r in rows]


def watch_remove(store: Store, name: str) -> None:
    with store.transaction():
        cur = store.conn.execute("DELETE FROM watches WHERE name = ?", (name,))
    if cur.rowcount == 0:
        raise NotFound(f"No saved search named '{name}'.")


def watch_digest(store: Store, since: str | None = None, limit: int | None = None,
                 now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    since = since or (now - timedelta(hours=24)).isoformat()
    watches = watch_list(store)
    out = []
    for w in watches:
        res = search_tenders(store, query=w["query"], cpv=w["cpv"] or None, region=w["region"],
                             status="any", published_since=since, limit=limit)
        out.append({"name": w["name"], "total": res["total"], "tenders": res["tenders"],
                    **({"note": res["note"]} if "note" in res else {})})
    result = {"since": since, "watches": out}
    if not watches:
        result["hint"] = ('No saved searches yet. Add one: prozorro-mcp watch add "name" --query "words" '
                          '[--cpv 15] [--region Львівська]')
    return result
