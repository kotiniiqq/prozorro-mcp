import json
from dataclasses import asdict

import anyio
import pytest
from factories import make_plan, make_tender
from mcp.client import Client

from prozorro_mcp.server import build_server
from prozorro_mcp.sync import Coverage

TOOLS = ["buyer_profile", "competitors", "coverage", "find_entity", "get_tender", "market_overview",
         "red_flags", "search_tenders", "supplier_profile", "upcoming_plans", "watch_digest"]
A = ("22222222", "ТОВ Альфа")
B = ("33333333", "ТОВ Бета")


@pytest.fixture
def filled(store):
    store.set_state("coverage", json.dumps(asdict(Coverage(["Львівська область"], 7, [],
                                                           "2026-09-01T00:00:00+00:00")), ensure_ascii=False))
    store.upsert_tender(make_tender(id="t1", status="active.tendering", title="Борошно пшеничне",
                                    items=[("15612100-2", "Борошно", 100, "KGM")], winner=None))
    store.upsert_tender(make_tender(id="t2", bids=[(*A, 1000.0), (*B, 1100.0)],
                                    items=[("15612100-2", "Борошно", 100, "KGM")]))
    store.upsert_tender(make_tender(id="t3", bids=[(*A, 900.0)], items=[("15612100-2", "Борошно", 100, "KGM")]))
    store.upsert_plan(make_plan(id="p1", description="Борошно на 2027", cpv="15612100-2"))
    return store


def payload(result):
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def call(store, name, args=None):
    async def go():
        async with Client(build_server(store)) as client:
            return await client.call_tool(name, args or {})
    return anyio.run(go)


def list_tools(store):
    async def go():
        async with Client(build_server(store)) as client:
            return await client.list_tools()
    res = anyio.run(go)
    return getattr(res, "tools", res)


def test_exactly_eleven_tools(store):
    assert sorted(t.name for t in list_tools(store)) == TOOLS


def test_every_tool_declares_all_four_hints_read_only(store):
    for t in list_tools(store):
        a = t.annotations
        assert a is not None, t.name
        assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (
            True, False, True, False), t.name


def test_every_tool_has_a_description(store):
    assert all(t.description and len(t.description) > 30 for t in list_tools(store))


def test_search_resolves_region_and_cpv_words(filled):
    res = call(filled, "search_tenders", {"query": "борошно", "region": "львовская", "cpv": "борошно"})
    assert not res.is_error
    data = payload(res)
    assert data["total"] == 1 and data["filters"]["region"] == "Львівська область"
    assert data["filters"]["cpv"][0]["prefix"] == "1561"


def test_unknown_region_is_a_readable_error(filled):
    res = call(filled, "search_tenders", {"region": "Атлантида"})
    assert res.is_error and "Атлантида" in res.content[0].text


def test_unsynced_region_returns_the_sync_command(filled):
    data = payload(call(filled, "search_tenders", {"region": "Одеська"}))
    assert data["total"] == 0 and 'prozorro-mcp sync --region "Одеська область"' in data["coverage_hint"]


def test_get_tender_and_not_found(filled):
    tid = filled.get_raw_tender("t2")["tenderID"]
    data = payload(call(filled, "get_tender", {"tender_id": tid}))
    assert data["tender_id"] == tid and len(data["bids"]) == 2
    res = call(filled, "get_tender", {"tender_id": "UA-0000"})
    assert res.is_error and "not in the local data" in res.content[0].text


def test_market_profiles_competitors(filled):
    assert payload(call(filled, "market_overview", {"cpv": "15612"}))["count"] == 3
    assert payload(call(filled, "supplier_profile", {"edrpou": "22222222"}))["wins"] == 2
    assert payload(call(filled, "buyer_profile", {"edrpou": "11111111"}))["tenders"] == 3
    assert payload(call(filled, "competitors", {"edrpou": "22222222"}))["rows"][0]["edrpou"] == "33333333"


def test_unknown_edrpou_points_to_find_entity(filled):
    res = call(filled, "supplier_profile", {"edrpou": "00000000"})
    assert res.is_error and "find_entity" in res.content[0].text


def test_find_entity_plans_flags_digest_coverage(filled):
    assert payload(call(filled, "find_entity", {"name": "альфа"}))["entities"][0]["edrpou"] == "22222222"
    assert payload(call(filled, "upcoming_plans", {"query": "борошно"}))["total"] == 1
    flags = payload(call(filled, "red_flags", {"region": "Львівська", "kinds": ["single_bidder"]}))
    assert flags["counts"]["single_bidder"] == 1
    assert "watch add" in payload(call(filled, "watch_digest"))["hint"]
    assert payload(call(filled, "coverage"))["regions"] == ["Львівська область"]


def test_red_flags_without_scope_is_an_error(filled):
    res = call(filled, "red_flags", {})
    assert res.is_error and "scope" in res.content[0].text


def test_bad_since_is_an_error(filled):
    res = call(filled, "market_overview", {"since": "last tuesday"})
    assert res.is_error and "7d" in res.content[0].text
