import pytest
from factories import make_tender

from prozorro_mcp import evals
from prozorro_mcp.store import Store


def test_flag_cases_cover_every_kind_with_positives_and_negatives():
    cases = evals.load_cases("flags.jsonl")
    assert len(cases) >= 30
    for kind in ("single_bidder", "supplier_concentration", "price_above_median", "short_tender_period"):
        labels = {c["expect"] for c in cases if c["kind"] == kind}
        assert labels == {True, False}, kind


def test_flags_suite_reports_precision_recall_per_kind():
    res = evals.run_flags()
    assert set(res["per_kind"]) == {"single_bidder", "supplier_concentration", "price_above_median",
                                    "short_tender_period"}
    for m in res["per_kind"].values():
        assert set(m) >= {"tp", "fp", "fn", "tn", "precision", "recall"}
    assert res["cases"] >= 30


def test_flags_suite_notices_a_broken_rule(monkeypatch):
    from prozorro_mcp.analytics import flags
    monkeypatch.setattr(flags, "MIN_DAYS", {**flags.MIN_DAYS, "aboveThreshold": 1})
    res = evals.run_flags()
    assert res["per_kind"]["short_tender_period"]["recall"] < 1
    assert not res["passed"]


def test_normalize_suite():
    res = evals.run_normalize()
    assert res["cases"] >= 60 and 0 <= res["accuracy"] <= 1 and "failures" in res


def test_reconcile_matches_an_independent_recount(tmp_path):
    store = Store(tmp_path / "r.db")
    for k in range(6):
        store.upsert_tender(make_tender(buyer=("11111111", "Лікарня"), amount=1000.0 + k,
                                        bids=[("22222222", "А", 900.0), ("33333333", "Б", 950.0)][: 1 + k % 2]))
    store.upsert_tender(make_tender(buyer=("55555555", "Школа"), method="reporting", bids=(), winner=None,
                                    award_amount=300.0))
    res = evals.run_reconcile(store)
    assert res["checked"] > 0 and res["mismatches"] == [] and res["passed"]


def test_reconcile_catches_a_wrong_aggregate(tmp_path, monkeypatch):
    store = Store(tmp_path / "r.db")
    store.upsert_tender(make_tender(bids=[("22222222", "А", 900.0)]))
    from prozorro_mcp.analytics import market
    real = market.market_overview

    def broken(*a, **kw):
        out = real(*a, **kw)
        out["awarded_sum"] += 1
        return out

    monkeypatch.setattr(market, "market_overview", broken)
    res = evals.run_reconcile(store)
    assert res["mismatches"] and not res["passed"]


def test_unknown_suite():
    with pytest.raises(ValueError):
        evals.run_suite("vibes")
