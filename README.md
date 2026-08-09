# Paper Trader MCP

A local, durable, **simulation-only** MCP server for recording US-equity order
intents and later estimating outcomes from historical Alpaca SIP NBBO quotes.
It never places live orders or Alpaca paper-broker orders. There is no order
endpoint, broker SDK, generic fetch tool, trades endpoint, GTC, extended-hours,
fractional-share, or partial-fill support.

## Install and run

Requirements: Python 3.14, [`uv`](https://docs.astral.sh/uv/), and Alpaca data
credentials with historical SIP access.

```sh
./setup.sh
export APCA_API_KEY_ID='...'
export APCA_API_SECRET_KEY='...'
./run.sh
```

The scripts are executable and `run.sh` uses only this project's `.venv`.
Credentials are read from the environment (the server does not load `.env`).
Recording, listing, and closing intents work without credentials; evaluation
requires them because it reads Alpaca's quote and calendar APIs.
The database defaults to
`~/.local/share/paper-trader/paper-trader.sqlite3`; set `PAPER_TRADER_DB` to
override it. See `.env.example` for bounded quote-policy and commission options.

### Defaults you may want to configure

| Setting | Default | Meaning |
|---|---:|---|
| Commission per order | `$0` | Applied to every simulated fill |
| Commission per share | `$0` | Applied to every simulated fill |
| Order latency | `250 ms` | Override per order with `latency_ms` |
| Quote lookback | `120 s` | Search window for prevailing top-of-book quote |
| Maximum quote age | `30 s` | Older quotes are unusable |
| Additional slippage beyond spread | `$0` | Buys still cross to ask; sells cross to bid |

Current sell-side SEC Section 31 and FINRA TAF fees are automatic and itemized.
Set the commission variables only when modeling a broker that is not
commission-free. Short borrow costs, margin interest, dividends, venue fees,
rebates, and taxes are not configurable or modeled.

## MCP configuration

Claude Code (`~/.claude.json`, within the desired project entry) or Desktop /
Cowork (`claude_desktop_config.json`) can use the same stdio configuration:

```json
{
  "mcpServers": {
    "paper-trader": {
      "command": "/absolute/path/to/paper-trader-mcp/run.sh",
      "env": {
        "APCA_API_KEY_ID": "YOUR_DATA_KEY",
        "APCA_API_SECRET_KEY": "YOUR_DATA_SECRET"
      }
    }
  }
}
```

Prefer launching the client from a shell containing credentials rather than
writing secrets into configuration. The five tools are
`paper_trade_record`, `paper_trade_get`, `paper_trade_list`,
`paper_trade_evaluate`, and `paper_trade_close`. Mutation calls require a
caller-generated unique `request_id`; exact retries replay while changed input
conflicts. `submitted_at` is accepted only with `backtest=true`. Evaluation
without `as_of` explicitly selects `now - 16 minutes`.

## Model and safety caveats

This is an intentionally conservative estimate, not an execution simulator.
It crosses one complete top-of-book quote (ask to buy, bid to sell), requires
the displayed size to cover the entire order, assumes zero additional
slippage, and declares resting limits or insufficient depth indeterminate.
There is no L2/depth. Quotes must be regular-session, fresh, complete,
non-crossed, SIP-feed records on or after 2025-11-03 (Alpaca changed quote-size
units then). Equal timestamps retain API ordering and raw nanosecond timestamps.
Temporary SIP embargoes use `pending_data`; model limitations that prevent a
defensible fill use the distinct terminal state `indeterminate`.

DAY eligibility uses Alpaca's New York calendar, including holidays and early
closes. Data newer than the 15-minute entitlement plus a one-minute guard is
never queried or clamped. Weekend, pre-open, and after-close marks use the most
recent session. Multi-session results do **not** model splits, dividends,
ticker changes, short borrow/availability and fees, dividends owed by shorts,
halts/LULD, auctions, or quote corrections.

Accounting uses decimal text and cash-flow logic. Configured commissions apply
per fill. Sell fees include FINRA's published 2025-2029 equity TAF schedule and
the SEC Section 31 rate (zero through 2026-04-03; 0.00002060 thereafter in the
verified 2026 period); dates outside explicit SEC ranges produce warnings. TAF
is calculated at the published per-share rate without an invented penny minimum
or per-fill rounding, capped per trade, and exempted when execution price is
below the applicable per-share rate.
Results expose commissions, SEC fees, and FINRA fees separately. Snapshots are immutable. SQLite uses WAL,
foreign keys, busy timeout, explicit short transactions, and one connection per
operation; network requests occur before write transactions.

The only HTTP destinations implemented are redirect-disabled `GET` requests to:

* `https://data.alpaca.markets/v2/stocks/quotes` (always `feed=sip`, one symbol)
* `https://paper-api.alpaca.markets/v2/calendar`

Authoritative references: [Alpaca historical stock
quotes](https://docs.alpaca.markets/reference/stockquotes), [historical stock
data](https://docs.alpaca.markets/docs/historical-stock-data-1), and [calendar
API](https://docs.alpaca.markets/reference/getcalendar-1).

## Tests

Core tests use only the standard library and fake providers, so MCP need not be
installed:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

No test uses credentials or network access.
