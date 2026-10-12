"""Shared helpers: money and paging formats, coverage of the local data and hints when it falls short."""
import re
from datetime import datetime, timedelta

from ..store import Store
from ..sync import get_coverage

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class NotFound(LookupError):
    pass


def money_text(x: float | None) -> str | None:
    if x is None:
        return None
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " грн"


def clamp_limit(limit: int | None) -> int:
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))


def paging(total: int, shown: int) -> dict:
    out = {"total": total, "shown": shown}
    if total > shown:
        out["note"] = f"shown {shown} of {total}; narrow the filters or raise limit (max {MAX_LIMIT})"
    return out


def date_only(value: str | None) -> str | None:
    return value[:10] if value else None


def coverage_info(store: Store) -> dict:
    cov = get_coverage(store)
    count = lambda table: store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return {
        "regions": cov.regions if cov else [],
        "days": cov.days if cov else None,
        "cpv_filter": cov.cpv if cov else [],
        "since": cov.since if cov else None,
        "last_sync": store.get_state("last_sync"),
        "tenders": count("tenders"),
        "plans": count("plans"),
        "hint": coverage_hint(store),
    }


def coverage_hint(store: Store, region: str | None = None, since: str | None = None) -> str | None:
    """The exact command to run when a question needs data the local store does not have."""
    cov = get_coverage(store)
    if cov is None:
        return ("No data synced yet. Run: prozorro-mcp sync --region <oblast> --days 7 "
                "(or use the bundled demo: prozorro-mcp serve --demo).")
    if region and region not in cov.regions:
        return (f'{region} is not synced (synced: {", ".join(cov.regions)}). '
                f'Run: prozorro-mcp sync --region "{region}"')
    if since and cov.since:
        row = store.conn.execute("SELECT julianday(?) < julianday(?)", (since, cov.since)).fetchone()
        if row[0]:
            return (f"Local data starts at {cov.since[:10]}; for earlier dates run: "
                    f"prozorro-mcp sync --days <more than {cov.days}>")
    return None


def parse_since(value: str | None, now: datetime | None = None) -> str | None:
    """'7d', '24h', '2026-10-01' or a full ISO timestamp -> a value SQLite's julianday() understands."""
    if not value:
        return None
    text = value.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([dh])", text)
    if m:
        now = now or datetime.now().astimezone()
        delta = timedelta(days=int(m[1])) if m[2] == "d" else timedelta(hours=int(m[1]))
        return (now - delta).isoformat()
    try:
        datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"Cannot read date '{value}'. Use 7d, 24h, 2026-10-01 or an ISO timestamp.") from None
    return value.strip()


def as_of(store: Store) -> str:
    """The moment the local data describes: the last sync (a fresh sync is about now)."""
    return store.get_state("last_sync") or datetime.now().astimezone().isoformat()
