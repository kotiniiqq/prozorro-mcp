import asyncio
from datetime import datetime

import pytest
from factories import make_plan, make_tender

from prozorro_mcp.api import APIError, FeedPage
from prozorro_mcp.normalize import NO_REGION
from prozorro_mcp.sync import SyncError, get_coverage, run_sync

NOW = datetime.fromisoformat("2026-10-12T12:00:00+03:00").timestamp()


RECENT = "2026-10-12T11:00:00+03:00"  # inside any synced period ending at NOW


def entry(doc, region="__from_doc__", modified=RECENT):
    if region == "__from_doc__":
        region = (doc.get("procuringEntity", {}).get("address") or {}).get("region", "")
    return {"id": doc["id"], "dateModified": modified, "procuringEntity": {"address": {"region": region}}}


class FakeAPI:
    """pages[kind] is a list of pages (lists of feed entries), oldest first; ascending offsets are page indexes
    as strings. Descending reads go from the last page back ("d<index>"); the first one also returns the
    ascending offset right after the newest page, like Prozorro's prev_page."""

    def __init__(self, tenders_pages=(), plans_pages=(), docs=(), fail_on_get=None):
        self.pages = {"tenders": list(tenders_pages), "plans": list(plans_pages)}
        self.docs = {d["id"]: d for d in docs}
        self.fail_on_get = fail_on_get
        self.feed_calls = []
        self.get_calls = []

    async def feed(self, kind, offset, opt_fields=None, descending=False):
        pages = self.pages[kind]
        if descending:
            self.feed_calls.append((kind, f"desc:{offset or 'newest'}"))
            i = len(pages) - 1 if not offset else int(str(offset)[1:])
            items = list(reversed(pages[i])) if i >= 0 else []
            return FeedPage(items=items, next_offset=f"d{i - 1}" if items else f"d{i}",
                            prev_offset=str(len(pages)) if not offset else None)
        self.feed_calls.append((kind, str(offset)))
        i = int(offset) if str(offset).isdigit() and int(offset) < 1000 else 0
        items = pages[i] if i < len(pages) else []
        return FeedPage(items=items, next_offset=str(i + 1) if items else str(i))

    async def get(self, kind, id):
        self.get_calls.append(id)
        if id == self.fail_on_get:
            raise APIError("down")
        return self.docs.get(id)


def sync(api, store, **kw):
    kw.setdefault("now", lambda: NOW)
    return asyncio.run(run_sync(api, store, **kw))


def ids(store, table="tenders"):
    return sorted(r[0] for r in store.conn.execute(f"SELECT id FROM {table}"))


def test_first_sync_needs_a_region(store):
    with pytest.raises(SyncError):
        sync(FakeAPI(), store)


def test_first_sync_stores_only_matching_region_and_skips_others_without_fetching(store):
    lviv = make_tender(id="lviv")
    kyiv = make_tender(id="kyiv", region="м. Київ")
    api = FakeAPI(tenders_pages=[[entry(lviv), entry(kyiv)]], docs=[lviv, kyiv])
    report = sync(api, store, regions=["Львівська область"], days=7, plans=False)
    assert ids(store) == ["lviv"]
    assert api.get_calls == ["lviv"]
    assert report.stored == 1 and report.skipped_region == 1


def test_first_sync_goes_from_the_newest_back_and_stops_at_the_start_of_the_period(store):
    old, mid, new = make_tender(id="old"), make_tender(id="mid"), make_tender(id="new")
    api = FakeAPI(tenders_pages=[[entry(old, modified="2026-09-01T10:00:00+03:00")],
                                 [entry(mid, modified="2026-10-08T10:00:00+03:00")], [entry(new)]],
                  docs=[old, mid, new])
    sync(api, store, regions=["Львівська область"], days=7, plans=False)
    assert api.feed_calls[0] == ("tenders", "desc:newest")
    assert api.get_calls == ["new", "mid"]  # newest first; the one before the period is never fetched
    assert ids(store) == ["mid", "new"]
    assert store.get_state("cursor:tenders") == "3" and store.get_state("desc:tenders") is None
    cov = get_coverage(store)
    assert cov.regions == ["Львівська область"] and cov.days == 7


def test_hidden_region_is_skipped_and_counted_unless_asked_for(store):
    unit = make_tender(id="unit", region="")
    api = FakeAPI(tenders_pages=[[entry(unit, region="")]], docs=[unit])
    report = sync(api, store, regions=["Львівська область"], days=7, plans=False)
    assert ids(store) == [] and report.hidden_region == 1 and api.get_calls == []

    store2_api = FakeAPI(tenders_pages=[[entry(unit, region="")]], docs=[unit])
    sync(store2_api, store, regions=[NO_REGION], days=7, plans=False)
    assert ids(store) == ["unit"]


def test_cpv_filter_applies_after_detail(store):
    flour = make_tender(id="flour")
    fuel = make_tender(id="fuel", items=[("09132000-3", "Бензин", 1000, "LTR")])
    api = FakeAPI(tenders_pages=[[entry(flour), entry(fuel)]], docs=[flour, fuel])
    report = sync(api, store, regions=["Львівська область"], days=7, cpv=["0913"], plans=False)
    assert ids(store) == ["fuel"] and report.skipped_cpv == 1


def test_second_run_resumes_from_cursor(store):
    a, b = make_tender(id="a"), make_tender(id="b")
    api = FakeAPI(tenders_pages=[[entry(a)]], docs=[a, b])
    sync(api, store, regions=["Львівська область"], days=7, plans=False)
    api.pages["tenders"].append([entry(b)])
    api.feed_calls.clear()
    api.get_calls.clear()
    sync(api, store, plans=False)
    assert ids(store) == ["a", "b"]
    assert api.feed_calls[0] == ("tenders", "1") and api.get_calls == ["b"]


def test_failure_mid_sync_keeps_finished_pages_and_resumes_without_duplicates(store):
    a, b, c = make_tender(id="a"), make_tender(id="b"), make_tender(id="c")
    api = FakeAPI(tenders_pages=[[entry(a), entry(b)], [entry(c)]], docs=[a, b, c], fail_on_get="a")
    with pytest.raises(APIError):
        sync(api, store, regions=["Львівська область"], days=7, plans=False)
    assert ids(store) == ["c"]  # the newest page is kept, the failed one was not half-stored
    assert store.get_state("desc:tenders") == "d0"
    api.fail_on_get = None
    api.feed_calls.clear()
    sync(api, store, plans=False)
    assert api.feed_calls[0] == ("tenders", "desc:d0")  # continues back from where it stopped
    assert ids(store) == ["a", "b", "c"]
    assert store.conn.execute("SELECT count(*) FROM items").fetchone()[0] == 3


def test_tender_gone_from_prozorro_is_marked_missing(store):
    a = make_tender(id="a")
    api = FakeAPI(tenders_pages=[[entry(a)]], docs=[a])
    sync(api, store, regions=["Львівська область"], days=7, plans=False)
    api.docs.pop("a")
    api.pages["tenders"].append([entry(a)])
    report = sync(api, store, plans=False)
    assert report.missing == 1
    assert store.conn.execute("SELECT missing FROM tenders WHERE id='a'").fetchone()[0] == 1


def test_adding_a_region_restarts_for_the_union(store):
    lviv = make_tender(id="lviv")
    kyiv = make_tender(id="kyiv", region="м. Київ")
    api = FakeAPI(tenders_pages=[[entry(lviv), entry(kyiv)]], docs=[lviv, kyiv])
    sync(api, store, regions=["Львівська область"], days=7, plans=False)
    api.feed_calls.clear()
    sync(api, store, regions=["м. Київ"], plans=False)
    assert api.feed_calls[0] == ("tenders", "desc:newest")
    assert get_coverage(store).regions == ["Львівська область", "м. Київ"]
    assert ids(store) == ["kyiv", "lviv"]


def test_plans_are_synced_with_the_same_filters(store):
    p1 = make_plan(id="p1")
    p2 = make_plan(id="p2", region="Одеська область")
    api = FakeAPI(plans_pages=[[entry(p1), entry(p2)]], docs=[p1, p2])
    report = sync(api, store, regions=["Львівська область"], days=7)
    assert ids(store, "plans") == ["p1"] and report.plans_stored == 1


def test_progress_is_reported(store):
    a, b = make_tender(id="a"), make_tender(id="b")
    seen = []
    api = FakeAPI(tenders_pages=[[entry(a, modified="2026-10-08T12:00:00+03:00")], [entry(b)]], docs=[a, b])
    sync(api, store, regions=["Львівська область"], days=7, plans=False, progress=seen.append)
    assert {p.kind for p in seen} == {"tenders"} and len(seen) >= 2 and seen[-1].done == 2
    assert 0 < seen[0].fraction < 1 and seen[-1].fraction == 1.0
