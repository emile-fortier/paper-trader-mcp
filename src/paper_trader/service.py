from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .db import Database, canonical
from .fees import commission_breakdown, sale_fee_breakdown
from .market import Quote, Session, eligibility, parse_session, select_quote
from .timeutil import parse_instant, rfc3339, safe_cutoff

BASE = {"simulation": True, "live_order_sent": False}
POLICY_VERSION = "top-of-book-v1"
NY = ZoneInfo("America/New_York")
WARN = (
    "Simulation only: historical SIP top-of-book quotes are not execution reports; "
    "no L2 depth, queue position, price improvement, or partial fills are modeled."
)
MULTI = (
    "Corporate actions (splits/dividends/ticker changes), borrow fees/availability, "
    "dividends owed on shorts, halts/LULD, auctions, and later quote corrections are not modeled."
)


class DomainError(Exception):
    pass


class ConcurrentStateError(Exception):
    pass


class PendingData(Exception):
    def __init__(self, earliest_retry_at: datetime):
        self.earliest_retry_at = earliest_retry_at


def _positive_decimal(value: str, name: str) -> Decimal:
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise DomainError(f"{name} must be decimal text") from exc
    if not number.is_finite() or number <= 0:
        raise DomainError(f"{name} must be a positive finite decimal")
    return number


class Service:
    def __init__(self, db: Database, provider, now=lambda: datetime.now(UTC)):
        self.db, self.provider, self.now = db, provider, now
        self.lookback = timedelta(seconds=int(os.getenv("PAPER_TRADER_LOOKBACK_SECONDS", "120")))
        self.stale = timedelta(seconds=int(os.getenv("PAPER_TRADER_MAX_STALENESS_SECONDS", "30")))
        if not timedelta(seconds=1) <= self.lookback <= timedelta(minutes=10):
            raise ValueError("lookback must be 1..600 seconds")
        if not timedelta(seconds=1) <= self.stale <= self.lookback:
            raise ValueError("staleness must be positive and <= lookback")
        try:
            self.per_order = Decimal(os.getenv("PAPER_TRADER_COMMISSION_PER_ORDER", "0"))
            self.per_share = Decimal(os.getenv("PAPER_TRADER_COMMISSION_PER_SHARE", "0"))
        except InvalidOperation as exc:
            raise ValueError("commission settings must be decimal text") from exc
        if any(not value.is_finite() or value < 0 for value in (self.per_order, self.per_share)):
            raise ValueError("commission settings must be finite and non-negative")

    def record(
        self,
        request_id: str,
        symbol: str,
        position_side: str,
        quantity: int,
        order_type: str,
        limit_price: str | None = None,
        time_in_force: str = "day",
        latency_ms: int = 250,
        submitted_at: str | None = None,
        backtest: bool = False,
    ) -> dict:
        if not request_id or len(request_id) > 200:
            raise DomainError("request_id must be 1..200 characters")
        symbol = symbol.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", symbol):
            raise DomainError("invalid US equity symbol")
        if position_side not in ("long", "short") or isinstance(quantity, bool) or quantity <= 0:
            raise DomainError("position_side must be long|short and quantity a positive whole share count")
        if order_type not in ("market", "limit") or time_in_force != "day":
            raise DomainError("only market or limit DAY orders are supported")
        if isinstance(latency_ms, bool) or not 0 <= latency_ms <= 10_000:
            raise DomainError("latency_ms must be 0..10000")
        if (order_type == "limit") != (limit_price is not None):
            raise DomainError("limit_price is required only for limit orders")
        if limit_price is not None:
            limit_price = str(_positive_decimal(limit_price, "limit_price"))
        if submitted_at and not backtest:
            raise DomainError("submitted_at is allowed only when backtest=true")
        submitted_input = rfc3339(parse_instant(submitted_at).dt) if submitted_at else None
        payload = {
            "request_id": request_id,
            "symbol": symbol,
            "position_side": position_side,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
            "time_in_force": time_in_force,
            "latency_ms": latency_ms,
            "submitted_at": submitted_input,
            "backtest": backtest,
        }
        key = canonical(payload)
        replay = self.db.replay(request_id, "record", key)
        if replay:
            return replay

        submitted = parse_instant(submitted_input).dt if submitted_input else self.now()
        trade_id, intent_id = str(uuid.uuid4()), str(uuid.uuid4())
        recorded = rfc3339(self.now())
        result = BASE | {
            "trade_id": trade_id,
            "state": "pending_data",
            "submitted_at": rfc3339(submitted),
            "order": {
                "symbol": symbol,
                "position_side": position_side,
                "quantity": str(quantity),
                "order_type": order_type,
                "limit_price": limit_price,
                "time_in_force": time_in_force,
                "latency_ms": latency_ms,
                "backtest": backtest,
            },
            "message": WARN,
            "next_action": (
                "Call paper_trade_evaluate with a new request_id. Recent prospective orders normally remain pending "
                "until their execution time is at least 16 minutes old."
            ),
        }
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (trade_id, symbol, position_side, str(quantity), "pending_data", recorded, None, None, None, None),
                )
                connection.execute(
                    "INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)",
                    (intent_id, trade_id, "entry", rfc3339(submitted), order_type, limit_price, "day", latency_ms, "pending"),
                )
                connection.execute(
                    "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                    (request_id, "record", key, canonical(result), recorded),
                )
        except sqlite3.IntegrityError:
            replay = self.db.replay(request_id, "record", key)
            if replay:
                return replay
            raise
        return result

    def get(self, trade_id: str) -> dict:
        trade = self.db.trade(trade_id)
        if not trade:
            raise DomainError("trade not found")
        explanations = {
            "pending_data": "An entry intent exists but has not produced an estimated fill.",
            "open": "The simulated entry filled and the position has no pending exit.",
            "exit_pending": "A simulated exit intent exists but has not produced an estimated fill.",
            "closed": "Both simulated entry and exit fills are stored.",
            "expired": "The DAY entry intent was ineligible for a regular-session fill.",
            "indeterminate": "The model could not justify a fill from available top-of-book data and policy.",
        }
        financial_summary = None
        if trade["entry_price"] is not None:
            quantity = Decimal(trade["quantity"])
            entry_price = Decimal(trade["entry_price"])
            entry_fees = Decimal(trade["entry_fees"])
            financial_summary = {
                "entry_notional": str(entry_price * quantity),
                "entry_fees": str(entry_fees),
            }
            if trade["exit_price"] is not None:
                exit_price = Decimal(trade["exit_price"])
                exit_fees = Decimal(trade["exit_fees"])
                direction = Decimal(1) if trade["position_side"] == "long" else Decimal(-1)
                gross = (exit_price - entry_price) * quantity * direction
                total_fees = entry_fees + exit_fees
                financial_summary.update(
                    {
                        "exit_notional": str(exit_price * quantity),
                        "exit_fees": str(exit_fees),
                        "total_fees": str(total_fees),
                        "gross_realized_pnl": str(gross),
                        "net_realized_pnl": str(gross - total_fees),
                    }
                )
        return BASE | {
            "trade": trade,
            "state_explanation": explanations.get(trade["state"], "Unknown state."),
            "financial_summary": financial_summary,
        }

    def list(self, limit: int = 50, state: str | None = None) -> dict:
        if isinstance(limit, bool) or not 1 <= limit <= 200:
            raise DomainError("limit must be 1..200")
        allowed_states = {"pending_data", "open", "exit_pending", "closed", "expired", "indeterminate"}
        if state is not None and state not in allowed_states:
            raise DomainError("invalid trade state filter")
        trades = self.db.list(limit, state)
        return BASE | {"trades": trades, "count": len(trades), "state_filter": state}

    def _sessions(self, around: datetime) -> list[Session]:
        rows = self.provider.calendar(
            (around.date() - timedelta(days=10)).isoformat(),
            (around.date() + timedelta(days=1)).isoformat(),
        )
        return sorted((parse_session(row) for row in rows), key=lambda session: session.open)

    @staticmethod
    def _session_for_order(sessions: list[Session], submitted: datetime) -> Session | None:
        local_day = submitted.astimezone(NY).date()
        return next((session for session in sessions if session.day == local_day), None)

    @staticmethod
    def _mark_session(sessions: list[Session], at: datetime) -> tuple[Session, datetime]:
        prior = [session for session in sessions if session.open <= at]
        if not prior:
            raise DomainError("no prior market session available for this timestamp")
        session = prior[-1]
        return session, min(at, session.close)

    def _quote(self, symbol: str, at: datetime, session: Session, now: datetime) -> tuple[Quote | None, dict]:
        if at > safe_cutoff(now):
            raise PendingData(at + timedelta(minutes=16))
        start = rfc3339(max(session.open, at - self.lookback))
        end = rfc3339(at)
        rows = self.provider.quotes(symbol, start, end)
        quotes = [Quote.from_api(row, ordinal) for ordinal, row in enumerate(rows)]
        quote = select_quote(quotes, at, session, self.lookback, self.stale)
        return quote, {"start": start, "end": end, "feed": "sip"}

    def _attempt_intent(self, trade: dict, intent: dict, as_of: datetime, now: datetime) -> dict:
        submitted = parse_instant(intent["submitted_at"]).dt
        sessions = self._sessions(submitted)
        session = self._session_for_order(sessions, submitted)
        if not session:
            return {
                "outcome": "expired",
                "reason": "no_regular_market_session_on_submission_date",
                "eligible_at": None,
                "quote": None,
                "query": None,
            }
        eligible_at = eligibility(submitted, session, intent["latency_ms"])
        if not eligible_at:
            return {
                "outcome": "expired",
                "reason": "day_order_submitted_after_regular_session_close",
                "eligible_at": None,
                "quote": None,
                "query": None,
            }
        if eligible_at > session.close:
            return {
                "outcome": "expired",
                "reason": "configured_latency_extended_past_regular_session_close",
                "eligible_at": eligible_at,
                "quote": None,
                "query": None,
            }
        if as_of < eligible_at:
            return {
                "outcome": "pending_data",
                "reason": "evaluation_as_of_precedes_order_eligibility",
                "eligible_at": eligible_at,
                "quote": None,
                "query": None,
                "earliest_retry_at": eligible_at + timedelta(minutes=16),
            }
        try:
            quote, query = self._quote(trade["symbol"], eligible_at, session, now)
        except PendingData as pending:
            return {
                "outcome": "pending_data",
                "reason": "historical_sip_data_is_inside_the_16_minute_safety_window",
                "eligible_at": eligible_at,
                "quote": None,
                "query": None,
                "earliest_retry_at": pending.earliest_retry_at,
            }
        if not quote:
            return {
                "outcome": "indeterminate_no_usable_quote",
                "reason": "no_complete_fresh_non_crossed_sip_quote_in_the_configured_window",
                "eligible_at": eligible_at,
                "quote": None,
                "query": query,
            }

        buy = (intent["kind"] == "entry" and trade["position_side"] == "long") or (
            intent["kind"] == "exit" and trade["position_side"] == "short"
        )
        price, displayed_size = (quote.ask, quote.ask_size) if buy else (quote.bid, quote.bid_size)
        if intent["order_type"] == "limit":
            limit = Decimal(intent["limit_price"])
            if (buy and price > limit) or (not buy and price < limit):
                return {
                    "outcome": "indeterminate_resting_limit_unsupported",
                    "reason": "limit_was_not_immediately_marketable_and_queue_position_cannot_be_reconstructed",
                    "eligible_at": eligible_at,
                    "quote": quote,
                    "query": query,
                }
        if Decimal(trade["quantity"]) > displayed_size:
            return {
                "outcome": "indeterminate_due_to_depth",
                "reason": "quantity_exceeded_displayed_top_of_book_size",
                "eligible_at": eligible_at,
                "quote": quote,
                "query": query,
                "displayed_size": displayed_size,
            }

        fee_components = commission_breakdown(int(trade["quantity"]), self.per_order, self.per_share)
        fee_warnings: list[str] = []
        if not buy:
            regulatory, fee_warnings = sale_fee_breakdown(
                price * Decimal(trade["quantity"]), int(trade["quantity"]), eligible_at.date()
            )
            fee_components.update(regulatory)
        fees = sum(fee_components.values(), Decimal(0))
        return {
            "outcome": "filled_entry" if intent["kind"] == "entry" else "filled_exit",
            "reason": "estimated_fill_at_complete_historical_sip_top_of_book",
            "eligible_at": eligible_at,
            "quote": quote,
            "query": query,
            "price": price,
            "fees": fees,
            "fee_breakdown": fee_components,
            "side": "buy" if buy else "sell",
            "warnings": fee_warnings,
        }

    def _mark(self, trade: dict, at: datetime, now: datetime, entry_price: Decimal, entry_fees: Decimal) -> dict:
        session, mark_at = self._mark_session(self._sessions(at), at)
        try:
            quote, query = self._quote(trade["symbol"], mark_at, session, now)
        except PendingData as pending:
            return {
                "outcome": "pending_data",
                "reason": "requested_mark_is_inside_the_16_minute_safety_window",
                "earliest_retry_at": pending.earliest_retry_at,
                "quote": None,
                "query": None,
            }
        if not quote:
            return {
                "outcome": "indeterminate_no_usable_quote",
                "reason": "no_complete_fresh_non_crossed_sip_quote_for_mark",
                "quote": None,
                "query": query,
            }
        quantity = Decimal(trade["quantity"])
        mark = quote.bid if trade["position_side"] == "long" else quote.ask
        direction = Decimal(1) if trade["position_side"] == "long" else Decimal(-1)
        gross = (mark - entry_price) * quantity * direction
        liquidation_breakdown = commission_breakdown(int(quantity), self.per_order, self.per_share)
        fee_warnings: list[str] = []
        if trade["position_side"] == "long":
            regulatory, fee_warnings = sale_fee_breakdown(mark * quantity, int(quantity), mark_at.date())
            liquidation_breakdown.update(regulatory)
        liquidation_fees = sum(liquidation_breakdown.values(), Decimal(0))
        calculations = {
            "mark_price": str(mark),
            "gross_pnl": str(gross),
            "fees_incurred": str(entry_fees),
            "estimated_liquidation_fees": str(liquidation_fees),
            "estimated_liquidation_fee_breakdown": {key: str(value) for key, value in liquidation_breakdown.items()},
            "net_liquidation_pnl": str(gross - entry_fees - liquidation_fees),
            "mark_as_of": rfc3339(mark_at),
            "displayed_liquidity_covers_quantity": quantity <= (quote.bid_size if trade["position_side"] == "long" else quote.ask_size),
        }
        return {
            "outcome": "marked_open",
            "reason": "position_marked_at_historical_liquidation_side_of_sip_quote",
            "quote": quote,
            "query": query,
            "calculations": calculations,
            "warnings": fee_warnings,
        }

    def evaluate(self, request_id: str, trade_id: str, as_of: str | None = None) -> dict:
        if not request_id or len(request_id) > 200:
            raise DomainError("request_id must be 1..200 characters")
        as_of_input = rfc3339(parse_instant(as_of).dt) if as_of else None
        input_json = canonical({"trade_id": trade_id, "as_of": as_of_input})
        replay = self.db.replay(request_id, "evaluate", input_json)
        if replay:
            return replay
        now = self.now()
        chosen = parse_instant(as_of_input).dt if as_of_input else safe_cutoff(now)
        as_of_policy = "caller_supplied" if as_of else "default_now_minus_16_minutes"
        trade = self.db.trade(trade_id)
        if not trade:
            raise DomainError("trade not found")

        entry_intent = next((intent for intent in trade["intents"] if intent["kind"] == "entry"), None)
        entry_fill = next((fill for fill in trade["fills"] if entry_intent and fill["intent_id"] == entry_intent["id"]), None)
        exit_intent_ids = {intent["id"] for intent in trade["intents"] if intent["kind"] == "exit"}
        exit_fill = next((fill for fill in trade["fills"] if fill["intent_id"] in exit_intent_ids), None)
        if entry_fill and chosen < parse_instant(entry_fill["filled_at_raw"]).dt:
            raise DomainError("as_of precedes the stored entry execution time")
        if exit_fill and chosen < parse_instant(exit_fill["filled_at_raw"]).dt:
            raise DomainError("as_of predates the stored exit; historical pre-close valuation is not supported after close")

        pending_intent = next((intent for intent in trade["intents"] if intent["status"] == "pending"), None)
        warnings = [WARN, MULTI]
        calculations: dict = {}
        selected_quotes: dict = {}
        queries: list[dict] = []
        attempt = None

        if pending_intent:
            attempt = self._attempt_intent(trade, pending_intent, chosen, now)
            outcome = attempt["outcome"]
            outcome_reason = attempt["reason"]
            if attempt.get("eligible_at"):
                calculations["eligible_at"] = rfc3339(attempt["eligible_at"])
            if attempt.get("query"):
                queries.append({"purpose": f"{pending_intent['kind']}_fill", **attempt["query"]})
            if attempt.get("quote"):
                selected_quotes["fill"] = attempt["quote"].raw
            warnings.extend(attempt.get("warnings", []))
            if outcome == "pending_data":
                calculations["earliest_retry_at"] = rfc3339(attempt["earliest_retry_at"])
            elif outcome.startswith("filled_"):
                calculations["fill"] = {
                    "price": str(attempt["price"]),
                    "fees": str(attempt["fees"]),
                    "fee_breakdown": {key: str(value) for key, value in attempt["fee_breakdown"].items()},
                    "side": attempt["side"],
                    "filled_at": rfc3339(attempt["eligible_at"]),
                    "quote_as_of": attempt["quote"].timestamp.raw,
                    "quantity": trade["quantity"],
                    "displayed_size": str(
                        attempt["quote"].ask_size if attempt["side"] == "buy" else attempt["quote"].bid_size
                    ),
                    "displayed_liquidity_covers_quantity": True,
                    "slippage_beyond_spread": "0",
                }
                if pending_intent["kind"] == "entry" and chosen >= attempt["eligible_at"]:
                    mark = self._mark(trade, chosen, now, attempt["price"], attempt["fees"])
                    if mark.get("query"):
                        queries.append({"purpose": "position_mark", **mark["query"]})
                    if mark.get("quote"):
                        selected_quotes["mark"] = mark["quote"].raw
                    warnings.extend(mark.get("warnings", []))
                    if mark["outcome"] == "marked_open":
                        calculations["position"] = mark["calculations"]
                        outcome = "filled_entry_and_marked"
                        outcome_reason = "entry_filled_then_position_marked_at_requested_as_of"
                    else:
                        calculations["mark"] = {
                            "outcome": mark["outcome"],
                            "outcome_reason": mark["reason"],
                        }
                        if mark.get("earliest_retry_at"):
                            calculations["mark"]["earliest_retry_at"] = rfc3339(mark["earliest_retry_at"])
                        outcome = f"filled_entry_mark_{mark['outcome']}"
                        outcome_reason = f"entry_filled_but_mark_failed: {mark['reason']}"
                elif pending_intent["kind"] == "exit":
                    quantity = Decimal(trade["quantity"])
                    direction = Decimal(1) if trade["position_side"] == "long" else Decimal(-1)
                    gross = (attempt["price"] - Decimal(trade["entry_price"])) * quantity * direction
                    total_fees = Decimal(trade["entry_fees"]) + attempt["fees"]
                    calculations["realized"] = {
                        "gross_pnl": str(gross),
                        "entry_fees": trade["entry_fees"],
                        "exit_fees": str(attempt["fees"]),
                        "net_realized_pnl": str(gross - total_fees),
                    }
        elif trade["state"] in {"open", "exit_pending"}:
            mark = self._mark(trade, chosen, now, Decimal(trade["entry_price"]), Decimal(trade["entry_fees"]))
            outcome = mark["outcome"]
            outcome_reason = mark["reason"]
            if mark.get("query"):
                queries.append({"purpose": "position_mark", **mark["query"]})
            if mark.get("quote"):
                selected_quotes["mark"] = mark["quote"].raw
            calculations.update(mark.get("calculations", {}))
            warnings.extend(mark.get("warnings", []))
            if outcome == "pending_data":
                calculations["earliest_retry_at"] = rfc3339(mark["earliest_retry_at"])
        elif trade["state"] == "closed":
            quantity = Decimal(trade["quantity"])
            direction = Decimal(1) if trade["position_side"] == "long" else Decimal(-1)
            gross = (Decimal(trade["exit_price"]) - Decimal(trade["entry_price"])) * quantity * direction
            calculations = {
                "gross_pnl": str(gross),
                "entry_fees": trade["entry_fees"],
                "exit_fees": trade["exit_fees"],
                "net_realized_pnl": str(gross - Decimal(trade["entry_fees"]) - Decimal(trade["exit_fees"])),
            }
            outcome = "closed"
            outcome_reason = "trade_was_already_closed_using_stored_entry_and_exit_fills"
        else:
            outcome = trade["state"]
            outcome_reason = "trade_was_already_in_this_terminal_state"

        created = rfc3339(now)
        snapshot_id = str(uuid.uuid4())
        calculations["queries"] = queries
        calculations["outcome_reason"] = outcome_reason
        if pending_intent and attempt and attempt.get("displayed_size") is not None:
            calculations["displayed_size"] = str(attempt["displayed_size"])
        if pending_intent and attempt and attempt["outcome"].startswith("filled_"):
            state_after = "open" if pending_intent["kind"] == "entry" else "closed"
        elif pending_intent and attempt and attempt["outcome"] == "expired":
            state_after = "expired" if pending_intent["kind"] == "entry" else "open"
        elif pending_intent and attempt and attempt["outcome"].startswith("indeterminate_"):
            state_after = "indeterminate" if pending_intent["kind"] == "entry" else "open"
        else:
            state_after = trade["state"]
        if outcome in {"pending_data", "filled_entry_mark_pending_data"}:
            next_action = "Wait until earliest_retry_at, then evaluate again with a new request_id."
        elif state_after == "open":
            next_action = "Use paper_trade_close to record a simulated exit, or evaluate later with a new request_id."
        elif state_after == "closed":
            next_action = "Use paper_trade_get to inspect the immutable completed lifecycle."
        elif state_after == "expired":
            next_action = "Record a new DAY order during or before a regular market session."
        elif state_after == "indeterminate":
            next_action = "Inspect outcome_reason; no fill was stored. Record a new order if you want a different simulation."
        else:
            next_action = "Inspect outcome_reason and the stored trade before deciding whether to retry."
        result = BASE | {
            "trade_id": trade_id,
            "snapshot_id": snapshot_id,
            "as_of": rfc3339(chosen),
            "as_of_policy": as_of_policy,
            "outcome": outcome,
            "outcome_reason": outcome_reason,
            "state_after": state_after,
            "next_action": next_action,
            "calculations": calculations,
            "warnings": list(dict.fromkeys(warnings)),
        }
        last_query = queries[-1] if queries else {}
        try:
            with self.db.transaction() as connection:
                state_change = pending_intent and attempt and attempt["outcome"] != "pending_data"
                if state_change:
                    status = (
                        "filled" if attempt["outcome"].startswith("filled_")
                        else "expired" if attempt["outcome"] == "expired"
                        else "indeterminate"
                    )
                    claimed = connection.execute(
                        "UPDATE order_intents SET status=? WHERE id=? AND status='pending'",
                        (status, pending_intent["id"]),
                    )
                    if claimed.rowcount != 1:
                        raise ConcurrentStateError("order intent was already evaluated")
                    expected_state = "pending_data" if pending_intent["kind"] == "entry" else "exit_pending"
                    changed = connection.execute(
                        "UPDATE trades SET state=? WHERE id=? AND state=?",
                        (state_after, trade_id, expected_state),
                    )
                    if changed.rowcount != 1:
                        raise ConcurrentStateError("trade state changed during evaluation")

                connection.execute(
                    "INSERT INTO evaluation_snapshots "
                    "(id,trade_id,created_at,as_of,outcome,query_start,query_end,feed,retrieved_at,policy_version,"
                    "selected_quote_json,calculations_json,warnings_json,outcome_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id, trade_id, created, rfc3339(chosen), outcome,
                        last_query.get("start"), last_query.get("end"), "sip", created, POLICY_VERSION,
                        canonical(selected_quotes) if selected_quotes else None,
                        canonical(calculations), canonical(result["warnings"]), outcome_reason,
                    ),
                )
                if pending_intent and attempt and attempt["outcome"].startswith("filled_"):
                    quote = attempt["quote"]
                    connection.execute(
                        "INSERT INTO fills(intent_id,filled_at_raw,filled_at_ns,price,quantity,fees,fee_breakdown_json,quote_json) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            pending_intent["id"], rfc3339(attempt["eligible_at"]),
                            parse_instant(rfc3339(attempt["eligible_at"])).ns,
                            str(attempt["price"]), trade["quantity"], str(attempt["fees"]),
                            canonical({key: str(value) for key, value in attempt["fee_breakdown"].items()}),
                            canonical(quote.raw),
                        ),
                    )
                    if pending_intent["kind"] == "entry":
                        connection.execute(
                            "UPDATE trades SET entry_price=?, entry_fees=? WHERE id=?",
                            (str(attempt["price"]), str(attempt["fees"]), trade_id),
                        )
                    else:
                        connection.execute(
                            "UPDATE trades SET exit_price=?, exit_fees=? WHERE id=?",
                            (str(attempt["price"]), str(attempt["fees"]), trade_id),
                        )
                connection.execute(
                    "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                    (request_id, "evaluate", input_json, canonical(result), created),
                )
        except (ConcurrentStateError, sqlite3.IntegrityError):
            replay = self.db.replay(request_id, "evaluate", input_json)
            if replay:
                return replay
            raise DomainError("trade changed concurrently; retrieve it and evaluate again with a new request_id")
        return result

    def close(
        self,
        request_id: str,
        trade_id: str,
        submitted_at: str | None = None,
        backtest: bool = False,
        latency_ms: int = 250,
    ) -> dict:
        if not request_id or len(request_id) > 200:
            raise DomainError("request_id must be 1..200 characters")
        if submitted_at and not backtest:
            raise DomainError("submitted_at is allowed only when backtest=true")
        if isinstance(latency_ms, bool) or not 0 <= latency_ms <= 10_000:
            raise DomainError("latency_ms must be 0..10000")
        submitted_input = rfc3339(parse_instant(submitted_at).dt) if submitted_at else None
        input_json = canonical(
            {"trade_id": trade_id, "submitted_at": submitted_input, "backtest": backtest, "latency_ms": latency_ms}
        )
        replay = self.db.replay(request_id, "close", input_json)
        if replay:
            return replay
        submitted = parse_instant(submitted_input).dt if submitted_input else self.now()
        trade = self.db.trade(trade_id)
        if not trade or trade["state"] != "open":
            raise DomainError("trade must be open and have no pending exit")
        entry_intent = next(intent for intent in trade["intents"] if intent["kind"] == "entry")
        entry_fill = next(fill for fill in trade["fills"] if fill["intent_id"] == entry_intent["id"])
        if submitted < parse_instant(entry_fill["filled_at_raw"]).dt:
            raise DomainError("exit submitted_at must not precede the stored entry execution time")
        result = BASE | {
            "trade_id": trade_id,
            "state": "exit_pending",
            "submitted_at": rfc3339(submitted),
            "exit_order": {
                "order_type": "market",
                "time_in_force": "day",
                "latency_ms": latency_ms,
                "backtest": backtest,
            },
            "message": "Prospective simulated market DAY exit recorded; no broker order sent.",
            "next_action": (
                "Call paper_trade_evaluate with a new request_id after the exit execution time is at least 16 minutes old."
            ),
        }
        created = rfc3339(self.now())
        try:
            with self.db.transaction() as connection:
                changed = connection.execute(
                    "UPDATE trades SET state='exit_pending' WHERE id=? AND state='open'", (trade_id,)
                )
                if changed.rowcount != 1:
                    raise ConcurrentStateError("trade is no longer open")
                connection.execute(
                    "INSERT INTO order_intents VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), trade_id, "exit", rfc3339(submitted), "market", None, "day", latency_ms, "pending"),
                )
                connection.execute(
                    "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                    (request_id, "close", input_json, canonical(result), created),
                )
        except (ConcurrentStateError, sqlite3.IntegrityError):
            replay = self.db.replay(request_id, "close", input_json)
            if replay:
                return replay
            raise DomainError("trade changed concurrently; retrieve it before retrying")
        return result
