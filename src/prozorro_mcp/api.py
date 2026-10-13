"""Async client for the public Prozorro API: change feeds and full documents, polite and retrying."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import httpx

from . import __version__

BASE_URL = "https://public-api.prozorro.gov.ua/api/2.5"
USER_AGENT = f"prozorro-mcp/{__version__} (+https://github.com/kotiniiqq/prozorro-mcp)"
ATTEMPTS = 5
BACKOFF = (1, 2, 4, 8, 16)
RETRY_STATUS = {429, 500, 502, 503, 504}

Kind = Literal["tenders", "plans"]

# the MCP server logs at INFO to stderr; httpx would add a line for every one of thousands of requests
logging.getLogger("httpx").setLevel(logging.WARNING)


class APIError(RuntimeError):
    pass


@dataclass
class FeedPage:
    items: list[dict]
    next_offset: str
    prev_offset: str | None = None  # in a descending read: the ascending offset at the newest item


class ProzorroAPI:
    def __init__(self, base_url: str = BASE_URL, client: httpx.AsyncClient | None = None,
                 concurrency: int = 5, sleep=asyncio.sleep, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._sem = asyncio.Semaphore(concurrency)
        self._sleep = sleep

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()

    async def _request(self, path: str, params: dict | None = None) -> httpx.Response | None:
        url = f"{self.base_url}{path}"
        last = ""
        for attempt in range(ATTEMPTS):
            try:
                async with self._sem:
                    resp = await self._client.get(url, params=params,
                                                  headers={"User-Agent": USER_AGENT})
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code in (404, 410):
                    return None
                if resp.status_code < 400:
                    return resp
                last = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRY_STATUS:
                    raise APIError(f"Prozorro API rejected {path}: {last} {resp.text[:200]}")
            if attempt < ATTEMPTS - 1:
                await self._sleep(BACKOFF[attempt])
        raise APIError(f"Prozorro API failed after {ATTEMPTS} attempts on {path}: {last}")

    async def feed(self, kind: Kind, offset: str | float | None, opt_fields: str | None = None,
                   descending: bool = False) -> FeedPage:
        """One page of the change feed. descending=True with no offset starts at the newest change."""
        params = {"limit": "1000"}
        if offset is not None:
            params["offset"] = str(offset)
        if descending:
            params["descending"] = "1"
        if opt_fields:
            params["opt_fields"] = opt_fields
        resp = await self._request(f"/{kind}", params)
        if resp is None:
            raise APIError(f"Prozorro feed /{kind} not found")
        body = resp.json()
        next_offset = str((body.get("next_page") or {}).get("offset") or offset)
        prev_offset = (body.get("prev_page") or {}).get("offset")
        return FeedPage(items=body.get("data") or [], next_offset=next_offset,
                        prev_offset=str(prev_offset) if prev_offset else None)

    async def get(self, kind: Kind, id: str) -> dict | None:
        resp = await self._request(f"/{kind}/{id}")
        return None if resp is None else resp.json().get("data")
