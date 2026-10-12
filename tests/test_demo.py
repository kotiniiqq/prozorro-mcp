import gzip
import json
from pathlib import Path

import pytest
from factories import make_plan, make_tender

from prozorro_mcp.demo import DEMO_FILE, demo_store, slim_plan, slim_tender
from prozorro_mcp.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def table(store: Store, sql: str):
    return [tuple(r) for r in store.conn.execute(sql).fetchall()]


def test_slim_tender_keeps_everything_the_store_reads(tmp_path):
    raw = json.loads((FIXTURES / "tender_competitive.json").read_text(encoding="utf-8"))
    full, slim = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    full.upsert_tender(raw)
    slim.upsert_tender(slim_tender(raw))
    for t in ("items", "bids", "awards", "documents", "lots", "entities"):
        assert table(full, f"SELECT * FROM {t} ORDER BY 1, 2") == table(slim, f"SELECT * FROM {t} ORDER BY 1, 2"), t
    cols = "tender_id, title, status, method_type, buyer_edrpou, region, amount, date_created, tender_end"
    assert table(full, f"SELECT {cols} FROM tenders") == table(slim, f"SELECT {cols} FROM tenders")
    assert len(json.dumps(slim_tender(raw))) < len(json.dumps(raw)) / 3


def test_slim_handles_multi_lot_and_plans(tmp_path):
    raw = json.loads((FIXTURES / "tender_multi_lot.json").read_text(encoding="utf-8"))
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    a.upsert_tender(raw)
    b.upsert_tender(slim_tender(raw))
    assert table(a, "SELECT * FROM bids ORDER BY 1,2,3") == table(b, "SELECT * FROM bids ORDER BY 1,2,3")
    plan = make_plan()
    assert slim_plan(plan)["classification"] == plan["classification"]
    t = make_tender(bids=[("1", "x", 1.0)])
    assert "contactPoint" not in json.dumps(slim_tender(t))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PROZORRO_MCP_CACHE", str(tmp_path / "cache"))
    return tmp_path / "cache"


def test_bundled_snapshot_is_small_and_has_no_contacts():
    assert DEMO_FILE.exists()
    assert DEMO_FILE.stat().st_size < 5 * 1024 * 1024
    text = gzip.decompress(DEMO_FILE.read_bytes()).decode("utf-8")
    assert "contactPoint" not in text and "signerInfo" not in text and "telephone" not in text


def test_demo_store_is_built_once_and_has_real_competition(cache):
    s = demo_store()
    path = s.path
    n = s.conn.execute("SELECT count(*) FROM tenders").fetchone()[0]
    with_bids = s.conn.execute("SELECT count(DISTINCT tender_id) FROM bids").fetchone()[0]
    s.close()
    assert n > 500 and with_bids > 50
    s2 = demo_store()
    assert s2.path == path and s2.conn.execute("SELECT value FROM sync_state WHERE key='coverage'").fetchone()
    s2.close()
