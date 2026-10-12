import asyncio

import httpx
import pytest

from prozorro_mcp.api import BASE_URL, APIError, ProzorroAPI


def run(coro):
    return asyncio.run(coro)


def make_api(handler, **kw):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = ProzorroAPI(client=client, sleep=fake_sleep, **kw)
    return api, sleeps


def test_feed_sends_paging_params_and_reads_next_offset():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json={"data": [{"id": "a", "dateModified": "x"}],
                                         "next_page": {"offset": "1791.5.1.abc"}})

    api, _ = make_api(handler)
    page = run(api.feed("tenders", 1790000000, opt_fields="procuringEntity"))
    assert page.items == [{"id": "a", "dateModified": "x"}] and page.next_offset == "1791.5.1.abc"
    assert seen["url"].path == "/api/2.5/tenders"
    assert seen["url"].params["limit"] == "1000"
    assert seen["url"].params["offset"] == "1790000000"
    assert seen["url"].params["opt_fields"] == "procuringEntity"
    assert seen["ua"].startswith("prozorro-mcp/")
    assert str(seen["url"]).startswith(BASE_URL)


def test_empty_page_keeps_offset():
    api, _ = make_api(lambda r: httpx.Response(200, json={"data": [], "next_page": {"offset": "5.0"}}))
    page = run(api.feed("plans", "5.0"))
    assert page.items == [] and page.next_offset == "5.0"


def test_get_returns_data_or_none():
    def handler(request):
        if request.url.path.endswith("/gone"):
            return httpx.Response(404, json={"status": "error"})
        return httpx.Response(200, json={"data": {"id": "t1"}})

    api, _ = make_api(handler)
    assert run(api.get("tenders", "t1")) == {"id": "t1"}
    assert run(api.get("tenders", "gone")) is None


def test_429_is_retried_with_backoff():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"data": {"id": "t1"}})

    api, sleeps = make_api(handler)
    assert run(api.get("tenders", "t1")) == {"id": "t1"}
    assert sleeps == [1, 2]


def test_network_error_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"data": {"id": "t1"}})

    api, _ = make_api(handler)
    assert run(api.get("tenders", "t1")) == {"id": "t1"}


def test_gives_up_after_five_attempts_with_a_clear_error():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503)

    api, sleeps = make_api(handler)
    with pytest.raises(APIError) as exc:
        run(api.get("tenders", "t1"))
    assert len(calls) == 5 and sleeps == [1, 2, 4, 8]
    assert "/tenders/t1" in str(exc.value) and "503" in str(exc.value)


def test_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text="bad offset")

    api, _ = make_api(handler)
    with pytest.raises(APIError):
        run(api.feed("tenders", "zzz"))
    assert len(calls) == 1


def test_concurrency_is_limited():
    state = {"now": 0, "max": 0}

    async def handler(request):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(0.01)
        state["now"] -= 1
        return httpx.Response(200, json={"data": {"id": "x"}})

    api, _ = make_api(handler, concurrency=3)

    async def go():
        await asyncio.gather(*(api.get("tenders", str(i)) for i in range(12)))

    run(go())
    assert state["max"] == 3


def test_httpx_does_not_log_every_request():
    """The MCP server logs at INFO to stderr; one line per request would flood the client's log."""
    import logging

    import prozorro_mcp.api  # noqa: F401
    assert logging.getLogger("httpx").level >= logging.WARNING  # set explicitly, whatever the root level is
