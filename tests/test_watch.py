from datetime import datetime

import pytest
from factories import make_tender

from prozorro_mcp.analytics.common import NotFound
from prozorro_mcp.analytics.watch import watch_add, watch_digest, watch_list, watch_remove

NOW = datetime.fromisoformat("2026-10-12T12:00:00+03:00")


def test_add_list_remove(store):
    watch_add(store, "борошно", query="борошно", region="Львівська область")
    watch_add(store, "паливо", cpv=["0913"])
    assert [w["name"] for w in watch_list(store)] == ["борошно", "паливо"]
    assert watch_list(store)[1]["cpv"] == ["0913"]
    watch_remove(store, "борошно")
    assert [w["name"] for w in watch_list(store)] == ["паливо"]


def test_duplicate_name_and_empty_criteria_rejected(store):
    watch_add(store, "x", query="борошно")
    with pytest.raises(ValueError):
        watch_add(store, "x", query="інше")
    with pytest.raises(ValueError):
        watch_add(store, "y")


def test_remove_unknown(store):
    with pytest.raises(NotFound):
        watch_remove(store, "nope")


def test_digest_returns_new_matches_per_watch_since_window(store):
    store.upsert_tender(make_tender(title="Борошно пшеничне", created="2026-10-12T09:00:00+03:00"))
    store.upsert_tender(make_tender(title="Борошно житнє", created="2026-10-10T09:00:00+03:00"))
    store.upsert_tender(make_tender(title="Бензин", items=[("09132000-3", "Бензин", 1, "LTR")],
                                    created="2026-10-12T08:00:00+03:00"))
    watch_add(store, "борошно", query="борошно")
    watch_add(store, "паливо", cpv=["0913"])
    res = watch_digest(store, now=NOW)  # default: last 24 hours
    by_name = {w["name"]: w for w in res["watches"]}
    assert by_name["борошно"]["total"] == 1 and by_name["паливо"]["total"] == 1
    res = watch_digest(store, since="2026-10-09T00:00:00+03:00", now=NOW)
    assert {w["name"]: w["total"] for w in res["watches"]}["борошно"] == 2
    assert res["since"] == "2026-10-09T00:00:00+03:00"


def test_digest_without_watches_says_how_to_add(store):
    res = watch_digest(store, now=NOW)
    assert res["watches"] == [] and "watch add" in res["hint"]
