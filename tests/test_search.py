import asyncio
import json

import pytest
from factories import make_plan, make_tender

from prozorro_mcp.analytics.common import NotFound, coverage_hint, coverage_info, money_text
from prozorro_mcp.analytics.search import find_entity, get_tender, search_tenders, upcoming_plans
from prozorro_mcp.sync import Coverage


def put(store, *docs):
    for d in docs:
        (store.upsert_plan if d["id"].startswith("p") else store.upsert_tender)(d)


def cover(store, regions=("Львівська область",), since="2026-10-01T00:00:00+00:00"):
    import json
    from dataclasses import asdict
    store.set_state("coverage", json.dumps(asdict(Coverage(list(regions), 14, [], since)), ensure_ascii=False))


def test_money_text():
    assert money_text(1234567.891) == "1 234 567,89 грн"
    assert money_text(None) is None


def test_open_tenders_by_default_soonest_deadline_first(store):
    put(store,
        make_tender(id="late", status="active.tendering", tender_days=20),
        make_tender(id="soon", status="active.tendering", tender_days=3),
        make_tender(id="done", status="complete"))
    res = search_tenders(store)
    assert [r["tender_id"] for r in res["tenders"]] == [
        store.get_raw_tender("soon")["tenderID"], store.get_raw_tender("late")["tenderID"]]
    assert res["total"] == 2
    row = res["tenders"][0]
    assert row["url"].startswith("https://prozorro.gov.ua/tender/UA-")
    assert row["amount_text"] == "100 000,00 грн" and row["buyer"] == "КНП Лікарня №1"


def test_words_are_anded_case_insensitive_over_title_and_items(store):
    put(store,
        make_tender(id="a", status="active.tendering", title="Закупівля БОРОШНА пшеничного"),
        make_tender(id="b", status="active.tendering", title="Закупівля палива",
                    items=[("09132000-3", "Бензин А-95", 100, "LTR")]),
        make_tender(id="c", status="active.tendering", title="Закупівля борошна житнього"))
    assert search_tenders(store, query="борошна пшеничного")["total"] == 1
    assert search_tenders(store, query="бензин")["total"] == 1  # matched in item description


def test_filters_cpv_region_amount_status_dates(store):
    put(store,
        make_tender(id="a", status="active.tendering", amount=50000.0),
        make_tender(id="b", status="active.tendering", amount=500000.0, region="Одеська область"),
        make_tender(id="c", status="complete", amount=70000.0, created="2026-09-01T10:00:00+03:00",
                    items=[("09132000-3", "Бензин", 10, "LTR")]))
    assert search_tenders(store, region="Одеська область")["total"] == 1
    assert search_tenders(store, amount_min=100000)["total"] == 1
    assert search_tenders(store, amount_max=60000)["total"] == 1
    assert search_tenders(store, status="any", cpv=["0913"])["total"] == 1
    assert search_tenders(store, status="complete")["total"] == 1
    assert search_tenders(store, status="any", published_since="2026-09-15T00:00:00+03:00")["total"] == 2
    assert search_tenders(store, deadline_after="2026-10-05T00:00:00+03:00")["total"] == 2


def test_dates_with_different_offsets_compare_correctly(store):
    # 2026-10-26T00:00+02:00 is later than 2026-10-25T23:30+03:00
    put(store, make_tender(id="a", status="active.tendering",
                           extra={"tenderPeriod": {"startDate": "2026-10-01T10:00:00+03:00",
                                                   "endDate": "2026-10-26T00:00:00+02:00"}}))
    assert search_tenders(store, deadline_after="2026-10-25T23:30:00+03:00")["total"] == 1


def test_pagination_note(store):
    put(store, *[make_tender(status="active.tendering") for _ in range(25)])
    res = search_tenders(store, limit=20)
    assert res["shown"] == 20 and res["total"] == 25 and "20 of 25" in res["note"]


def test_missing_tenders_are_hidden(store):
    put(store, make_tender(id="a", status="active.tendering"))
    store.mark_missing("a")
    assert search_tenders(store)["total"] == 0


def test_get_tender_by_ua_id_with_bids_awards_documents(store):
    t = make_tender(bids=[("33333333", "ТОВ Бета", 95000.0), ("22222222", "ТОВ Альфа", 90000.0)], winner=1,
                    documents=[("tz.pdf", "technicalSpecifications")])
    put(store, t)
    res = get_tender(store, t["tenderID"])
    assert [b["name"] for b in res["bids"]] == ["ТОВ Альфа", "ТОВ Бета"]  # cheapest first
    assert res["awards"][0]["edrpou"] == "22222222"
    assert res["documents"] == [{"title": "tz.pdf", "type": "technicalSpecifications",
                                 "url": "https://public-docs.prozorro.gov.ua/tz.pdf"}]
    assert res["items"][0]["cpv"] == "15610000-7" and res["competitive"] is True
    assert get_tender(store, t["id"])["tender_id"] == t["tenderID"]


def test_get_tender_unknown(store):
    with pytest.raises(NotFound):
        get_tender(store, "UA-2000-01-01-000000-a")


def test_upcoming_plans_exclude_already_tendered(store):
    put(store,
        make_plan(id="p1", budget=300000.0),
        make_plan(id="p2", tender_id="t-already"),
        make_plan(id="p3", region="Одеська область"),
        make_plan(id="p4", cpv="09132000-3", description="Бензин", tender_start="2026-12-01T00:00:00+02:00"))
    res = upcoming_plans(store, region="Львівська область")
    assert sorted(r["plan_id"] for r in res["plans"]) == sorted(
        r[0] for r in store.conn.execute("SELECT plan_id FROM plans WHERE id IN ('p1','p4')"))
    assert upcoming_plans(store, cpv=["0913"])["total"] == 1
    assert upcoming_plans(store, query="реагенти")["total"] == 2
    assert upcoming_plans(store, budget_min=260000)["total"] == 1
    assert upcoming_plans(store, start_from="2026-11-15T00:00:00+02:00")["total"] == 1


def test_find_entity_partial_cyrillic_case_insensitive(store):
    put(store, make_tender(buyer=("12345678", "КП «Київводоканал»"),
                           bids=[("87654321", "ТОВ Водосервіс", 1.0)]))
    assert find_entity(store, "київводоканал")["entities"][0]["edrpou"] == "12345678"
    assert find_entity(store, "водо", role="supplier")["entities"][0]["edrpou"] == "87654321"
    assert find_entity(store, "12345678")["entities"][0]["name"] == "КП «Київводоканал»"


def test_coverage_hint_for_unsynced_region_and_period(store):
    assert "prozorro-mcp sync --region" in coverage_hint(store)
    cover(store)
    assert coverage_hint(store, region="Львівська область") is None
    hint = coverage_hint(store, region="Одеська область")
    assert 'prozorro-mcp sync --region "Одеська область"' in hint
    assert "--days" in coverage_hint(store, since="2026-01-01T00:00:00+02:00")


def test_coverage_info_counts(store):
    cover(store)
    put(store, make_tender(), make_plan())
    info = coverage_info(store)
    assert info["regions"] == ["Львівська область"] and info["tenders"] == 1 and info["plans"] == 1


def test_sync_records_last_sync_time(store):
    from test_sync import NOW, FakeAPI

    from prozorro_mcp.sync import run_sync
    asyncio.run(run_sync(FakeAPI(), store, regions=["Львівська область"], plans=False, now=lambda: NOW))
    assert coverage_info(store)["last_sync"].startswith("2026-10-12")


def test_real_tender_fixture_reads_back_in_full(store):
    import json
    from pathlib import Path
    raw = json.loads((Path(__file__).parent / "fixtures" / "tender_competitive.json").read_text(encoding="utf-8"))
    store.upsert_tender(raw)
    res = get_tender(store, raw["tenderID"])
    amounts = [b["amount"] for b in res["bids"]]
    assert len(amounts) == len(raw["bids"]) and amounts == sorted(amounts)
    assert res["awards"] and res["awards"][0]["status"] == "active"
    assert all(d["url"].startswith("https://") for d in res["documents"])


def test_parse_since_relative_and_absolute():
    from datetime import datetime

    from prozorro_mcp.analytics.common import parse_since
    now = datetime.fromisoformat("2026-10-12T12:00:00+03:00")
    assert parse_since("7d", now) == "2026-10-05T12:00:00+03:00"
    assert parse_since("24h", now) == "2026-10-11T12:00:00+03:00"
    assert parse_since("2026-10-01", now) == "2026-10-01"
    assert parse_since(None, now) is None
    with pytest.raises(ValueError):
        parse_since("last tuesday", now)


def test_open_means_deadline_not_passed_as_of_the_data(store):
    put(store,
        make_tender(id="stale", status="active.tendering", created="2026-09-01T10:00:00+03:00", tender_days=10),
        make_tender(id="live", status="active.tendering", tender_days=5))
    res = search_tenders(store)
    assert [r["tender_id"] for r in res["tenders"]] == [store.get_raw_tender("live")["tenderID"]]
    assert res["as_of"] == "2026-10-01T00:00:00+03:00"


def test_upcoming_plans_skip_long_past_planned_starts_by_default(store):
    put(store, make_plan(id="pold", tender_start="2025-07-01T00:00:00+03:00"),
        make_plan(id="precent", tender_start="2026-09-15T00:00:00+03:00"),
        make_plan(id="pnext", tender_start="2026-11-01T00:00:00+02:00"))
    got = {r["planned_tender_start"][:7] for r in upcoming_plans(store)["plans"]}
    assert got == {"2026-09", "2026-11"}
    assert upcoming_plans(store, start_from="2025-01-01")["total"] == 3


def test_coverage_hint_for_a_category_outside_the_synced_cpv_filter(store):
    from dataclasses import asdict

    from prozorro_mcp.sync import Coverage
    store.set_state("coverage", json.dumps(asdict(Coverage(["Харківська область"], 7, ["15"], "2026-10-06"))))
    assert coverage_hint(store, cpv=["1561"]) is None                  # inside food: covered
    hint = coverage_hint(store, cpv=["0913"])                           # fuel: not synced
    assert "0913" in hint and "--cpv 0913 --cpv 15" in hint
    assert "15" in coverage_hint(store, empty_without_cpv=True)          # words only, nothing found
    assert coverage_hint(store) is None
