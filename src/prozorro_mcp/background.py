"""Sync in a background thread, so the MCP server keeps answering while data downloads.

Used by `serve`: on start (regions from the Claude Desktop extension settings) and by the `sync_data`
tool. The thread has its own SQLite connection; WAL lets the server read while it writes.
"""
import asyncio
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .api import APIError
from .store import Store
from .sync import SyncError, SyncProgress, get_coverage, run_sync


class BackgroundSync:
    def __init__(self, db_path: str | Path, api_factory: Callable, now: Callable[[], float] = time.time):
        self.db_path = Path(db_path)
        self._api_factory = api_factory
        self._now = now
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = {"state": "idle"}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def start(self, regions: list[str] | None = None, days: int | None = None,
              cpv: list[str] | None = None) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {**self._state, "started": False,
                        "message": "A sync is already running; check its progress with coverage."}
            store = Store(self.db_path)
            if get_coverage(store) is None and not regions:
                store.close()
                return {**self._state, "started": False,
                        "message": "Nothing synced yet: pass at least one region, e.g. Львівська."}
            cov = get_coverage(store)
            target = sorted(set(regions or []) | set(cov.regions if cov else []))
            self._state = {"state": "running", "regions": target, "started_at": self._iso(),
                           "kind": None, "fetched": 0, "fraction": 0.0, "eta_minutes": None}
            self._thread = threading.Thread(target=self._run, args=(store, regions, days, cpv),
                                            name="prozorro-sync", daemon=True)
            self._thread.start()
            return {**self._state, "started": True,
                    "message": "Sync started in the background. The first sync of a busy oblast takes about "
                               "20 minutes per week of data; check progress with coverage."}

    def _iso(self) -> str:
        return datetime.fromtimestamp(self._now(), UTC).isoformat()

    def _progress(self, p: SyncProgress) -> None:
        with self._lock:
            self._state.update(kind=p.kind, fetched=p.done, fraction=round(p.fraction, 3),
                               eta_minutes=round(p.eta_seconds / 60, 1) if p.eta_seconds else None)

    def _run(self, store: Store, regions, days, cpv) -> None:
        async def go():
            api = self._api_factory()
            try:
                return await run_sync(api, store, regions=regions, days=days, cpv=cpv,
                                      progress=self._progress, now=self._now)
            finally:
                if hasattr(api, "aclose"):
                    await api.aclose()

        try:
            report = asyncio.run(go())
            final = {"state": "done", "stored": report.stored, "plans_stored": report.plans_stored,
                     "message": "Sync finished."}
        except (APIError, SyncError, OSError) as exc:
            final = {"state": "error",
                     "message": f"Sync stopped: {exc}. What was downloaded is saved; start it again to continue."}
        except Exception as exc:  # noqa: BLE001 - the server must survive anything the thread hits
            final = {"state": "error", "message": f"Sync failed unexpectedly: {exc!r}. Start it again to retry."}
        finally:
            store.close()
        with self._lock:
            self._state.update(final, finished_at=self._iso())
