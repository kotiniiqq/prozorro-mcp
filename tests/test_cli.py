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
