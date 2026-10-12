"""Record real Prozorro responses as test fixtures (run once; outputs are committed).

    python scripts/capture_fixtures.py

Contact persons are stripped from everything saved.
"""
import asyncio
import json
import time
from pathlib import Path

from prozorro_mcp.api import ProzorroAPI
from prozorro_mcp.models import COMPETITIVE
from prozorro_mcp.privacy import strip_contacts

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

WANTED = {
    "tender_competitive.json": lambda t: t.get("procurementMethodType") in COMPETITIVE
    and t.get("status") == "complete" and len(t.get("bids") or []) >= 3 and not t.get("lots")
    and len(t.get("items") or []) == 1,
    "tender_single_bid.json": lambda t: t.get("procurementMethodType") in COMPETITIVE
    and t.get("status") in ("complete", "active.awarded") and len(t.get("bids") or []) == 1
    and not t.get("lots"),
    "tender_reporting.json": lambda t: t.get("procurementMethodType") == "reporting"
    and t.get("status") == "complete",
    "tender_multi_lot.json": lambda t: len(t.get("lots") or []) >= 2 and t.get("bids"),
}


def save(name: str, data) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(strip_contacts(data), ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", name)


async def main() -> None:
    async with ProzorroAPI() as api:
        offset = time.time() - 3 * 86400
        page = await api.feed("tenders", offset, opt_fields="procuringEntity,tenderPeriod")
        save("feed_page.json", {"data": page.items[:40], "next_page": {"offset": page.next_offset}})
        found: dict[str, dict] = {}
        while len(found) < len(WANTED):
            for entry in page.items:
                t = await api.get("tenders", entry["id"])
                if not t:
                    continue
                for name, ok in WANTED.items():
                    if name not in found and ok(t):
                        found[name] = t
                        save(name, t)
                if len(found) == len(WANTED):
                    break
            page = await api.feed("tenders", page.next_offset, opt_fields="procuringEntity,tenderPeriod")
        plans = await api.feed("plans", offset, opt_fields="procuringEntity")
        save("plans_feed_page.json", {"data": plans.items[:20], "next_page": {"offset": plans.next_offset}})
        save("plan.json", await api.get("plans", plans.items[0]["id"]))


if __name__ == "__main__":
    asyncio.run(main())
