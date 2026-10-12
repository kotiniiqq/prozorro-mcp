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
