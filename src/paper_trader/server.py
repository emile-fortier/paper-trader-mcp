from __future__ import annotations

import os
import sys
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .db import ConflictError, Database
from .http import AlpacaHTTP
from .service import DomainError, Service

mcp = MCPServer(
    "paper-trader",
    version="0.1.0",
    instructions=(
        "Simulation-only US-equity paper trading; no tool can send a broker order. "
        "Workflow: record an entry intent, evaluate it after historical SIP data clears the 16-minute safety window, "
        "inspect or mark it later, optionally record an exit, then evaluate the exit. Use a new request_id for every "
        "new mutation or evaluation; reuse one only for an exact retry. DAY orders are regular-hours only and expire "
        "when submitted after the close or on a non-session date. Execution eligibility/fill time is submitted_at plus "
        "latency (or the regular-session open), while as_of is the later valuation/mark time. Explain outcome_reason and "
        "warnings; never infer a fill. Invalid requests are MCP tool errors and do not mutate stored state."
    ),
)

RequestId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=200,
        description="Caller-generated idempotency key. Reuse only to retry the exact same call; use a new value otherwise.",
    ),
]
TradeId = Annotated[str, Field(description="Trade UUID returned by paper_trade_record.")]
Symbol = Annotated[str, Field(description="Uppercase US equity ticker, for example AAPL or BRK.B.")]
Quantity = Annotated[int, Field(gt=0, description="Positive whole-share quantity; fractional shares are unsupported.")]
Latency = Annotated[
    int,
    Field(
        ge=0,
        le=10_000,
        description=(
            "Assumed order-to-market latency in milliseconds; defaults to 250 ms. Combined with submitted_at "
            "(or the session open for pre-open orders) to determine execution eligibility and the stored fill time."
        ),
    ),
]
Timestamp = Annotated[
    str,
    Field(description="RFC3339 timestamp with timezone. Caller-supplied timestamps require backtest=true."),
]
AsOf = Annotated[
    str,
    Field(
        description=(
            "RFC3339 valuation timestamp. If omitted, the server explicitly uses current UTC time minus 16 minutes "
            "to stay behind Alpaca Basic's historical SIP embargo. For a pending order, the fill is tested at its "
            "earlier eligibility time; an open position is marked at this as_of time."
        )
    ),
]
LimitPrice = Annotated[
    str,
    Field(
        description=(
            "Positive decimal price. Required when order_type='limit' and forbidden when order_type='market'. "
            "Only immediately marketable limits can receive estimated fills."
        )
    ),
]


class MissingMarketDataProvider:
    def calendar(self, start: str, end: str):
        raise DomainError("Alpaca data credentials are required to evaluate a trade")

    def quotes(self, symbol: str, start: str, end: str):
        raise DomainError("Alpaca data credentials are required to evaluate a trade")


def service() -> Service:
    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    provider = AlpacaHTTP(key, secret) if key and secret else MissingMarketDataProvider()
    return Service(Database(), provider)


def call(method: str, **kwargs) -> dict[str, object]:
    try: return getattr(service(), method)(**kwargs)
    except (DomainError, ConflictError, ValueError, RuntimeError) as e: raise ToolError(str(e)) from e


@mcp.tool(
    description=(
        "Durably record a simulation-only whole-share US-equity DAY entry intent; never sends a live or broker-paper "
        "order. Without submitted_at, the server timestamps receipt. Historical submitted_at requires backtest=true. "
        "Regular hours only: pre-open DAY orders wait for the open; after-close or non-session DAY orders later expire."
    )
)
def paper_trade_record(request_id: RequestId, symbol: Symbol, position_side: Literal["long", "short"], quantity: Quantity,
                       order_type: Literal["market", "limit"], limit_price: LimitPrice | None = None,
                       time_in_force: Literal["day"] = "day", latency_ms: Latency = 250,
                       submitted_at: Timestamp | None = None, backtest: bool = False) -> dict[str, object]:
    return call("record", **locals())


@mcp.tool(
    description=(
        "Read one durable simulated trade with native structured intents, fills, itemized fees, raw selected quotes, "
        "and immutable evaluation snapshots. No market-data or broker call is made."
    )
)
def paper_trade_get(trade_id: TradeId) -> dict[str, object]:
    return call("get", trade_id=trade_id)


@mcp.tool(description="List durable simulated trades, optionally filtered by state. No market-data or broker call is made.")
def paper_trade_list(
    limit: Annotated[int, Field(ge=1, le=200, description="Maximum rows to return.")] = 50,
    state: Literal["pending_data", "open", "exit_pending", "closed", "expired", "indeterminate"] | None = None,
) -> dict[str, object]:
    return call("list", limit=limit, state=state)


@mcp.tool(
    description=(
        "Evaluate a pending simulated entry/exit or mark an open position using delayed historical consolidated SIP "
        "top-of-book quotes; no order is sent. Buys use ask, sells use bid, latency and displayed size are enforced, "
        "and fees are itemized. A fill's execution time is its submitted_at plus latency (or session open); quote_as_of "
        "identifies the latest qualifying quote used at or before that instant. For entries, a separate position mark "
        "uses as_of. If as_of is omitted, it means now minus 16 minutes—not latest/live data. Read outcome_reason, "
        "state_after, next_action, and warnings; never infer a fill from an indeterminate outcome."
    )
)
def paper_trade_evaluate(request_id: RequestId, trade_id: TradeId, as_of: AsOf | None = None) -> dict[str, object]:
    return call("evaluate", request_id=request_id, trade_id=trade_id, as_of=as_of)


@mcp.tool(
    description=(
        "Durably record a simulation-only market DAY exit intent for an open trade; never sends a live or broker-paper "
        "order and does not close immediately. Evaluate later, after the 16-minute SIP safety window, to estimate fill."
    )
)
def paper_trade_close(request_id: RequestId, trade_id: TradeId, submitted_at: Timestamp | None = None,
                      backtest: bool = False, latency_ms: Latency = 250) -> dict[str, object]:
    return call("close", **locals())


def main() -> None:
    print("paper-trader MCP starting on stdio; simulation only", file=sys.stderr)
    mcp.run()


if __name__ == "__main__": main()
