"""MCP server: 11 read-only tools over the local Prozorro data.

    claude mcp add prozorro -- prozorro-mcp serve      # after: uv tool install git+https://github.com/kotiniiqq/prozorro-mcp
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

INSTRUCTIONS = (
    "Ukrainian public procurement (Prozorro) from a local copy of the official open data. Every number comes "
    "from these tools; do not estimate figures yourself. Call `coverage` first when unsure what regions and "
    "dates are available; if a result carries `coverage_hint`, show the user that command. Regions and CPV "
    "accept plain words in Ukrainian, Russian or English. Always cite tender links. Red flags are reasons to "
    "look closer, never accusations."
)

# The tools only read the local database: nothing is changed and nothing outside the machine is called.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)


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
               empty: bool = False) -> dict:
    hint = coverage_hint(store, region=region, since=since)
    if hint is None and empty:
        hint = coverage_hint(store)
    if hint:
        result["coverage_hint"] = hint
    return result


def build_server(store: Store) -> MCPServer:
    server = MCPServer(name="prozorro", instructions=INSTRUCTIONS)
    tool = lambda description: server.tool(annotations=READ_ONLY, description=description)

    @tool("What the local data covers: regions, period, CPV filter, counts, last sync, and the command to "
          "add more. Call this first when unsure.")
    @_errors
    def coverage() -> dict:
        return coverage_info(store)

    @tool("Find buyers and suppliers by part of a name (or an exact EDRPOU code). role: any, buyer, supplier.")
    @_errors
    def find_entity(name: str, role: Literal["any", "buyer", "supplier"] = "any", limit: int = 10) -> dict:
        return search.find_entity(store, name, role=role, limit=limit)

    @tool("Search tenders. Words match the title and item descriptions; cpv is a ДК 021 code, prefix or "
          "words; region in any spelling; status: open (default, accepting bids), any, or a Prozorro status. "
          "Dates as 2026-10-01, 7d or 24h. Returns tender ids, buyers, amounts, deadlines and links.")
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
        return _with_hint(store, res, r, since, empty=res["total"] == 0)

    @tool("One tender in full by its UA-... id: items with quantities and CPV, lots, every bid, the winner, "
          "and documents with direct links.")
    @_errors
    def get_tender(tender_id: str) -> dict:
        return search.get_tender(store, tender_id.strip())

    @tool("Market for a CPV and/or region over a period: number and value of tenders, awarded sum, share of "
          "competitive procedures, average bidders, unit-price spread (P25/median/P75, same CPV and unit), "
          "top buyers and winners.")
    @_errors
    def market_overview(cpv: str | None = None, region: str | None = None, since: str | None = None,
                        top: int = 5) -> dict:
        r = _region(region)
        prefixes, cpv_used = _cpv(cpv)
        s = parse_since(since)
        res = market.market_overview(store, cpv=prefixes, region=r, since=s, top=top)
        res["filters"] = {"region": r, "cpv": cpv_used, "since": s}
        return _with_hint(store, res, r, s, empty=res["count"] == 0)

    @tool("Supplier profile by EDRPOU: wins and their value, main buyers and CPVs, win rate where it bid, "
          "median discount from the expected value. Use find_entity to get the code from a name.")
    @_errors
    def supplier_profile(edrpou: str, since: str | None = None) -> dict:
        return market.supplier_profile(store, edrpou.strip(), since=parse_since(since))

    @tool("Buyer profile by EDRPOU: what it buys, from whom, procedure mix, share of competitive tenders "
          "with a single bidder.")
    @_errors
    def buyer_profile(edrpou: str, since: str | None = None) -> dict:
        return market.buyer_profile(store, edrpou.strip(), since=parse_since(since))

    @tool("Who competes with a company (EDRPOU): bidders met most often in the same tenders, and how many of "
          "those tenders each side won.")
    @_errors
    def competitors(edrpou: str, since: str | None = None, top: int = 10) -> dict:
        return market.competitors(store, edrpou.strip(), since=parse_since(since), top=top)

    @tool("Upcoming purchases from annual plans that have no tender yet: see demand before it is announced. "
          "Filter by words, CPV, region, budget, planned start date.")
    @_errors
    def upcoming_plans(query: str | None = None, cpv: str | None = None, region: str | None = None,
                       budget_min: float | None = None, budget_max: float | None = None,
                       start_from: str | None = None, limit: int = 20) -> dict:
        r = _region(region)
        prefixes, cpv_used = _cpv(cpv)
        res = search.upcoming_plans(store, query=query, cpv=prefixes, region=r, budget_min=budget_min,
                                    budget_max=budget_max, start_from=parse_since(start_from), limit=limit)
        res["filters"] = {"query": query, "region": r, "cpv": cpv_used}
        return _with_hint(store, res, r, empty=res["total"] == 0)

    @tool("Red flags for a buyer (EDRPOU), CPV or region, each with evidence and a link: single_bidder, "
          "supplier_concentration (one supplier > share of a buyer's spend), price_above_median (unit price "
          "pct above peers), short_tender_period. A flag is a reason to look closer, not proof.")
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
        return _with_hint(store, res, r, s)

    @tool("New tenders for the user's saved searches since a moment (default: last 24 hours). Saved searches "
          "are added with the CLI: prozorro-mcp watch add.")
    @_errors
    def watch_digest(since: str | None = None, limit: int = 20) -> dict:
        return watch.watch_digest(store, since=parse_since(since), limit=limit)

    return server
