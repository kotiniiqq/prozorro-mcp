import json

import pytest
from factories import make_plan, make_tender

from prozorro_mcp.models import COMPETITIVE, tender_url


def rows(store, sql, *args):
    return [tuple(r) for r in store.conn.execute(sql, args).fetchall()]


def test_tender_is_normalized_into_all_tables(store):
    t = make_tender(bids=[("22222222", "ТОВ Альфа", 90000.0), ("33333333", "ТОВ Бета", 95000.0)],
                    documents=[("tz.pdf", "technicalSpecifications")])
    store.upsert_tender(t)
    (row,) = rows(store, "SELECT tender_id, buyer_edrpou, region, amount, method_type, status FROM tenders")
    assert row == (t["tenderID"], "11111111", "Львівська область", 100000.0, "aboveThreshold", "complete")
    assert rows(store, "SELECT cpv, quantity, unit_code FROM items") == [("15610000-7", 400.0, "KGM")]
    assert sorted(rows(store, "SELECT edrpou, amount FROM bids")) == [("22222222", 90000.0),
                                                                        ("33333333", 95000.0)]
    assert rows(store, "SELECT edrpou, amount, bid_id FROM awards") == [("22222222", 90000.0, "b0")]
    assert rows(store, "SELECT title, url FROM documents") == [
        ("tz.pdf", "https://public-docs.prozorro.gov.ua/tz.pdf")]
    assert store.get_raw_tender(t["id"])["tenderID"] == t["tenderID"]


def test_second_upsert_rewrites_children(store):
    t = make_tender(bids=[("22222222", "ТОВ Альфа", 90000.0), ("33333333", "ТОВ Бета", 95000.0)])
    store.upsert_tender(t)
    t2 = json.loads(json.dumps(t))
    t2["bids"] = t2["bids"][:1]
    store.upsert_tender(t2)
    assert rows(store, "SELECT edrpou FROM bids") == [("22222222",)]
    assert rows(store, "SELECT count(*) FROM tenders") == [(1,)]


def test_bids_with_lot_values_give_one_row_per_lot(store):
    t = make_tender(extra={"bids": [{"id": "b0", "status": "active",
                                     "tenderers": [{"name": "ТОВ Альфа", "identifier": {"id": "22222222"}}],
                                     "lotValues": [{"relatedLot": "L1", "value": {"amount": 10.0}},
                                                   {"relatedLot": "L2", "value": {"amount": 20.0}}]}]})
    store.upsert_tender(t)
    assert sorted(rows(store, "SELECT lot_id, amount FROM bids")) == [("L1", 10.0), ("L2", 20.0)]


def test_entities_track_buyer_and_supplier_roles(store):
    store.upsert_tender(make_tender(bids=[("22222222", "ТОВ Альфа", 90000.0)]))
    store.upsert_tender(make_tender(buyer=("22222222", "ТОВ Альфа"), bids=[]))
    assert rows(store, "SELECT edrpou, is_buyer, is_supplier FROM entities ORDER BY edrpou") == [
        ("11111111", 1, 0), ("22222222", 1, 1)]


def test_plan_round_trip(store):
    store.upsert_plan(make_plan(budget=1000.0))
    assert rows(store, "SELECT cpv, budget, region, tender_id FROM plans") == [
        ("33696500-0", 1000.0, "Львівська область", None)]


def test_missing_mark_and_state(store):
    t = make_tender()
    store.upsert_tender(t)
    store.mark_missing(t["id"])
    assert rows(store, "SELECT missing FROM tenders") == [(1,)]
    assert store.get_state("cursor:tenders") is None
    store.set_state("cursor:tenders", "123.4")
    assert store.get_state("cursor:tenders") == "123.4"


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError), store.transaction():
        store.upsert_tender(make_tender())
        raise RuntimeError("boom")
    assert rows(store, "SELECT count(*) FROM tenders") == [(0,)]


def test_tender_without_optional_parts_is_stored(store):
    t = make_tender()
    for key in ("documents", "tenderPeriod", "value"):
        t.pop(key)
    t["procuringEntity"].pop("address")
    store.upsert_tender(t)
    assert rows(store, "SELECT region, amount FROM tenders") == [(None, None)]


def test_models_helpers():
    assert "aboveThreshold" in COMPETITIVE and "reporting" not in COMPETITIVE
    assert tender_url("UA-2026-10-01-000001-a") == "https://prozorro.gov.ua/tender/UA-2026-10-01-000001-a"
