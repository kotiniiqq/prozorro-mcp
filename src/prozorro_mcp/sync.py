"""Sync Prozorro change feeds into the local store for the chosen regions, period and CPV.

The first run walks the feed backwards, from the newest change to (now - days), so the tenders that are
open right now arrive in the first minutes; it remembers where the feed stood when it began. Later runs
go forward from that point and pick up only what changed. Each feed page is stored in one transaction
together with its cursor, so an interrupted run loses at most the page in flight, and the next run
continues from there, backwards or forwards.
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
                store.set_state(f"desc:{kind}", "")

    report = SyncReport()
    wanted = set(cov.regions)
    for kind in ("tenders", "plans") if plans else ("tenders",):
        if store.get_state(f"desc:{kind}") is not None or store.get_state(f"cursor:{kind}") is None:
            await _sync_back(api, store, kind, wanted, cov.cpv, start, report, progress, now)
        else:
            await _sync_kind(api, store, kind, wanted, cov.cpv, start, report, progress, now)
    store.set_state("last_sync", datetime.fromtimestamp(now(), UTC).isoformat())
    return report


def _wanted_ids(entries, wanted, report) -> list[str]:
    ids = []
    for entry in entries:
        region = _region_of(entry)
        if not region:
            if NO_REGION not in wanted:
                report.hidden_region += 1
                continue
        elif region not in wanted:
            report.skipped_region += 1
            continue
        ids.append(entry["id"])
    return ids


def _save(store, kind, ids, docs, cpv, report) -> None:
    """Store fetched documents; the caller holds the transaction that also moves the cursor."""
    for doc_id, doc in zip(ids, docs):
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


async def _sync_back(api, store, kind, wanted, cpv, start, report, progress, now):
    """First sync: newest change first, back to the start of the period."""
    offset = store.get_state(f"desc:{kind}") or None
    fresh = offset is None
    began = now()
    done = 0
    while True:
        page = await api.feed(kind, offset, opt_fields="procuringEntity", descending=True)
        in_period = [e for e in page.items if (_ts(e.get("dateModified")) or start) >= start]
        finished = (not page.items or len(in_period) < len(page.items)
                    or page.next_offset == str(offset))
        ids = _wanted_ids(in_period, wanted, report)
        docs = await asyncio.gather(*(api.get(kind, i) for i in ids))
        with store.transaction():
            _save(store, kind, ids, docs, cpv, report)
            if fresh:
                # where the feed stood when this pass began: later runs go forward from here
                store.set_state(f"cursor:{kind}", page.prev_offset or str(began))
                fresh = False
            if finished:
                store.conn.execute("DELETE FROM sync_state WHERE key=?", (f"desc:{kind}",))
            else:
                store.set_state(f"desc:{kind}", page.next_offset)
        done += len(ids)
        if progress:
            last = _ts(page.items[-1].get("dateModified")) if page.items else start
            span = began - start
            fraction = 1.0 if finished else (min(max((began - last) / span, 0.0), 1.0) if last and span > 0
                                             else 0.0)
            elapsed = now() - began
            eta = elapsed * (1 - fraction) / fraction if 0 < fraction < 1 else None
            progress(SyncProgress(kind, done, fraction, eta))
        if finished:
            break
        offset = page.next_offset


async def _sync_kind(api, store, kind, wanted, cpv, start, report, progress, now):
    """Later syncs: forward from the saved cursor."""
    offset = store.get_state(f"cursor:{kind}") or start
    began = now()
    done = 0
    while True:
        page = await api.feed(kind, offset, opt_fields="procuringEntity")
        if not page.items:
            break
        matching = _wanted_ids(page.items, wanted, report)
        docs = await asyncio.gather(*(api.get(kind, i) for i in matching))
        with store.transaction():
            _save(store, kind, matching, docs, cpv, report)
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
