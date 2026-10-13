"""SQLite storage: Prozorro tenders and plans normalized into tables, raw JSON kept alongside."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tenders (
    id TEXT PRIMARY KEY, tender_id TEXT, title TEXT, status TEXT, method_type TEXT,
    buyer_edrpou TEXT, buyer_name TEXT, region TEXT, amount REAL, currency TEXT,
    date_created TEXT, date_modified TEXT, tender_start TEXT, tender_end TEXT,
    missing INTEGER NOT NULL DEFAULT 0, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lots (tender_id TEXT, lot_id TEXT, title TEXT, amount REAL, status TEXT);
CREATE TABLE IF NOT EXISTS items (tender_id TEXT, lot_id TEXT, cpv TEXT, cpv_name TEXT, quantity REAL,
    unit_code TEXT, unit_name TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS bids (tender_id TEXT, bid_id TEXT, lot_id TEXT, edrpou TEXT, name TEXT,
    amount REAL, status TEXT);
CREATE TABLE IF NOT EXISTS awards (tender_id TEXT, award_id TEXT, lot_id TEXT, bid_id TEXT, edrpou TEXT,
    name TEXT, amount REAL, status TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS documents (tender_id TEXT, title TEXT, document_type TEXT, url TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY, plan_id TEXT, status TEXT, buyer_edrpou TEXT, buyer_name TEXT, region TEXT,
    cpv TEXT, cpv_name TEXT, description TEXT, budget REAL, tender_start TEXT, tender_id TEXT,
    date_modified TEXT, raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (edrpou TEXT PRIMARY KEY, name TEXT,
    is_buyer INTEGER NOT NULL DEFAULT 0, is_supplier INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS watches (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, query TEXT,
    cpv TEXT, region TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_tenders_scope ON tenders(region, status, date_created);
CREATE INDEX IF NOT EXISTS ix_tenders_buyer ON tenders(buyer_edrpou);
CREATE INDEX IF NOT EXISTS ix_items_cpv ON items(cpv);
CREATE INDEX IF NOT EXISTS ix_items_tender ON items(tender_id);
CREATE INDEX IF NOT EXISTS ix_bids_edrpou ON bids(edrpou);
CREATE INDEX IF NOT EXISTS ix_bids_tender ON bids(tender_id);
CREATE INDEX IF NOT EXISTS ix_awards_edrpou ON awards(edrpou);
CREATE INDEX IF NOT EXISTS ix_awards_tender ON awards(tender_id);
CREATE INDEX IF NOT EXISTS ix_plans_scope ON plans(region, status);
"""

CHILD_TABLES = ("lots", "items", "bids", "awards", "documents")


def _party(p: dict | None) -> tuple[str | None, str | None]:
    if not p:
        return None, None
    ident = p.get("identifier") or {}
    return ident.get("id"), ident.get("legalName") or p.get("name")


def _amount(obj: dict | None) -> float | None:
    if not obj or obj.get("amount") is None:
        return None
    return float(obj["amount"])


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # several server copies share one file (Claude Desktop starts two or three at once): wait on locks
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        # SQLite's LIKE/lower() only fold ASCII; Cyrillic search needs Python's casefold
        self._conn.create_function("casefold", 1, lambda v: v.casefold() if isinstance(v, str) else v,
                                   deterministic=True)
        self._depth = 0
        for attempt in range(50):
            try:
                self._init_schema()
                break
            except sqlite3.OperationalError as exc:
                # switching a new file to WAL ignores the busy timeout, so retry while another copy does it
                if "locked" not in str(exc) or attempt == 49:
                    raise
                self._conn.rollback()
                time.sleep(0.1)

    def _init_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        if not self._conn.execute("SELECT 1 FROM schema_version").fetchone():
            self._conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        self._conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self):
        """Nested-safe: only the outermost block commits or rolls back."""
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.rollback()
            raise
        self._depth -= 1
        if self._depth == 0:
            self._conn.commit()

    def _entity(self, edrpou: str | None, name: str | None, *, buyer=False, supplier=False) -> None:
        if not edrpou:
            return
        self._conn.execute(
            "INSERT INTO entities(edrpou, name, is_buyer, is_supplier) VALUES (?,?,?,?) "
            "ON CONFLICT(edrpou) DO UPDATE SET name=coalesce(excluded.name, name), "
            "is_buyer=max(is_buyer, excluded.is_buyer), is_supplier=max(is_supplier, excluded.is_supplier)",
            (edrpou, name, int(buyer), int(supplier)))

    def upsert_tender(self, raw: dict) -> None:
        tid = raw["id"]
        buyer = raw.get("procuringEntity") or {}
        b_id, b_name = _party(buyer)
        value = raw.get("value") or {}
        period = raw.get("tenderPeriod") or {}
        with self.transaction():
            c = self._conn
            for table in CHILD_TABLES:
                c.execute(f"DELETE FROM {table} WHERE tender_id=?", (tid,))
            c.execute(
                "INSERT OR REPLACE INTO tenders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (tid, raw.get("tenderID"), raw.get("title"), raw.get("status"),
                 raw.get("procurementMethodType"), b_id, b_name,
                 (buyer.get("address") or {}).get("region") or None, _amount(value), value.get("currency"),
                 raw.get("dateCreated") or raw.get("date"), raw.get("dateModified"),
                 period.get("startDate"), period.get("endDate"), json.dumps(raw, ensure_ascii=False)))
            self._entity(b_id, b_name, buyer=True)
            for lot in raw.get("lots") or []:
                c.execute("INSERT INTO lots VALUES (?,?,?,?,?)",
                          (tid, lot.get("id"), lot.get("title"), _amount(lot.get("value")), lot.get("status")))
            for it in raw.get("items") or []:
                cls = it.get("classification") or {}
                unit = it.get("unit") or {}
                c.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
                          (tid, it.get("relatedLot"), cls.get("id"), cls.get("description"),
                           it.get("quantity"), unit.get("code"), unit.get("name"), it.get("description")))
            for bid in raw.get("bids") or []:
                tenderers = bid.get("tenderers") or [{}]
                e_id, e_name = _party(tenderers[0])
                if bid.get("lotValues"):
                    parts = [(lv.get("relatedLot"), _amount(lv.get("value"))) for lv in bid["lotValues"]]
                else:
                    parts = [(None, _amount(bid.get("value")))]
                for lot_id, amount in parts:
                    c.execute("INSERT INTO bids VALUES (?,?,?,?,?,?,?)",
                              (tid, bid.get("id"), lot_id, e_id, e_name, amount, bid.get("status")))
            for aw in raw.get("awards") or []:
                suppliers = aw.get("suppliers") or [{}]
                e_id, e_name = _party(suppliers[0])
                c.execute("INSERT INTO awards VALUES (?,?,?,?,?,?,?,?,?)",
                          (tid, aw.get("id"), aw.get("lotID"), aw.get("bid_id"), e_id, e_name,
                           _amount(aw.get("value")), aw.get("status"), aw.get("date")))
                if aw.get("status") == "active":
                    self._entity(e_id, e_name, supplier=True)
            for doc in raw.get("documents") or []:
                c.execute("INSERT INTO documents VALUES (?,?,?,?,?)",
                          (tid, doc.get("title"), doc.get("documentType"), doc.get("url"),
                           doc.get("datePublished")))

    def upsert_plan(self, raw: dict) -> None:
        buyer = raw.get("procuringEntity") or {}
        b_id, b_name = _party(buyer)
        cls = raw.get("classification") or {}
        budget = raw.get("budget") or {}
        start = ((raw.get("tender") or {}).get("tenderPeriod") or {}).get("startDate")
        with self.transaction():
            self._conn.execute(
                "INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (raw["id"], raw.get("planID"), raw.get("status"), b_id, b_name,
                 (buyer.get("address") or {}).get("region") or None, cls.get("id"), cls.get("description"),
                 budget.get("description"), _amount(budget), start, raw.get("tender_id"),
                 raw.get("dateModified"), json.dumps(raw, ensure_ascii=False)))
            self._entity(b_id, b_name, buyer=True)

    def get_raw_tender(self, tender_id: str) -> dict | None:
        row = self._conn.execute("SELECT raw FROM tenders WHERE id=?", (tender_id,)).fetchone()
        return json.loads(row["raw"]) if row else None

    def mark_missing(self, tender_id: str) -> None:
        with self.transaction():
            self._conn.execute("UPDATE tenders SET missing=1 WHERE id=?", (tender_id,))

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.transaction():
            self._conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)", (key, value))
