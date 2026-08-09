from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, position_side TEXT NOT NULL,
 quantity TEXT NOT NULL, state TEXT NOT NULL, recorded_at TEXT NOT NULL,
 entry_price TEXT, entry_fees TEXT, exit_price TEXT, exit_fees TEXT
);
CREATE TABLE IF NOT EXISTS order_intents (
 id TEXT PRIMARY KEY, trade_id TEXT NOT NULL REFERENCES trades(id), kind TEXT NOT NULL,
 submitted_at TEXT NOT NULL, order_type TEXT NOT NULL, limit_price TEXT,
 time_in_force TEXT NOT NULL, latency_ms INTEGER NOT NULL, status TEXT NOT NULL
);
DROP INDEX IF EXISTS one_kind_per_trade;
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_kind_per_trade
 ON order_intents(trade_id, kind) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS fills (
 id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL UNIQUE REFERENCES order_intents(id),
 filled_at_raw TEXT NOT NULL, filled_at_ns INTEGER NOT NULL, price TEXT NOT NULL,
 quantity TEXT NOT NULL, fees TEXT NOT NULL, fee_breakdown_json TEXT,
 quote_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_snapshots (
 id TEXT PRIMARY KEY, trade_id TEXT NOT NULL REFERENCES trades(id), created_at TEXT NOT NULL,
 as_of TEXT NOT NULL, outcome TEXT NOT NULL, query_start TEXT, query_end TEXT,
 feed TEXT NOT NULL, retrieved_at TEXT NOT NULL, policy_version TEXT NOT NULL,
 selected_quote_json TEXT, calculations_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
 outcome_reason TEXT
);
CREATE TABLE IF NOT EXISTS idempotency (
 request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, input_json TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class ConflictError(Exception): pass


class Database:
    def __init__(self, path: str | None = None):
        default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "paper-trader/paper-trader.sqlite3"
        self.path = Path(path or os.environ.get("PAPER_TRADER_DB", default)).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript(SCHEMA)
            fill_columns = {row["name"] for row in c.execute("PRAGMA table_info(fills)")}
            if "fee_breakdown_json" not in fill_columns:
                c.execute("ALTER TABLE fills ADD COLUMN fee_breakdown_json TEXT")
            snapshot_columns = {row["name"] for row in c.execute("PRAGMA table_info(evaluation_snapshots)")}
            if "outcome_reason" not in snapshot_columns:
                c.execute("ALTER TABLE evaluation_snapshots ADD COLUMN outcome_reason TEXT")
            c.commit()

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=5000")
        try: yield c
        finally: c.close()

    @contextmanager
    def transaction(self):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            try: yield c; c.commit()
            except Exception: c.rollback(); raise

    def replay(self, request_id: str, operation: str, input_json: str) -> dict | None:
        with self.connection() as c: row = c.execute("SELECT * FROM idempotency WHERE request_id=?", (request_id,)).fetchone()
        if not row: return None
        if row["operation"] != operation or row["input_json"] != input_json:
            raise ConflictError("request_id was already used with different input")
        return json.loads(row["result_json"])

    def trade(self, trade_id: str) -> dict | None:
        with self.connection() as c:
            t = c.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if not t: return None
            intents = c.execute("SELECT * FROM order_intents WHERE trade_id=? ORDER BY kind", (trade_id,)).fetchall()
            fills = c.execute(
                "SELECT fills.* FROM fills JOIN order_intents ON fills.intent_id=order_intents.id "
                "WHERE order_intents.trade_id=? ORDER BY fills.id",
                (trade_id,),
            ).fetchall()
            snaps = c.execute("SELECT * FROM evaluation_snapshots WHERE trade_id=? ORDER BY created_at", (trade_id,)).fetchall()
        out = dict(t)
        out["intents"] = [dict(x) for x in intents]
        out["fills"] = []
        for row in fills:
            fill = dict(row)
            fill["fee_breakdown"] = json.loads(fill.pop("fee_breakdown_json")) if fill.get("fee_breakdown_json") else None
            fill["quote"] = json.loads(fill.pop("quote_json"))
            out["fills"].append(fill)
        out["evaluations"] = []
        for row in snaps:
            snapshot = dict(row)
            raw_quotes = snapshot.pop("selected_quote_json")
            snapshot["selected_quotes"] = json.loads(raw_quotes) if raw_quotes else None
            snapshot["calculations"] = json.loads(snapshot.pop("calculations_json"))
            snapshot["warnings"] = json.loads(snapshot.pop("warnings_json"))
            out["evaluations"].append(snapshot)
        return out

    def list(self, limit: int, state: str | None) -> list[dict]:
        sql, args = "SELECT * FROM trades", []
        if state: sql += " WHERE state=?"; args.append(state)
        sql += " ORDER BY recorded_at DESC LIMIT ?"; args.append(limit)
        with self.connection() as c: return [dict(x) for x in c.execute(sql, args)]


def _json_default(item):
    if isinstance(item, Decimal):
        return str(item)
    raise TypeError(f"cannot serialize {type(item).__name__}")


def canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    )
