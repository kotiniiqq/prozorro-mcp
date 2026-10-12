"""Bundled demo: a small real snapshot (one oblast, about a week) loaded into a cached SQLite file.

The snapshot keeps only the fields the store reads, with contact persons removed.
"""
import gzip
import hashlib
import json
import os
from importlib import resources
from pathlib import Path

from .store import Store

DEMO_FILE = Path(str(resources.files("prozorro_mcp").joinpath("data", "demo.jsonl.gz")))


def _pick(d: dict | None, keys) -> dict | None:
    if not d:
        return d
    return {k: d[k] for k in keys if k in d}


def _party(p: dict | None) -> dict | None:
    if not p:
        return p
    out = _pick(p, ("name", "identifier"))
    out["identifier"] = _pick(p.get("identifier"), ("scheme", "id", "legalName"))
    if p.get("address"):
        out["address"] = _pick(p["address"], ("region", "locality", "countryName"))
    return out


def slim_tender(raw: dict) -> dict:
    t = _pick(raw, ("id", "tenderID", "title", "status", "procurementMethodType", "dateCreated", "date",
                    "dateModified", "value", "tenderPeriod"))
    t["procuringEntity"] = _party(raw.get("procuringEntity"))
    t["items"] = [{**_pick(it, ("id", "description", "quantity", "relatedLot")),
                   "classification": _pick(it.get("classification"), ("scheme", "id", "description")),
                   "unit": _pick(it.get("unit"), ("code", "name"))} for it in raw.get("items") or []]
    if raw.get("lots"):
        t["lots"] = [_pick(lot, ("id", "title", "value", "status")) for lot in raw["lots"]]
    if raw.get("bids"):
        t["bids"] = [{**_pick(b, ("id", "status", "value")),
                      "tenderers": [_party(x) for x in b.get("tenderers") or []],
                      **({"lotValues": [_pick(lv, ("relatedLot", "value")) for lv in b["lotValues"]]}
                         if b.get("lotValues") else {})} for b in raw["bids"]]
    if raw.get("awards"):
        t["awards"] = [{**_pick(a, ("id", "bid_id", "lotID", "status", "date", "value")),
                        "suppliers": [_party(x) for x in a.get("suppliers") or []]} for a in raw["awards"]]
    if raw.get("documents"):
        t["documents"] = [_pick(d, ("title", "documentType", "url", "datePublished")) for d in raw["documents"]]
    return t


def slim_plan(raw: dict) -> dict:
    p = _pick(raw, ("id", "planID", "status", "dateModified", "classification", "budget", "tender_id"))
    p["procuringEntity"] = _party(raw.get("procuringEntity"))
    tender = raw.get("tender") or {}
    p["tender"] = {"procurementMethodType": tender.get("procurementMethodType"),
                   "tenderPeriod": _pick(tender.get("tenderPeriod"), ("startDate",))}
    if p.get("budget"):
        p["budget"] = _pick(p["budget"], ("amount", "currency", "description"))
    return p


def _cache_dir() -> Path:
    return Path(os.environ.get("PROZORRO_MCP_CACHE") or Path.home() / ".cache" / "prozorro-mcp")


def demo_store() -> Store:
    """Open the demo database, building it from the bundled snapshot on first use."""
    digest = hashlib.sha256(DEMO_FILE.read_bytes()).hexdigest()[:12]
    target = _cache_dir() / f"demo-{digest}.db"
    if not target.exists():
        tmp = target.with_suffix(".building")
        for leftover in tmp.parent.glob(tmp.name + "*") if tmp.parent.exists() else []:
            leftover.unlink()
        store = Store(tmp)
        with gzip.open(DEMO_FILE, "rt", encoding="utf-8") as f, store.transaction():
            for line in f:
                rec = json.loads(line)
                if rec["kind"] == "meta":
                    for key, value in rec["state"].items():
                        store.set_state(key, value)
                elif rec["kind"] == "tender":
                    store.upsert_tender(rec["doc"])
                else:
                    store.upsert_plan(rec["doc"])
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.close()
        os.replace(tmp, target)
    return Store(target)
