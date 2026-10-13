"""MCP server: 11 read-only tools over the local Prozorro data, plus `sync_data` when a syncer is given.

    claude mcp add prozorro -- prozorro-mcp serve      # after: uv tool install prozorro-mcp
"""
from functools import wraps
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .analytics import flags as flags_mod
from .analytics import market, search, watch
from .analytics.common import NotFound, coverage_hint, coverage_info, parse_since
from .normalize import UnknownInput, prefix_name, resolve_cpv, resolve_region
from .store import Store
from .sync import get_coverage

INSTRUCTIONS = (
    "Ukrainian public procurement (Prozorro) from a local copy of the official open data. Every number comes "
    "from these tools; do not estimate figures yourself. Call `coverage` first when unsure what regions and "
    "dates are available; if a result carries `coverage_hint`, show the user that command. Regions and CPV "
    "accept plain words in Ukrainian, Russian or English. Always cite tender links. Red flags are reasons to "
    "look closer, never accusations."
)
SYNC_INSTRUCTIONS = (
    " Data can be downloaded from here with sync_data instead of the CLI command, but a download takes "
    "time (about 20 minutes and 350 MB per week of a busy oblast) and calls the public Prozorro API, so "
    "never start one on your own: when a question needs a region, category or period that is not synced, "
    "tell the user what is missing, what you would download and roughly how long it takes, and call "
    "sync_data only after they agree. Check progress with coverage. If a result has `no_data`, ask the user "
    "which oblast and how many days back to download before anything else."
)
ASK_FIRST = ("This data is not downloaded. Tell the user what is missing and ask the user whether to "
             "download it (say roughly how long it takes); call sync_data only if they agree.")
NO_DATA = ("No Prozorro data has been downloaded yet. Ask the user which oblast(s) to download and how many "
           "days back (7 is a good start; a busy oblast takes about 20 minutes per week of data); do not pick it "
           "yourself. Then call sync_data with what they chose.")

# The tools only read the local database: nothing is changed and nothing outside the machine is called.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)
# sync_data writes the local database and calls the public Prozorro API; running it twice adds nothing.
SYNC = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                       open_world_hint=True)


def _errors(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except UnknownInput as exc:
            raise ToolError(str(exc)) from exc
        except (NotFound, ValueError) as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def _region(text: str | None) -> str | None:
    return resolve_region(text) if text else None


def _cpv(text: str | None) -> tuple[list[str] | None, list[dict]]:
    if not text:
        return None, []
    prefixes = resolve_cpv(text)
    return prefixes, [{"prefix": p, "name": prefix_name(p)} for p in prefixes]


def _with_hint(store: Store, result: dict, region: str | None = None, since: str | None = None,
               empty: bool = False, cpv: list[str] | None = None) -> dict:
    hint = coverage_hint(store, region=region, since=since, cpv=cpv)
    if hint is None and empty:
        hint = coverage_hint(store, empty_without_cpv=True) or coverage_hint(store)
    if hint:
        result["coverage_hint"] = hint
    return result


def _sync_note(status: dict) -> str:
    kind = "plans" if status.get("kind") == "plans" else "tenders (plans come after them)"
    eta = f", about {status['eta_minutes']:.0f} min left" if status.get("eta_minutes") else ""
    return (f"A download is still running: {status.get('fraction', 0):.0%} of the period done for {kind}{eta}. "
            "These numbers are incomplete and will grow; tell the user so.")


def build_server(store: Store, syncer=None) -> MCPServer:
    server = MCPServer(name="prozorro", instructions=INSTRUCTIONS + (SYNC_INSTRUCTIONS if syncer else ""))

    def guarded(fn):
        """With a syncer: before the first sync say what to ask the user; during a sync show its progress."""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if syncer is None:
                return fn(*args, **kwargs)
            status = syncer.status()
            running = status.get("state") == "running"
            if get_coverage(store) is None:
                if running:
                    return {"no_data": True, "message": (
                        f"The first download is running ({status.get('fraction', 0):.0%} done). Tell the user "
                        "the data is not ready yet; check progress with coverage.")}
                return {"no_data": True, "message": NO_DATA}
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("coverage_hint"):
                result["next_step"] = ASK_FIRST
            if running and isinstance(result, dict):
                result["sync_in_progress"] = status
                result["sync_note"] = _sync_note(status)
            return result
        return wrapper

    @server.tool(
        annotations=READ_ONLY,
        description="What the local data covers: regions, period, CPV filter, counts, last sync, the "
                    "background sync progress if any, and how to add more. Call this first when unsure.")
    @_errors
    def coverage() -> dict:
        info = coverage_info(store)
        if syncer is not None:
            info["sync"] = syncer.status()
            if info["sync"].get("state") == "running":
                info["sync_note"] = _sync_note(info["sync"])
        return info

    if syncer is not None:
        @server.tool(
            annotations=SYNC,
            description="Download Prozorro tenders and plans for regions (any spelling) into the local "
                        "data, in the background. Call it only after the user agreed to the download. "
                        "days: how far back; the user chooses it for the first download, later downloads "
                        "keep the synced period unless the user asks for another. cpv: categories to download, "
                        "added to those already synced (nothing changes when all categories are synced). "
                        "Without arguments it fetches what changed since the last sync. Regions are added "
                        "to what is already synced; a new region, category or period walks the whole period "
                        "again. Returns at once; check coverage for progress.")
        @_errors
        def sync_data(regions: list[str] | None = None, days: int | None = None,
                      cpv: str | None = None) -> dict:
            resolved = [resolve_region(r) for r in regions] if regions else None
            prefixes, _ = _cpv(cpv)
            current = get_coverage(store)
            if current is None and days is None:
                raise ValueError("Ask the user how many days back to download (for example 7; a busy oblast "
                                 "takes about 20 minutes per week) and pass it as days. Do not pick it yourself.")
            if prefixes and current is not None:
                # add categories to what is synced; a sync of all categories is never narrowed
                prefixes = sorted(set(prefixes) | set(current.cpv)) if current.cpv else None
            if days is not None and not 1 <= days <= 92:
                raise ValueError("days must be between 1 and 92.")
            return syncer.start(resolved, days, prefixes)

    @server.tool(
        annotations=READ_ONLY,
        description="Find buyers and suppliers by part of a name (or an exact EDRPOU code). role: any, "
                    "buyer, supplier.")
    @guarded
    @_errors
    def find_entity(name: str, role: Literal["any", "buyer", "supplier"] = "any", limit: int = 10) -> dict:
        return search.find_entity(store, name, role=role, limit=limit)

    @server.tool(
        annotations=READ_ONLY,
        description="Search tenders. Words match the title and item descriptions; cpv is a ДК 021 code, "
                    "prefix or words; region in any spelling; status: open (default, accepting bids), any, "
                    "or a Prozorro status. Dates as 2026-10-01, 7d or 24h. Returns tender ids, buyers, "
                    "amounts, deadlines and links.")
    @guarded
    @_errors
    def search_tenders(query: str | None = None, cpv: str | None = None, region: str | None = None,
                       status: str = "open", amount_min: float | None = None, amount_max: float | None = None,
                       deadline_after: str | None = None, published_since: str | None = None,
                       limit: int = 20) -> dict:
        r = _region(region)
        prefixes, cpv_used = _cpv(cpv)
        since = parse_since(published_since)
        res = search.search_tenders(store, query=query, cpv=prefixes, region=r, status=status,
                                    amount_min=amount_min, amount_max=amount_max,
                                    deadline_after=parse_since(deadline_after), published_since=since,
                                    limit=limit)
        res["filters"] = {"query": query, "region": r, "cpv": cpv_used, "status": status}
        return _with_hint(store, res, r, since, empty=res["total"] == 0, cpv=prefixes)

    @server.tool(
        annotations=READ_ONLY,
        description="One tender in full by its UA-... id: items with quantities and CPV, lots, every bid, "
                    "the winner, and documents with direct links.")
    @guarded
    @_errors
    def get_tender(tender_id: str) -> dict:
        return search.get_tender(store, tender_id.strip())

    @server.tool(
        annotations=READ_ONLY,
        description="Market for a CPV and/or region over a period: number and value of tenders, awarded "
                    "sum, share of competitive procedures, average bidders, unit-price spread "
                    "(P25/median/P75, same CPV and unit), top buyers and winners.")
    @guarded
    @_errors
    def market_overview(cpv: str | None = None, region: str | None = None, since: str | None = None,
                        top: int = 5) -> dict:
        r = _region(region)
        prefixes, cpv_used = _cpv(cpv)
        s = parse_since(since)
        res = market.market_overview(store, cpv=prefixes, region=r, since=s, top=top)
        res["filters"] = {"region": r, "cpv": cpv_used, "since": s}
        return _with_hint(store, res, r, s, empty=res["count"] == 0, cpv=prefixes)

    @server.tool(
        annotations=READ_ONLY,
        description="Supplier profile by EDRPOU: wins and their value, main buyers and CPVs, win rate where "
                    "it bid, median discount from the expected value. Use find_entity to get the code from "
                    "a name.")
    @guarded
    @_errors
    def supplier_profile(edrpou: str, since: str | None = None) -> dict:
        return market.supplier_profile(store, edrpou.strip(), since=parse_since(since))

    @server.tool(
        annotations=READ_ONLY,
        description="Buyer profile by EDRPOU: what it buys, from whom, procedure mix, share of competitive "
                    "tenders with a single bidder.")
    @guarded
    @_errors
    def buyer_profile(edrpou: str, since: str | None = None) -> dict:
        return market.buyer_profile(store, edrpou.strip(), since=parse_since(since))

    @server.tool(
        annotations=READ_ONLY,
        description="Who competes with a company (EDRPOU): bidders met most often in the same tenders, and "
                    "how many of those tenders each side won.")
    @guarded
    @_errors
    def competitors(edrpou: str, since: str | None = None, top: int = 10) -> dict:
        return market.competitors(store, edrpou.strip(), since=parse_since(since), top=top)

    @server.tool(
        annotations=READ_ONLY,
        description="Upcoming purchases from annual plans that have no tender yet: see demand before it is "
                    "announced. Filter by words, CPV, region, budget, planned start date.")
    @guarded
    @_errors
    def upcoming_plans(query: str | None = None, cpv: str | None = None, region: str | None = None,
                       budget_min: float | None = None, budget_max: float | None = None,
                       start_from: str | None = None, limit: int = 20) -> dict:
        r = _region(region)
        prefixes, cpv_used = _cpv(cpv)
        res = search.upcoming_plans(store, query=query, cpv=prefixes, region=r, budget_min=budget_min,
                                    budget_max=budget_max, start_from=parse_since(start_from), limit=limit)
        res["filters"] = {"query": query, "region": r, "cpv": cpv_used}
        return _with_hint(store, res, r, empty=res["total"] == 0, cpv=prefixes)

    @server.tool(
        annotations=READ_ONLY,
        description="Red flags for a buyer (EDRPOU), CPV or region, each with evidence and a link: "
                    "single_bidder, supplier_concentration (one supplier > share of a buyer's spend), "
                    "price_above_median (unit price pct above peers), short_tender_period. A flag is a "
                    "reason to look closer, not proof.")
    @guarded
    @_errors
    def red_flags(buyer: str | None = None, cpv: str | None = None, region: str | None = None,
                  since: str | None = None, kinds: list[str] | None = None, share: float = 0.5,
                  min_awards: int = 5, pct: float = 0.30, min_peers: int = 5, limit: int = 20) -> dict:
        r = _region(region)
        prefixes, cpv_used = _cpv(cpv)
        s = parse_since(since)
        res = flags_mod.red_flags(store, buyer=buyer, cpv=prefixes, region=r, since=s, kinds=kinds,
                                  share=share, min_awards=min_awards, pct=pct, min_peers=min_peers, limit=limit)
        res["filters"] = {"buyer": buyer, "region": r, "cpv": cpv_used, "since": s}
        return _with_hint(store, res, r, s, cpv=prefixes)

    @server.tool(
        annotations=READ_ONLY,
        description="New tenders for the user's saved searches since a moment (default: last 24 hours). "
                    "Saved searches are added with the CLI: prozorro-mcp watch add.")
    @guarded
    @_errors
    def watch_digest(since: str | None = None, limit: int = 20) -> dict:
        return watch.watch_digest(store, since=parse_since(since), limit=limit)

    return server
