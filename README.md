# prozorro-mcp

An MCP server and a command-line tool for **Prozorro**, Ukraine's public procurement system. It keeps a
local copy of the official open data for the regions you choose. Then you can ask an AI assistant
(Claude, Cursor, VS Code Copilot and others), or the CLI on its own, things like:

- *"Which open tenders for laboratory reagents are in Lviv oblast, and when are the deadlines?"*
- *"What will hospitals in my region buy next month? Show plans that are not tenders yet."*
- *"Who wins most of the flour tenders here, and at what price per kilogram?"*
- *"Who do we usually compete with (EDRPOU 12345678), and how often do we win against them?"*
- *"Any red flags at this buyer: single bidders, one supplier taking most of the money?"*

Every number is computed by code from the data, and the assistant only writes the text. No API keys are
needed: the Prozorro API is public, and nothing here calls an LLM.

> Ukrainian step-by-step guide for non-developers: [docs/guide.uk.md](https://github.com/kotiniiqq/prozorro-mcp/blob/main/docs/guide.uk.md).

## Try it in one minute (bundled demo, no sync)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (or plain `pip install prozorro-mcp`):

```bash
uvx prozorro-mcp search --demo --query борошно --status any
```

The demo is a real snapshot of Lviv oblast for one week (5–12 October 2026): 5,479 tenders, 1,120 of
them with public bids, and 1,619 upcoming plans. Contact persons were removed.

## Claude Desktop: one-click extension

Download [prozorro-mcp.mcpb](https://github.com/kotiniiqq/prozorro-mcp/releases/latest/download/prozorro-mcp.mcpb)
and open it (or drag it into Settings → Extensions). Claude Desktop installs Python and the package by
itself, so there is nothing else to install and no config to edit. In the install dialog, type your
oblasts (for example `Львівська, Волинська`) and the days of history.

It answers from the bundled demo right away, while your regions download in the background. Every
start fetches what changed since the last one. To add a region later, ask Claude: *"download Volyn oblast
for the last two weeks"*.

## Connect it to other AI clients

The server runs locally over stdio. Pick your client:

**Claude Code**

```bash
claude mcp add prozorro -- uvx prozorro-mcp serve
```

**Claude Desktop without the extension**: Settings → Developer → Edit Config. The file is
`%APPDATA%\Claude\claude_desktop_config.json` on Windows and
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS:

```json
{
  "mcpServers": {
    "prozorro": {
      "command": "uvx",
      "args": ["prozorro-mcp", "serve"]
    }
  }
}
```

**Cursor** (`~/.cursor/mcp.json`) and **Windsurf, Cline and other clients**: the same `mcpServers` block
as Claude Desktop.

**VS Code** (`.vscode/mcp.json`):

```json
{
  "servers": {
    "prozorro": {
      "type": "stdio",
      "command": "uvx",
      "args": ["prozorro-mcp", "serve"]
    }
  }
}
```

To try it on the demo first, add `"--demo"` after `"serve"`. Restart the client after editing the config.

ChatGPT and claude.ai in the browser only connect to remote servers over HTTPS. See
[HTTP mode](#http-mode).

## Sync your own data

```bash
uvx prozorro-mcp sync --region Львівська --days 7
```

- **Regions** work in any spelling: `Львів`, `львовская`, `Lviv`, `Kyiv city`, `Київська`. Repeat
  `--region` for several. Military units hide their address by law, so their tenders have no region.
  Add them with `--region "без регіону"`.
- **First sync** walks the change feed back over the period and downloads every matching tender. A busy
  oblast takes about 20 minutes per week of data (Lviv: about 5,500 tenders and as many plans, roughly
  350 MB on disk), mostly waiting on the public API, which is used
  politely with 5 requests at a time and retries. `--cpv 33` (or `--cpv реактиви`) keeps only your
  categories.
- **Next runs** (`prozorro-mcp sync` with no options) fetch only what changed since the last run.
  Ctrl+C is safe: run the command again and it continues from where it stopped.
- **From the server**: `prozorro-mcp serve --sync-on-start` fetches what changed in the background when the
  client starts it (add `--region` and `--days` to widen the coverage). Until the first sync finishes,
  the tools answer from the demo and every result says so in `data_source`.
- **Status**: `prozorro-mcp status` shows what is covered. Data lives in `~/.prozorro-mcp/data.db`; set
  `PROZORRO_MCP_DB` to move it.

## Use it without an AI client

The same numbers, as a table, CSV (opens in Excel with Cyrillic intact) or JSON. Install the command
once with `uv tool install prozorro-mcp`, and then:

```bash
prozorro-mcp search --cpv реактиви --region Львівська            # open tenders, nearest deadline first
prozorro-mcp plans --query "лабораторні" --budget-min 100000    # upcoming purchases
prozorro-mcp flags --buyer 05498849                             # red flags for one buyer
prozorro-mcp watch add "реактиви Львів" --cpv 33696 --region Львівська
prozorro-mcp digest --since 24h --format csv --out digest.csv   # what is new for your saved searches
```

Run `sync` and `digest` every morning with Task Scheduler or cron (the guide shows how), and you have a
daily list of new tenders without opening any website.

## Tools

Eleven tools only read the local database. Each one declares `readOnlyHint: true`,
`destructiveHint: false`, `idempotentHint: true` and `openWorldHint: false`. The twelfth, `sync_data`,
downloads data, so it is marked `readOnlyHint: false` and `openWorldHint: true`. It exists only in the
local stdio server, not in HTTP mode.

| tool | what it returns |
|---|---|
| `coverage` | synced regions, period, counts, last sync; the command to add more |
| `find_entity` | EDRPOU codes of buyers and suppliers by part of a name |
| `search_tenders` | tenders by words, CPV, region, status, amount, deadline, publication date |
| `get_tender` | one tender: items, lots, every bid, the winner, documents with direct links |
| `market_overview` | count and value, competitive share, average bidders, unit-price spread, top buyers and winners |
| `supplier_profile` | wins, buyers, CPVs, win rate, median discount |
| `buyer_profile` | what it buys, from whom, procedure mix, single-bidder share |
| `competitors` | who bids in the same tenders and the head-to-head score |
| `upcoming_plans` | annual-plan items with no tender yet |
| `red_flags` | the four flags below, each with evidence and a link |
| `watch_digest` | new tenders for your saved searches |
| `sync_data` | starts a background download of regions or a longer period; `coverage` shows the progress |

When a question needs data that is not synced, for example another oblast or an older period, the
result says so and gives the exact `sync` command. It does not return an empty list that looks like
"nothing exists".

## How the numbers are defined

- **Open tender**: status `active.tendering` or `active.enquiries`, and the deadline has not passed as of
  the last sync. Prozorro sometimes changes the status late.
- **Competitive procedure**: open, below-threshold, price quotation, competitive dialogue, framework
  selection and similar types. `reporting` (a direct contract) and `negotiation` are not competitive.
  The full list is in `src/prozorro_mcp/models.py`.
- **Decided tender**: one that has an active award.
- **Unit price**: award ÷ quantity. It is computed only for tenders with exactly one item, and grouped
  by the full CPV code and unit, so kilograms are never compared with tonnes. Other tenders are counted
  as `excluded`.
- **Win rate**: decided tenders won ÷ decided tenders bid in. Bids become public only after the auction.
- **Discount**: (expected value − award) ÷ expected value, for competitive tenders without lots.

### Red flags

A flag is a reason to look closer, **not proof of wrongdoing**.

| flag | rule (defaults are parameters) |
|---|---|
| `single_bidder` | a competitive procedure that ended with one bidder |
| `supplier_concentration` | one supplier got more than 50% of a buyer's awarded money, with at least 5 awards |
| `price_above_median` | unit price at least 30% above the median of at least 5 comparable tenders (same CPV and unit) |
| `short_tender_period` | tendering shorter than 7 days (open), 15 (EU), 3 (below-threshold, price quotation). This is a heuristic, not a legal check: martial-law rules change often |

## Evals

They run without keys or network, all computed by code: `prozorro-mcp eval --suite all`. The latest
results are in [evals/RESULTS.md](https://github.com/kotiniiqq/prozorro-mcp/blob/main/evals/RESULTS.md).

- **Flags**: 35 labelled cases, including near-threshold negatives such as 29% vs 30% above the median
  or exactly half the money. Precision and recall are 1.0 for every flag.
- **Regions and CPV words**: 94 spellings, 98.9% recognized. The one miss is shown in the results.
- **Reconcile**: buyer and category totals from the analytics are checked against an independent
  recount over the raw JSON of the demo snapshot. 35 aggregates, 0 mismatches.

## HTTP mode

```bash
prozorro-mcp serve --http --host 127.0.0.1 --port 8000      # MCP endpoint: http://127.0.0.1:8000/mcp
```

The same tools are served over streamable HTTP. To reach it from ChatGPT or claude.ai, put it behind a
reverse proxy with HTTPS (Caddy, nginx). Version 1 has no authentication: the data is public and
read-only. Add rate limits at the proxy before exposing it.

## Limits

- **Local copy**: you sync what you need. All of Ukraine for a month is about 100k requests to the
  public API, and that is not the intended local use.
- **The period means last modification**: a sync covers tenders changed in the last N days. Old tenders
  that are still being updated come along.
- **Hidden addresses**: tenders of military units have no region in Prozorro (see `--region "без регіону"`).
- **CPV by words** matches the official ДК 021 names. When the words are too broad, pass a code prefix.
  Tools echo the categories they used.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"    # Windows: .venv\Scripts\pip
pytest -q          # 197 tests, no network: recorded API responses and built tenders
ruff check .
python scripts/capture_fixtures.py    # refresh the recorded API responses
python scripts/build_demo.py          # rebuild the demo snapshot (live sync, about 20 minutes)
python scripts/build_mcpb.py          # the Claude Desktop extension (needs Node for the official packer)
```

The code is split by job: `api` (feed client) → `sync` → `store` (SQLite) → `analytics` (every number)
→ `server` (MCP) and `cli`. `normalize` handles regions and CPV.

## Roadmap

- **v2**: a `price_raised_after_signing` flag, for contracts whose price grew through amendments. This
  needs a sync of contract changes.
- **Later**: a hosted HTTP instance for ChatGPT and claude.ai; Telegram or e-mail alerts as a separate service; owners of
  companies from the EDR register.
- **Not planned**: a "chance to win" or "recommended bid". It would be a guess dressed as a fact.

## Data and credits

Tender and plan data come from the public [Prozorro API](https://public-api.prozorro.gov.ua). ДК 021
(CPV) names and the region list come from [ProzorroUKR/standards](https://github.com/ProzorroUKR/standards)
(Apache-2.0); see [NOTICE](https://github.com/kotiniiqq/prozorro-mcp/blob/main/NOTICE).

MIT License.
