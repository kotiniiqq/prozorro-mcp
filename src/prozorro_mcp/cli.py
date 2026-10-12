"""prozorro-mcp: sync Prozorro data, query it without an AI client, or serve it over MCP."""
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from .analytics import flags as flags_mod
from .analytics import search, watch
from .analytics.common import NotFound, coverage_info, parse_since
from .api import APIError, ProzorroAPI
from .normalize import UnknownInput, resolve_cpv, resolve_region
from .output import render
from .store import Store
from .sync import SyncError, SyncProgress, run_sync

_api_factory = ProzorroAPI
_now = time.time


def default_db() -> Path:
    return Path(os.environ.get("PROZORRO_MCP_DB") or Path.home() / ".prozorro-mcp" / "data.db")


def _open_store(args) -> Store:
    if getattr(args, "demo", False):
        from .demo import demo_store
        return demo_store()
    return Store(args.db or default_db())


def _region(text):
    return resolve_region(text) if text else None


def _cpv(text):
    return resolve_cpv(text) if text else None


def _emit(args, rows: list[dict], columns: list[str]) -> None:
    text = render(rows, columns, args.format)
    if args.out:
        encoding = "utf-8-sig" if args.format == "csv" else "utf-8"
        Path(args.out).write_text(text + ("" if text.endswith("\n") else "\n"), encoding=encoding)
        print(f"saved {len(rows)} rows to {args.out}")
    else:
        print(text)


def _short(row: dict) -> dict:
    out = dict(row)
    for key in ("deadline", "published", "planned_tender_start"):
        if out.get(key):
            out[key] = out[key][:16].replace("T", " ")
    return out


TENDER_COLS = ["tender_id", "deadline", "amount_text", "buyer", "title", "url"]


def cmd_search(args, store):
    res = search.search_tenders(store, query=args.query, cpv=_cpv(args.cpv), region=_region(args.region),
                                status=args.status, amount_min=args.amount_min, amount_max=args.amount_max,
                                deadline_after=parse_since(args.deadline_after),
                                published_since=parse_since(args.since), limit=args.limit)
    _emit(args, [_short(r) for r in res["tenders"]], TENDER_COLS)
    if res.get("note") and args.format == "table" and not args.out:
        print(res["note"], file=sys.stderr)


def cmd_digest(args, store):
    res = watch.watch_digest(store, since=parse_since(args.since or "24h"), limit=args.limit)
    if res.get("hint"):
        print(res["hint"], file=sys.stderr)
    rows = [{"watch": w["name"], **_short(t)} for w in res["watches"] for t in w["tenders"]]
    _emit(args, rows, ["watch", "tender_id", "published", "amount_text", "title", "url"])


def cmd_flags(args, store):
    res = flags_mod.red_flags(store, buyer=args.buyer, cpv=_cpv(args.cpv), region=_region(args.region),
                              since=parse_since(args.since), kinds=args.kind or None, limit=args.limit)
    _emit(args, res["flags"], ["kind", "tender_id", "buyer", "amount_text", "evidence", "url"])
    if args.format == "table" and not args.out:
        counts = ", ".join(f"{k}: {v}" for k, v in res["counts"].items())
        print(f"{counts}\n{res['note']}", file=sys.stderr)


def cmd_plans(args, store):
    res = search.upcoming_plans(store, query=args.query, cpv=_cpv(args.cpv), region=_region(args.region),
                                budget_min=args.budget_min, budget_max=args.budget_max,
                                start_from=parse_since(args.start_from), limit=args.limit)
    _emit(args, [_short(r) for r in res["plans"]],
          ["planned_tender_start", "budget_text", "buyer", "description", "cpv", "url"])


def cmd_status(args, store):
    info = coverage_info(store)
    rows = [{"key": k, "value": ", ".join(v) if isinstance(v, list) else v} for k, v in info.items()]
    _emit(args, rows, ["key", "value"])


def cmd_watch(args, store):
    if args.watch_cmd == "add":
        watch.watch_add(store, args.name, query=args.query, cpv=_cpv(args.cpv), region=_region(args.region))
        print(f"saved search '{args.name}' added")
    elif args.watch_cmd == "remove":
        watch.watch_remove(store, args.name)
        print(f"saved search '{args.name}' removed")
    else:
        _emit(args, watch.watch_list(store), ["name", "query", "cpv", "region", "created"])


def _print_progress(p: SyncProgress) -> None:
    eta = f", ~{max(1, round(p.eta_seconds / 60))} min left" if p.eta_seconds else ""
    print(f"\r[{p.kind}] {p.done} fetched, {p.fraction:.0%} of the period{eta}   ", end="", file=sys.stderr,
          flush=True)


def cmd_sync(args, store):
    regions = [resolve_region(r) for r in args.region] if args.region else None
    cpv = sorted({p for c in args.cpv for p in resolve_cpv(c)}) if args.cpv else None

    async def go():
        api = _api_factory()
        try:
            return await run_sync(api, store, regions=regions, days=args.days, cpv=cpv,
                                  progress=_print_progress, plans=not args.no_plans, now=_now)
        finally:
            if hasattr(api, "aclose"):
                await api.aclose()

    report = asyncio.run(go())
    print(file=sys.stderr)
    print(f"done: stored {report.stored} tenders, {report.plans_stored} plans; "
          f"skipped {report.skipped_region} from other regions, {report.skipped_cpv} by CPV, "
          f"{report.hidden_region} with a hidden address (mostly military units; "
          f'sync them with --region "без регіону"); {report.missing} no longer in Prozorro')


def _eval_lines(results: dict) -> list[str]:
    lines = []
    for name, res in results.items():
        status = "PASS" if res["passed"] else "FAIL"
        if name == "flags":
            lines.append(f"## flags: {status} ({res['cases']} labelled cases)\n")
            lines.append("| kind | precision | recall | tp | fp | fn | tn |\n|---|---|---|---|---|---|---|")
            for kind, m in res["per_kind"].items():
                lines.append(f"| {kind} | {m['precision']:.3f} | {m['recall']:.3f} | {m['tp']} | {m['fp']} | "
                             f"{m['fn']} | {m['tn']} |")
            if res["failures"]:
                lines.append("\nmisjudged: " + "; ".join(res["failures"]))
        elif name == "normalize":
            lines.append(f"## normalize: {status} (accuracy {res['accuracy']:.3f} on {res['cases']} spellings)")
            for f in res["failures"]:
                lines.append(f"- `{f['input']}` -> {f['got']} (expected {f['expected']})")
        else:
            lines.append(f"## reconcile: {status} ({res['checked']} aggregates checked against a recount "
                         f"from raw JSON, {len(res['mismatches'])} mismatches)")
            for m in res["mismatches"]:
                lines.append(f"- {m}")
        lines.append("")
    return lines


def cmd_eval(args, store):
    from datetime import date

    from . import evals
    results = evals.run_suite(args.suite)
    lines = _eval_lines(results)
    print("\n".join(lines))
    if args.write:
        header = [f"# Eval results ({date.today().isoformat()})", "",
                  ("Computed by code, no API keys: `prozorro-mcp eval --suite all`. The reconcile suite runs on "
                  "the bundled demo snapshot."), ""]
        Path(args.write).write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    if not all(r["passed"] for r in results.values()):
        raise SystemExit(1)


def cmd_serve(args, store):
    from .server import build_server
    server = build_server(store)
    if args.http:
        print(f"MCP over streamable HTTP on http://{args.host}:{args.port}/mcp", file=sys.stderr)
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prozorro-mcp", description=__doc__)
    p.add_argument("--db", help="database path (default ~/.prozorro-mcp/data.db or $PROZORRO_MCP_DB)")
    p.add_argument("--format", choices=["table", "csv", "json"], default="table")
    p.add_argument("--out", help="write the result to a file (CSV gets a BOM so Excel reads Cyrillic)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="download tenders and plans for your regions")
    s.add_argument("--region", action="append", help="oblast or city in any spelling; repeat for several")
    s.add_argument("--days", type=int, help="how many days back (default 7)")
    s.add_argument("--cpv", action="append", help="keep only these ДК 021 codes/words; repeatable")
    s.add_argument("--no-plans", action="store_true", help="skip annual plans")
    s.set_defaults(func=cmd_sync)

    sub.add_parser("status", help="what the local data covers").set_defaults(func=cmd_status)

    def filters(sp, demo=True):
        sp.add_argument("--query", help="words in the title or items")
        sp.add_argument("--cpv", help="ДК 021 code, prefix or words")
        sp.add_argument("--region", help="oblast or city in any spelling")
        sp.add_argument("--limit", type=int, default=20)
        if demo:
            sp.add_argument("--demo", action="store_true", help="use the bundled demo data")

    s = sub.add_parser("search", help="find tenders")
    filters(s)
    s.add_argument("--status", default="open", help="open (default), any, or a Prozorro status")
    s.add_argument("--amount-min", type=float)
    s.add_argument("--amount-max", type=float)
    s.add_argument("--deadline-after")
    s.add_argument("--since", help="published since: 7d, 24h or 2026-10-01")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("plans", help="upcoming purchases from annual plans")
    filters(s)
    s.add_argument("--budget-min", type=float)
    s.add_argument("--budget-max", type=float)
    s.add_argument("--from", dest="start_from", help="planned start from: 2026-11-01")
    s.set_defaults(func=cmd_plans)

    s = sub.add_parser("digest", help="new tenders for your saved searches")
    s.add_argument("--since", help="default 24h; also 7d or 2026-10-01")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--demo", action="store_true")
    s.set_defaults(func=cmd_digest)

    s = sub.add_parser("flags", help="red flags for a buyer, CPV or region")
    s.add_argument("--buyer", help="buyer EDRPOU")
    s.add_argument("--cpv")
    s.add_argument("--region")
    s.add_argument("--since")
    s.add_argument("--kind", action="append", choices=list(flags_mod.KINDS))
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--demo", action="store_true")
    s.set_defaults(func=cmd_flags)

    w = sub.add_parser("watch", help="saved searches")
    wsub = w.add_subparsers(dest="watch_cmd", required=True)
    wa = wsub.add_parser("add")
    wa.add_argument("name")
    wa.add_argument("--query")
    wa.add_argument("--cpv")
    wa.add_argument("--region")
    wsub.add_parser("list")
    wr = wsub.add_parser("remove")
    wr.add_argument("name")
    w.set_defaults(func=cmd_watch)

    s = sub.add_parser("eval", help="run the keyless evals")
    s.add_argument("--suite", default="all", help="flags, normalize, reconcile or all")
    s.add_argument("--write", help="also write the results as Markdown to this file")
    s.set_defaults(func=cmd_eval, no_store=True)

    s = sub.add_parser("serve", help="run the MCP server (stdio by default)")
    s.add_argument("--demo", action="store_true", help="serve the bundled demo data")
    s.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        store = None if getattr(args, "no_store", False) else _open_store(args)
    except OSError as exc:
        print(f"error: cannot open the database: {exc}", file=sys.stderr)
        return 1
    try:
        args.func(args, store)
        return 0
    except (UnknownInput, NotFound, SyncError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except APIError as exc:
        print(f"\nerror: {exc}\nWhat was downloaded is saved; run the same command again to continue.",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped. What was downloaded is saved; run the same command again to continue.",
              file=sys.stderr)
        return 130
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
