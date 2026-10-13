import csv
import io
import json

import pytest
from factories import make_plan, make_tender
from test_sync import NOW, FakeAPI, entry

from prozorro_mcp import cli
from prozorro_mcp.store import Store


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cli.db"
    s = Store(path)
    s.set_state("last_sync", "2026-10-01T00:00:00+03:00")
    s.upsert_tender(make_tender(id="t1", status="active.tendering", title="Борошно пшеничне", amount=12345.5,
                                created="2026-10-12T09:00:00+03:00"))
    s.upsert_tender(make_tender(id="t2", status="complete", title="Бензин А-95",
                                items=[("09132000-3", "Бензин", 10, "LTR")], bids=[("22222222", "ТОВ Альфа", 1.0)]))
    s.upsert_plan(make_plan(description="Реактиви лабораторні", budget=250000.0))
    s.close()
    return str(path)


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_search_table(db, capsys):
    code, out, _ = run(["--db", db, "search", "--query", "борошно"], capsys)
    assert code == 0 and "Борошно пшеничне" in out and "12 345,50 грн" in out and "Бензин" not in out


def test_search_csv_and_json(db, capsys):
    code, out, _ = run(["--db", db, "--format", "csv", "search", "--status", "any"], capsys)
    rows = list(csv.DictReader(io.StringIO(out)))
    assert code == 0 and len(rows) == 2 and "url" in rows[0]
    code, out, _ = run(["--db", db, "--format", "json", "search", "--cpv", "бензин", "--status", "any"], capsys)
    assert json.loads(out)[0]["title"] == "Бензин А-95"


def test_out_file_csv_has_bom_for_excel(db, tmp_path, capsys):
    target = tmp_path / "out.csv"
    code, _, _ = run(["--db", db, "--format", "csv", "--out", str(target), "search", "--status", "any"], capsys)
    assert code == 0 and target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_unknown_region_exits_2_with_suggestion(db, capsys):
    code, _, err = run(["--db", db, "search", "--region", "Атлантида"], capsys)
    assert code == 2 and "Атлантида" in err


def test_flags_need_scope(db, capsys):
    code, _, err = run(["--db", db, "flags"], capsys)
    assert code == 2 and "scope" in err
    code, out, _ = run(["--db", db, "flags", "--region", "Львівська", "--kind", "single_bidder"], capsys)
    assert code == 0 and "single_bidder" in out


def test_watch_add_list_remove_and_digest(db, capsys):
    assert run(["--db", db, "watch", "add", "борошно", "--query", "борошно"], capsys)[0] == 0
    code, out, _ = run(["--db", db, "watch", "list"], capsys)
    assert "борошно" in out
    code, out, _ = run(["--db", db, "digest", "--since", "2026-10-11"], capsys)
    assert code == 0 and "Борошно пшеничне" in out
    assert run(["--db", db, "watch", "remove", "борошно"], capsys)[0] == 0
    assert run(["--db", db, "watch", "remove", "борошно"], capsys)[0] == 2


def test_digest_since_parsing_error(db, capsys):
    code, _, err = run(["--db", db, "digest", "--since", "вчора"], capsys)
    assert code == 2 and "7d" in err


def test_sync_uses_the_api_and_prints_progress(tmp_path, capsys, monkeypatch):
    a = make_tender(id="a")
    fake = FakeAPI(tenders_pages=[[entry(a)]], docs=[a])
    monkeypatch.setattr(cli, "_api_factory", lambda: fake)
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    db = str(tmp_path / "s.db")
    code, out, err = run(["--db", db, "sync", "--region", "львів", "--days", "3", "--no-plans"], capsys)
    assert code == 0 and "stored 1" in out and "[tenders]" in err
    code, out, _ = run(["--db", db, "status"], capsys)
    assert "Львівська область" in out and "tenders" in out


def test_sync_first_time_without_region_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_api_factory", lambda: FakeAPI())
    code, _, err = run(["--db", str(tmp_path / "x.db"), "sync"], capsys)
    assert code == 2 and "--region" in err


def test_plans_command(db, capsys):
    code, out, _ = run(["--db", db, "plans", "--query", "реактиви"], capsys)
    assert code == 0 and "250 000,00 грн" in out and "Реактиви лабораторні" in out


def test_eval_command_prints_metrics_and_writes_results(tmp_path, capsys):
    target = tmp_path / "RESULTS.md"
    code, out, _ = run(["eval", "--suite", "flags", "--write", str(target)], capsys)
    assert code == 0 and "single_bidder" in out and "precision" in out
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Eval results") and "single_bidder" in text


def test_eval_unknown_suite_exits_2(capsys):
    assert run(["eval", "--suite", "vibes"], capsys)[0] == 2


def test_unsynced_region_prints_the_sync_command(db, capsys):
    import json as _json
    s = Store(db)
    s.set_state("coverage", _json.dumps({"regions": ["Львівська область"], "days": 7, "cpv": [],
                                         "since": "2026-09-01T00:00:00+00:00"}, ensure_ascii=False))
    s.close()
    for cmd in (["search", "--region", "Одеса"], ["plans", "--region", "Одеса"],
                ["flags", "--region", "Одеса"]):
        code, _, err = run(["--db", db, *cmd], capsys)
        assert code == 0 and 'prozorro-mcp sync --region "Одеська область"' in err, cmd


def test_progress_line_drops_eta_when_done(capsys):
    from prozorro_mcp.sync import SyncProgress
    cli._print_progress(SyncProgress("tenders", 10, 1.0, 0.4))
    assert "left" not in capsys.readouterr().err
    cli._print_progress(SyncProgress("tenders", 10, 0.5, 120.0))
    assert "~2 min left" in capsys.readouterr().err


class Captured:
    def __init__(self):
        self.store = self.syncer = self.transport = self.demo_while_running = None

    def build(self, store, syncer=None):
        self.store, self.syncer = store, syncer
        return self

    def run(self, transport, **kw):
        self.transport = transport
        if self.syncer is not None:
            self.syncer.wait(10)
        self.demo_while_running = getattr(self.store, "is_demo", None)


@pytest.fixture
def served(monkeypatch):
    import prozorro_mcp.server as server_mod
    cap = Captured()
    monkeypatch.setattr(server_mod, "build_server", cap.build)
    return cap


def test_serve_syncs_on_start_the_regions_from_the_settings(tmp_path, capsys, monkeypatch, served):
    a = make_tender(id="a")
    fake = FakeAPI(tenders_pages=[[entry(a)]], docs=[a])
    monkeypatch.setattr(cli, "_api_factory", lambda: fake)
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    db = str(tmp_path / "s.db")
    code, _, _ = run(["--db", db, "serve", "--sync-on-start", "--region", "Львів, Волинь", "--days", "3"], capsys)
    assert code == 0 and served.transport == "stdio"
    status = served.syncer.status()
    assert status["state"] == "done" and status["regions"] == ["Волинська область", "Львівська область"]
    assert served.demo_while_running is False  # the finished sync switched it to the live data


def test_serve_with_empty_settings_on_a_fresh_install_starts_nothing(tmp_path, capsys, monkeypatch, served):
    monkeypatch.setattr(cli, "_api_factory", lambda: FakeAPI())
    code, _, _ = run(["--db", str(tmp_path / "e.db"), "serve", "--sync-on-start", "--region", ""], capsys)
    assert code == 0 and served.syncer.status()["state"] == "idle" and served.demo_while_running


def test_serve_over_http_has_no_sync_tool(tmp_path, capsys, served):
    code, _, _ = run(["--db", str(tmp_path / "h.db"), "serve", "--http"], capsys)
    assert code == 0 and served.syncer is None and served.transport == "streamable-http"


def test_serve_unknown_region_in_settings_exits_2(tmp_path, capsys, served):
    code, _, err = run(["--db", str(tmp_path / "u.db"), "serve", "--sync-on-start", "--region", "Атлантида"],
                       capsys)
    assert code == 2 and "Атлантида" in err


@pytest.mark.parametrize("settings", [
    ["--region", "", "--days", ""],                                       # fields left empty
    ["--region", "${user_config.regions}", "--days", "${user_config.days}"],  # not substituted yet
    ["--days", "7.0"],                                                    # a number field sent as a float
])
def test_serve_starts_with_unfilled_extension_settings(tmp_path, capsys, monkeypatch, served, settings):
    """Claude Desktop starts the server before the user has saved the settings; it must not exit."""
    monkeypatch.setattr(cli, "_api_factory", lambda: FakeAPI())
    code, _, err = run(["--db", str(tmp_path / "f.db"), "serve", "--sync-on-start", *settings], capsys)
    assert code == 0 and served.transport == "stdio", err
