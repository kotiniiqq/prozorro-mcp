import pytest
from factories import make_tender

from prozorro_mcp.analytics.common import NotFound
from prozorro_mcp.analytics.market import (
    buyer_profile,
    competitors,
    market_overview,
    percentile,
    supplier_profile,
    unit_prices,
)

A = ("22222222", "ТОВ Альфа")
B = ("33333333", "ТОВ Бета")
C = ("44444444", "ТОВ Гамма")


def bid(party, amount):
    return (party[0], party[1], amount)


def put(store, *docs):
    for d in docs:
        store.upsert_tender(d)


def flour(price_per_kg, qty=100, **kw):
    """One-item flour tender won by Альфа at price_per_kg * qty."""
    kw.setdefault("bids", [bid(A, price_per_kg * qty), bid(B, price_per_kg * qty + 50)])
    return make_tender(items=[("15612100-2", "Борошно пшеничне", qty, "KGM")], **kw)


def test_percentile_linear_interpolation():
    assert percentile([10, 20, 30, 40], 0.5) == 25
    assert percentile([10, 20, 30, 40], 0.25) == 17.5
    assert percentile([7], 0.75) == 7


def test_unit_prices_only_single_item_tenders_and_count_the_rest(store):
    put(store, flour(10), flour(12), flour(14), flour(20),
        make_tender(items=[("15612100-2", "Борошно", 100, "KGM"), ("15612100-2", "Борошно 2", 50, "KGM")],
                    bids=[bid(A, 5000.0)]))
    res = unit_prices(store, cpv=["15612"])
    (g,) = res["groups"]
    assert (g["cpv"], g["unit"], g["n"]) == ("15612100-2", "KGM", 4)
    assert g["median"] == 13.0 and g["p25"] == 11.5 and g["p75"] == 15.5
    assert res["excluded"] == 1


def test_unit_prices_keep_units_apart(store):
    put(store, flour(10), flour(10),
        make_tender(items=[("15612100-2", "Борошно", 2, "TNE")], bids=[bid(A, 20000.0)]))
    units = {g["unit"]: g["n"] for g in unit_prices(store, cpv=["15612"])["groups"]}
    assert units == {"KGM": 2, "TNE": 1}


def test_market_overview_numbers(store):
    put(store,
        flour(10, amount=1200.0),                                       # competitive, 2 bidders, won 1000
        flour(10, amount=1500.0, bids=[bid(B, 1100.0)]),                 # competitive, 1 bidder, won 1100
        make_tender(method="reporting", amount=900.0, bids=(), winner=None, award_amount=900.0,
                    items=[("15612100-2", "Борошно", 90, "KGM")]))
    res = market_overview(store, cpv=["15612"])
    assert res["count"] == 3
    assert res["expected_sum"] == 3600.0 and res["awarded_sum"] == 3000.0
    assert res["competitive_share"] == pytest.approx(2 / 3)
    assert res["avg_bidders"] == 1.5
    assert res["top_winners"][0] == {"edrpou": "33333333", "name": "ТОВ Бета", "wins": 1, "sum": 1100.0,
                                     "sum_text": "1 100,00 грн"}
    assert res["top_buyers"][0]["count"] == 3
    assert res["awarded_sum_text"] == "3 000,00 грн"


def test_market_overview_respects_region_and_since(store):
    put(store, flour(10, region="Одеська область"), flour(10, created="2026-08-01T10:00:00+03:00"), flour(10))
    assert market_overview(store, region="Одеська область")["count"] == 1
    assert market_overview(store, since="2026-09-01T00:00:00+03:00")["count"] == 2


def test_supplier_profile_win_rate_and_discount(store):
    put(store,
        flour(9, amount=1000.0),                                        # A wins 900: discount 10%
        flour(8, amount=1000.0),                                        # A wins 800: discount 20%
        flour(10, amount=1000.0, bids=[bid(B, 950.0), bid(A, 990.0)]),   # A bids, B wins
        make_tender(status="active.tendering", bids=[bid(A, 1.0)], winner=None),  # undecided: not counted
        make_tender(method="reporting", bids=(), winner=None, award_amount=500.0))  # someone else, reporting
    p = supplier_profile(store, "22222222")
    assert p["name"] == "ТОВ Альфа"
    assert p["wins"] == 2 and p["wins_sum"] == 1700.0
    assert p["bids_in"] == 3 and p["win_rate"] == pytest.approx(2 / 3)
    assert p["median_discount"] == pytest.approx(0.15)
    assert p["buyers"][0]["edrpou"] == "11111111"
    assert p["cpvs"][0]["cpv"] == "15612100-2"


def test_buyer_profile(store):
    put(store,
        flour(10, bids=[bid(A, 1000.0)]),                                # single bidder
        flour(10),                                                       # two bidders
        flour(10, bids=[bid(B, 900.0), bid(C, 950.0)]),
        make_tender(method="reporting", bids=(), winner=None, award_amount=300.0))
    p = buyer_profile(store, "11111111")
    assert p["tenders"] == 4
    assert p["single_bidder_share"] == pytest.approx(1 / 3)
    assert p["methods"] == {"aboveThreshold": 3, "reporting": 1}
    assert p["suppliers"][0]["edrpou"] == "22222222" and p["suppliers"][0]["sum"] == 2000.0
    assert p["spend"] == 3200.0


def test_competitors_head_to_head(store):
    put(store,
        flour(10, bids=[bid(A, 1000.0), bid(B, 1100.0)]),                # A beats B
        flour(10, bids=[bid(B, 900.0), bid(A, 1000.0)]),                 # B beats A
        flour(10, bids=[bid(B, 900.0), bid(A, 1000.0)]),                 # B beats A
        flour(10, bids=[bid(A, 900.0), bid(C, 1000.0)]))                 # A beats C
    rows = competitors(store, "22222222")["rows"]
    assert rows[0] == {"edrpou": "33333333", "name": "ТОВ Бета", "shared_tenders": 3, "they_won": 2,
                       "we_won": 1}
    assert rows[1]["edrpou"] == "44444444" and rows[1]["we_won"] == 1


def test_unknown_edrpou_points_to_find_entity(store):
    with pytest.raises(NotFound) as exc:
        supplier_profile(store, "00000000")
    assert "find_entity" in str(exc.value)
    with pytest.raises(NotFound):
        buyer_profile(store, "00000000")
    with pytest.raises(NotFound):
        competitors(store, "00000000")
