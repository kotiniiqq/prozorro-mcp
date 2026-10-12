import pytest
from factories import make_tender

from prozorro_mcp.analytics.flags import red_flags

A = ("22222222", "ТОВ Альфа")
B = ("33333333", "ТОВ Бета")
LVIV = "Львівська область"


def put(store, *docs):
    for d in docs:
        store.upsert_tender(d)


def kinds(res, kind):
    return [f for f in res["flags"] if f["kind"] == kind]


def test_scope_is_required(store):
    with pytest.raises(ValueError):
        red_flags(store)


def test_single_bidder_flags_competitive_only(store):
    put(store,
        make_tender(id="one", bids=[(*A, 100.0)]),
        make_tender(id="two", bids=[(*A, 100.0), (*B, 110.0)]),
        make_tender(id="rep", method="reporting", bids=(), winner=None, award_amount=100.0),
        make_tender(id="neg", method="negotiation", bids=[(*A, 100.0)]))
    res = red_flags(store, region=LVIV, kinds=["single_bidder"])
    flagged = kinds(res, "single_bidder")
    assert [f["tender_id"] for f in flagged] == [store.get_raw_tender("one")["tenderID"]]
    assert flagged[0]["evidence"]["bidders"] == 1 and flagged[0]["url"].startswith("https://prozorro")


def test_single_bidder_ignores_tenders_still_running(store):
    put(store, make_tender(status="active.tendering", bids=[(*A, 100.0)], winner=None))
    assert red_flags(store, region=LVIV, kinds=["single_bidder"])["counts"]["single_bidder"] == 0


def test_supplier_concentration_needs_enough_awards(store):
    put(store, *[make_tender(bids=[(*A, 100.0)]) for _ in range(4)], make_tender(bids=[(*B, 100.0)]))
    res = red_flags(store, buyer="11111111", kinds=["supplier_concentration"])
    (f,) = kinds(res, "supplier_concentration")
    assert f["evidence"]["supplier_edrpou"] == "22222222"
    assert f["evidence"]["share"] == pytest.approx(0.8) and f["evidence"]["awards"] == 5
    assert red_flags(store, buyer="11111111", kinds=["supplier_concentration"],
                     min_awards=6)["counts"]["supplier_concentration"] == 0


def test_supplier_concentration_silent_when_spread(store):
    put(store, *[make_tender(bids=[(*A, 100.0)]) for _ in range(2)],
        *[make_tender(bids=[(*B, 100.0)]) for _ in range(3)])
    # Бета holds 60% > 50% -> flagged; raise the threshold and it is silent
    assert red_flags(store, buyer="11111111", kinds=["supplier_concentration"])["counts"][
        "supplier_concentration"] == 1
    assert red_flags(store, buyer="11111111", kinds=["supplier_concentration"], share=0.6)["counts"][
        "supplier_concentration"] == 0


def flour(price, **kw):
    return make_tender(items=[("15612100-2", "Борошно", 100, "KGM")], bids=[(*A, price * 100)], **kw)


def test_price_above_median_with_enough_peers(store):
    put(store, *[flour(10) for _ in range(5)], flour(13, id="dear"), flour(12.9, id="edge"))
    res = red_flags(store, region=LVIV, kinds=["price_above_median"])
    flagged = kinds(res, "price_above_median")
    assert [f["tender_id"] for f in flagged] == [store.get_raw_tender("dear")["tenderID"]]
    ev = flagged[0]["evidence"]
    assert ev["unit_price"] == 13.0 and ev["median"] == 10.0 and ev["peers"] == 7
    assert ev["pct_above"] == pytest.approx(0.30)


def test_price_above_median_silent_with_few_peers(store):
    put(store, flour(10), flour(10), flour(20))
    assert red_flags(store, region=LVIV, kinds=["price_above_median"])["counts"]["price_above_median"] == 0


def test_short_tender_period_per_method(store):
    put(store,
        make_tender(id="short", method="aboveThreshold", tender_days=5, bids=[(*A, 1.0), (*B, 2.0)]),
        make_tender(id="ok", method="aboveThreshold", tender_days=7, bids=[(*A, 1.0), (*B, 2.0)]),
        make_tender(id="pq", method="priceQuotation", tender_days=3, bids=[(*A, 1.0), (*B, 2.0)]),
        make_tender(id="eu", method="aboveThresholdEU", tender_days=10, bids=[(*A, 1.0), (*B, 2.0)]))
    res = red_flags(store, region=LVIV, kinds=["short_tender_period"])
    flagged = sorted(f["tender_id"] for f in kinds(res, "short_tender_period"))
    assert flagged == sorted([store.get_raw_tender("short")["tenderID"], store.get_raw_tender("eu")["tenderID"]])
    ev = next(f for f in res["flags"] if f["tender_id"] == store.get_raw_tender("short")["tenderID"])["evidence"]
    assert ev == {"days": 5.0, "min_days": 7, "method": "aboveThreshold"}


def test_result_has_counts_note_and_limit(store):
    put(store, *[make_tender(bids=[(*A, 100.0)]) for _ in range(5)])
    res = red_flags(store, region=LVIV, kinds=["single_bidder"], limit=2)
    assert res["counts"]["single_bidder"] == 5 and len(res["flags"]) == 2
    assert "not proof" in res["note"]


def test_unknown_kind_is_rejected(store):
    with pytest.raises(ValueError):
        red_flags(store, region=LVIV, kinds=["bribery"])
