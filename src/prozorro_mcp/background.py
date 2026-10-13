"""Sync in a background thread, so the MCP server keeps answering while data downloads.

Used by `serve`: on start (regions from the Claude Desktop extension settings) and by the `sync_data`
tool. The thread has its own SQLite connection; WAL lets the server read while it writes.

Claude Desktop runs several copies of the server on the same database (chat, Cowork, Code), so the
status lives in the database, not in memory: every copy reports the same progress, and only one copy
downloads at a time. The running copy refreshes a heartbeat; a copy that died stops blocking the others
once its heartbeat is older than STALE_SECONDS.
"""
import asyncio
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .api import APIError
from .store import Store
from .sync import SyncError, SyncProgress, get_coverage, run_sync

STATUS_KEY = "sync_status"
HEARTBEAT_SECONDS = 15
STALE_SECONDS = 90
_PRIVATE = ("heartbeat", "owner")


class BackgroundSync:
    def __init__(self, db_path: str | Path, api_factory: Callable, now: Callable[[], float] = time.time,
                 heartbeat_seconds: float = HEARTBEAT_SECONDS):
        self.db_path = Path(db_path)
        self._api_factory = api_factory
        self._now = now
        self._heartbeat_seconds = heartbeat_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._owner = uuid.uuid4().hex

    # -- status shared through the database --------------------------------------------------------

    def _read(self, store: Store) -> dict:
        raw = store.get_state(STATUS_KEY)
        status = json.loads(raw) if raw else {"state": "idle"}
        if status.get("state") == "running" and self._now() - status.get("heartbeat", 0) > STALE_SECONDS:
            status = {**status, "state": "interrupted",
                      "message": "The last download stopped before finishing (the app was closed?). Start it "
                                 "again and it continues from where it stopped."}
        return status

    def _patch(self, store: Store, changes: dict) -> None:
        """Update only this copy's status, field by field, so the heartbeat and progress never clash."""
        with store.transaction():
            store.conn.execute(
                "UPDATE sync_state SET value=json_patch(value, ?) WHERE key=? AND "
                "json_extract(value, '$.owner')=?", (json.dumps(changes, ensure_ascii=False), STATUS_KEY,
                                                    self._owner))

    @staticmethod
    def _public(status: dict) -> dict:
        return {k: v for k, v in status.items() if k not in _PRIVATE}

    def status(self) -> dict:
        store = Store(self.db_path)
        try:
            return self._public(self._read(store))
        finally:
            store.close()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    # -- starting a download -----------------------------------------------------------------------

    def start(self, regions: list[str] | None = None, days: int | None = None,
              cpv: list[str] | None = None) -> dict:
        with self._lock:
            store = Store(self.db_path)
            conn = store.conn
            conn.execute("BEGIN IMMEDIATE")  # one copy at a time decides whether a download may start
            try:
                current = self._read(store)
                if current["state"] == "running":
                    conn.rollback()
                    store.close()
                    return {**self._public(current), "started": False,
                            "message": "A download is already running (maybe in another window); check its "
                                       "progress with coverage."}
                cov = get_coverage(store)
                if cov is None and not regions:
                    conn.rollback()
                    store.close()
                    return {**self._public(current), "started": False,
                            "message": "Nothing synced yet: pass at least one region, e.g. Львівська."}
                status = {"state": "running",
                          "regions": sorted(set(regions or []) | set(cov.regions if cov else [])),
                          "started_at": self._iso(), "kind": None, "fetched": 0, "fraction": 0.0,
                          "eta_minutes": None, "heartbeat": self._now(), "owner": self._owner}
                conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?)",
                             (STATUS_KEY, json.dumps(status, ensure_ascii=False)))
                conn.commit()
            except BaseException:
                conn.rollback()
                store.close()
                raise
            self._thread = threading.Thread(target=self._run, args=(store, regions, days, cpv),
                                            name="prozorro-sync", daemon=True)
            self._thread.start()
            return {**self._public(status), "started": True,
                    "message": "Sync started in the background. The first sync of a busy oblast takes about "
                               "20 minutes per week of data; check progress with coverage."}

    def _iso(self) -> str:
        return datetime.fromtimestamp(self._now(), UTC).isoformat()

    def _beat(self, stop: threading.Event) -> None:
        store = Store(self.db_path)
        try:
            while not stop.wait(self._heartbeat_seconds):
                self._patch(store, {"heartbeat": self._now()})
        finally:
            store.close()

    def _run(self, store: Store, regions, days, cpv) -> None:
        stop = threading.Event()
        beat = threading.Thread(target=self._beat, args=(stop,), name="prozorro-sync-heartbeat", daemon=True)
        beat.start()

        def progress(p: SyncProgress) -> None:
            self._patch(store, {"kind": p.kind, "fetched": p.done, "fraction": round(p.fraction, 3),
                                "eta_minutes": round(p.eta_seconds / 60, 1) if p.eta_seconds else None,
                                "heartbeat": self._now()})

        async def go():
            api = self._api_factory()
            try:
                return await run_sync(api, store, regions=regions, days=days, cpv=cpv, progress=progress,
                                      now=self._now)
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
        stop.set()
        beat.join()
        try:
            self._patch(store, {**final, "finished_at": self._iso()})
        finally:
            store.close()
