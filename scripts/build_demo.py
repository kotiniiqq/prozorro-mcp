"""Build the bundled demo snapshot from live Prozorro data.

    python scripts/build_demo.py [--region Львівська] [--days 7]

Syncs into a temporary database with the normal sync code, then writes slimmed tenders and plans
(contact persons removed) to src/prozorro_mcp/data/demo.jsonl.gz.
"""
import argparse
import asyncio
import gzip
import json
import sys
import tempfile
from pathlib import Path

from prozorro_mcp.api import ProzorroAPI
from prozorro_mcp.demo import slim_plan, slim_tender
from prozorro_mcp.normalize import resolve_region
from prozorro_mcp.privacy import strip_contacts
from prozorro_mcp.store import Store
from prozorro_mcp.sync import run_sync

OUT = Path(__file__).resolve().parent.parent / "src" / "prozorro_mcp" / "data" / "demo.jsonl.gz"
LIMIT = 5 * 1024 * 1024


def progress(p):
    print(f"\r[{p.kind}] {p.done} fetched, {p.fraction:.0%}   ", end="", file=sys.stderr, flush=True)


async def build(region: str, days: int, db: Path) -> Store:
    store = Store(db)
    async with ProzorroAPI() as api:
        report = await run_sync(api, store, regions=[region], days=days, progress=progress)
    print(f"\n{report}", file=sys.stderr)
    return store


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="Львівська")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    region = resolve_region(args.region)
    with tempfile.TemporaryDirectory() as tmp:
        store = asyncio.run(build(region, args.days, Path(tmp) / "demo.db"))
        state = {k: store.get_state(k) for k in ("coverage", "last_sync")}
        with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as f:
            f.write(json.dumps({"kind": "meta", "state": state}, ensure_ascii=False) + "\n")
            for (raw,) in store.conn.execute("SELECT raw FROM tenders WHERE missing = 0 ORDER BY id"):
                doc = strip_contacts(slim_tender(json.loads(raw)))
                f.write(json.dumps({"kind": "tender", "doc": doc}, ensure_ascii=False) + "\n")
            # only plans still waiting for a tender: upcoming_plans shows nothing else, and it keeps the file small
            for (raw,) in store.conn.execute(
                    "SELECT raw FROM plans WHERE tender_id IS NULL AND status = 'scheduled' ORDER BY id"):
                doc = strip_contacts(slim_plan(json.loads(raw)))
                f.write(json.dumps({"kind": "plan", "doc": doc}, ensure_ascii=False) + "\n")
        counts = [store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tenders", "plans")]
        store.close()
    size = OUT.stat().st_size
    print(f"wrote {OUT.name}: {counts[0]} tenders, {counts[1]} plans, {size / 1e6:.2f} MB")
    if size > LIMIT:
        sys.exit(f"snapshot is {size / 1e6:.1f} MB, over the 5 MB limit: use fewer --days")


if __name__ == "__main__":
    main()
