"""Deterministic MCP host fixture for model-driven integration tests.

This runs the production MCP tools, database, and simulation service while
replacing only Alpaca's network responses with fixed regular-session data.
"""

import tempfile
from datetime import UTC, datetime
from decimal import Decimal

from paper_trader import server
from paper_trader.db import Database
from paper_trader.service import Service


class FixtureProvider:
    def calendar(self, start: str, end: str) -> list[dict]:
        return [{"date": "2026-08-07", "open": "09:30", "close": "16:00"}]

    def quotes(self, symbol: str, start: str, end: str) -> list[dict]:
        later_mark = end >= "2026-08-07T18:00:00"
        return [
            {
                "t": end,
                "bp": Decimal("101.00" if later_mark else "99.90"),
                "ap": Decimal("101.10" if later_mark else "100.00"),
                "bs": Decimal("200"),
                "as": Decimal("150"),
                "bx": "Q",
                "ax": "N",
                "z": "C",
            }
        ]


_temporary_directory = tempfile.TemporaryDirectory(prefix="paper-trader-claude-fixture-")
_database_path = f"{_temporary_directory.name}/fixture.sqlite3"
_database = Database(_database_path)
_now = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)

server.service = lambda: Service(_database, FixtureProvider(), lambda: _now)


if __name__ == "__main__":
    server.main()
