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


def call_with(store, syncer, name, args=None):
    async def go():
        async with Client(build_server(store, syncer)) as client:
            return await client.call_tool(name, args or {})
    return anyio.run(go)


def call(store, name, args=None):
    return call_with(store, None, name, args)


def list_tools_with(store, syncer):
    async def go():
        async with Client(build_server(store, syncer)) as client:
            return await client.list_tools()
    res = anyio.run(go)
    return getattr(res, "tools", res)


def list_tools(store):
    return list_tools_with(store, None)


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


class FakeSyncer:
    def __init__(self):
        self.calls = []

    def start(self, regions=None, days=None, cpv=None):
        self.calls.append((regions, days, cpv))
        return {"started": True, "state": "running", "regions": regions}

    def status(self):
        return {"state": "running", "fraction": 0.4}


def test_with_a_syncer_there_is_one_more_tool_and_only_it_writes(store):
    tools = {t.name: t.annotations for t in list_tools_with(store, FakeSyncer())}
    assert sorted(tools) == sorted(TOOLS + ["sync_data"])
    a = tools["sync_data"]
    assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (
        False, False, True, True)
    assert all(tools[n].read_only_hint for n in TOOLS)


def test_sync_data_resolves_words_and_starts_the_syncer(filled):
    fake = FakeSyncer()
    data = payload(call_with(filled, fake, "sync_data", {"regions": ["волынь"], "days": 14, "cpv": "борошно"}))
    from prozorro_mcp.normalize import resolve_cpv
    assert data["started"] and fake.calls == [(["Волинська область"], 14, resolve_cpv("борошно"))]


def test_sync_data_unknown_region_is_a_readable_error(filled):
    res = call_with(filled, FakeSyncer(), "sync_data", {"regions": ["Атлантида"]})
    assert res.is_error and "Атлантида" in res.content[0].text


def test_sync_data_rejects_absurd_days(filled):
    res = call_with(filled, FakeSyncer(), "sync_data", {"regions": ["Львів"], "days": 400})
    assert res.is_error and "92" in res.content[0].text


def test_coverage_shows_the_sync_status(filled):
    assert payload(call_with(filled, FakeSyncer(), "coverage"))["sync"]["state"] == "running"


class RunningSyncer(FakeSyncer):
    def status(self):
        return {"state": "running", "fraction": 0.25, "eta_minutes": 12.0}


class IdleSyncer(FakeSyncer):
    def status(self):
        return {"state": "idle"}


def test_before_any_sync_data_tools_ask_for_a_region_and_days_instead_of_answering(store):
    res = call_with(store, IdleSyncer(), "search_tenders", {"query": "борошно"})
    assert not res.is_error
    data = payload(res)
    assert data["no_data"] is True and "sync_data" in data["message"] and "days" in data["message"]
    assert "tenders" not in data
    assert payload(call_with(store, IdleSyncer(), "find_entity", {"name": "альфа"}))["no_data"] is True


def test_coverage_still_answers_before_any_sync(store):
    data = payload(call_with(store, IdleSyncer(), "coverage"))
    assert data["regions"] == [] and data["sync"]["state"] == "idle" and "no_data" not in data


def test_while_the_first_sync_runs_the_message_says_it_is_downloading(store):
    data = payload(call_with(store, RunningSyncer(), "search_tenders", {}))
    assert data["no_data"] is True and "25%" in data["message"] and "coverage" in data["message"]


def test_during_a_sync_results_carry_its_progress(filled):
    data = payload(call_with(filled, RunningSyncer(), "search_tenders", {"status": "any"}))
    assert data["total"] == 3 and data["sync_in_progress"]["fraction"] == 0.25


def test_without_a_syncer_an_empty_store_keeps_the_cli_hint(store):
    data = payload(call(store, "search_tenders", {}))
    assert "no_data" not in data and "prozorro-mcp sync" in data["coverage_hint"]
