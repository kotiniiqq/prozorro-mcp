import threading

from factories import make_tender
from test_sync import NOW, FakeAPI, entry

from prozorro_mcp.background import BackgroundSync
from prozorro_mcp.store import Store


class GatedAPI(FakeAPI):
    """Holds the first feed call until the test opens the gate, so 'running' can be observed."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = threading.Event()

    async def feed(self, kind, offset, opt_fields=None):
        import asyncio
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        return await super().feed(kind, offset, opt_fields)


def syncer(tmp_path, api):
    return BackgroundSync(tmp_path / "bg.db", api_factory=lambda: api, now=lambda: NOW)


def test_runs_in_the_background_and_the_data_is_readable_from_another_connection(tmp_path):
    a = make_tender(id="a")
    api = GatedAPI(tenders_pages=[[entry(a)]], docs=[a])
    bg = syncer(tmp_path, api)
    first = bg.start(["Львівська область"], days=3)
    assert first["started"] and first["state"] == "running"
    api.gate.set()
    assert bg.wait(10)
    status = bg.status()
    assert status["state"] == "done" and status["stored"] == 1 and status["regions"] == ["Львівська область"]
    reader = Store(tmp_path / "bg.db")
    assert reader.conn.execute("SELECT id FROM tenders").fetchall()[0][0] == "a"
    reader.close()


def test_second_start_while_running_does_not_start_another(tmp_path):
    api = GatedAPI()
    bg = syncer(tmp_path, api)
    bg.start(["Львівська область"], days=3)
    again = bg.start(["Волинська область"], days=3)
    assert not again["started"] and again["state"] == "running"
    assert "already running" in again["message"]
    api.gate.set()
    assert bg.wait(10)


def test_an_api_failure_is_reported_and_a_new_start_resumes(tmp_path):
    a, b = make_tender(id="a"), make_tender(id="b")
    api = FakeAPI(tenders_pages=[[entry(a)], [entry(b)]], docs=[a, b], fail_on_get="b")
    bg = syncer(tmp_path, api)
    bg.start(["Львівська область"], days=3)
    assert bg.wait(10)
    status = bg.status()
    assert status["state"] == "error" and "down" in status["message"] and "again" in status["message"]
    api.fail_on_get = None
    bg.start()
    assert bg.wait(10) and bg.status()["state"] == "done"


def test_first_start_without_regions_explains_what_to_pass(tmp_path):
    bg = syncer(tmp_path, FakeAPI())
    res = bg.start()
    assert not res["started"] and res["state"] == "idle" and "region" in res["message"]


def test_idle_status_before_anything_ran(tmp_path):
    assert syncer(tmp_path, FakeAPI()).status()["state"] == "idle"


# Claude Desktop runs several copies of the server on one database (chat, Cowork, Code).


def test_a_second_copy_does_not_start_while_the_first_downloads_and_sees_its_progress(tmp_path):
    a = make_tender(id="a")
    api = GatedAPI(tenders_pages=[[entry(a)]], docs=[a])
    first, second = syncer(tmp_path, api), syncer(tmp_path, FakeAPI())
    first.start(["Львівська область"], days=3)
    res = second.start(["Волинська область"], days=3)
    assert not res["started"] and "already running" in res["message"]
    assert second.status()["state"] == "running"
    api.gate.set()
    assert first.wait(10)
    status = second.status()
    assert status["state"] == "done" and status["stored"] == 1


def test_a_copy_that_died_mid_download_does_not_block_forever(tmp_path):
    import json
    store = Store(tmp_path / "bg.db")
    store.set_state("coverage", json.dumps({"regions": ["Львівська область"], "days": 3, "cpv": [],
                                            "since": "2026-10-09T00:00:00+00:00"}))
    store.set_state("sync_status", json.dumps({"state": "running", "heartbeat": NOW - 3600}))
    store.close()
    bg = syncer(tmp_path, FakeAPI())
    assert bg.status()["state"] == "interrupted"
    assert bg.start()["started"]
    assert bg.wait(10) and bg.status()["state"] == "done"


def test_two_copies_starting_at_the_same_moment_run_one_download(tmp_path):
    api = GatedAPI()
    copies = [syncer(tmp_path, api) for _ in range(4)]
    results = []
    threads = [threading.Thread(target=lambda c=c: results.append(c.start(["Львівська область"], days=3)))
               for c in copies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["started"] for r in results) == 1
    api.gate.set()
    for c in copies:
        assert c.wait(10)


def test_the_running_copy_keeps_its_heartbeat_fresh(tmp_path):
    import json
    import time
    api = GatedAPI()
    bg = BackgroundSync(tmp_path / "bg.db", api_factory=lambda: api, heartbeat_seconds=0.05)
    bg.start(["Львівська область"], days=3)
    reader = Store(tmp_path / "bg.db")
    first = json.loads(reader.get_state("sync_status"))["heartbeat"]
    time.sleep(0.3)
    later = json.loads(reader.get_state("sync_status"))["heartbeat"]
    reader.close()
    api.gate.set()
    assert bg.wait(10) and later > first
