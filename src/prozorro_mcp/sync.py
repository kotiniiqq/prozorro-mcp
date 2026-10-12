"""Sync Prozorro change feeds into the local store for the chosen regions, period and CPV.

First run walks the feed from (now - days); later runs resume from the saved cursor. Each feed page
is stored in one transaction together with the cursor, so an interrupted run loses at most the page
in flight and the next run continues from there.
"""
import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .normalize import NO_REGION
from .store import Store

DEFAULT_DAYS = 7


class SyncError(ValueError):
    pass


@dataclass
class Coverage:
    regions: list[str]
    days: int
    cpv: list[str] = field(default_factory=list)
    since: str = ""


@dataclass
class SyncProgress:
    kind: str
    done: int
    fraction: float
    eta_seconds: float | None


@dataclass
class SyncReport:
    stored: int = 0
    plans_stored: int = 0
    skipped_region: int = 0
    skipped_cpv: int = 0
    hidden_region: int = 0
    missing: int = 0


def get_coverage(store: Store) -> Coverage | None:
    raw = store.get_state("coverage")
    return Coverage(**json.loads(raw)) if raw else None


def _ts(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def _region_of(entry: dict) -> str:
    return ((entry.get("procuringEntity") or {}).get("address") or {}).get("region") or ""


def _cpv_codes(doc: dict, kind: str) -> list[str]:
    if kind == "plans":
        return [(doc.get("classification") or {}).get("id") or ""]
    return [((it.get("classification") or {}).get("id") or "") for it in doc.get("items") or []]


async def run_sync(api, store: Store, regions: list[str] | None = None, days: int | None = None,
                   cpv: list[str] | None = None, progress: Callable[[SyncProgress], None] | None = None,
                   plans: bool = True, now: Callable[[], float] = time.time) -> SyncReport:
    current = get_coverage(store)
    if current is None and not regions:
        raise SyncError("Nothing synced yet: choose at least one region, e.g. "
                        "prozorro-mcp sync --region Львівська --days 7")
    if current is None:
        cov = Coverage(sorted(set(regions)), days or DEFAULT_DAYS, sorted(set(cpv or [])))
        restart = True
    else:
        new_regions = sorted(set(current.regions) | set(regions or []))
        new_cpv = sorted(set(cpv)) if cpv is not None else current.cpv
        new_days = days or current.days
        restart = (new_regions != current.regions or new_cpv != current.cpv or new_days != current.days)
        cov = Coverage(new_regions, new_days, new_cpv, current.since)
    start = now() - cov.days * 86400
    if restart:
        cov.since = datetime.fromtimestamp(start, UTC).isoformat()
        with store.transaction():
            store.set_state("coverage", json.dumps(asdict(cov), ensure_ascii=False))
            for kind in ("tenders", "plans"):
                store.conn.execute("DELETE FROM sync_state WHERE key=?", (f"cursor:{kind}",))

    report = SyncReport()
    wanted = set(cov.regions)
    for kind in ("tenders", "plans") if plans else ("tenders",):
        await _sync_kind(api, store, kind, wanted, cov.cpv, start, report, progress, now)
    store.set_state("last_sync", datetime.fromtimestamp(now(), UTC).isoformat())
    return report


async def _sync_kind(api, store, kind, wanted, cpv, start, report, progress, now):
    offset = store.get_state(f"cursor:{kind}") or start
    began = now()
    done = 0
    while True:
        page = await api.feed(kind, offset, opt_fields="procuringEntity")
        if not page.items:
            break
        matching = []
        for entry in page.items:
            region = _region_of(entry)
            if not region:
                if NO_REGION not in wanted:
                    report.hidden_region += 1
                    continue
            elif region not in wanted:
                report.skipped_region += 1
                continue
            matching.append(entry["id"])
        docs = await asyncio.gather(*(api.get(kind, i) for i in matching))
        with store.transaction():
            for doc_id, doc in zip(matching, docs):
                if doc is None:
                    report.missing += 1
                    if kind == "tenders":
                        store.mark_missing(doc_id)
                    continue
                if cpv and not any(code.startswith(p) for code in _cpv_codes(doc, kind) for p in cpv):
                    report.skipped_cpv += 1
                    continue
                if kind == "tenders":
                    store.upsert_tender(doc)
                    report.stored += 1
                else:
                    store.upsert_plan(doc)
                    report.plans_stored += 1
            store.set_state(f"cursor:{kind}", page.next_offset)
        done += len(matching)
        if progress:
            last = _ts(page.items[-1].get("dateModified"))
            span = now() - start
            fraction = min(max((last - start) / span, 0.0), 1.0) if last and span > 0 else 0.0
            elapsed = now() - began
            eta = elapsed * (1 - fraction) / fraction if fraction > 0 else None
            progress(SyncProgress(kind, done, fraction, eta))
        if page.next_offset == str(offset):
            break
        offset = page.next_offset
