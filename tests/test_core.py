import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from paper_trader.db import ConflictError, Database, canonical
from paper_trader.fees import sale_fee_breakdown, sale_fees
from paper_trader.http import AlpacaHTTP
from paper_trader.market import Quote, eligibility, parse_session, select_quote
from paper_trader.service import DomainError, Service
from paper_trader.timeutil import parse_instant, safe_cutoff


class FakeProvider:
    def __init__(self, quote_time=None, bid="99", ask="100", size="100"):
        self.quote_time, self.bid, self.ask, self.size = quote_time,bid,ask,size
    def calendar(self,start,end):
        return [
            {"date":"2026-06-01","open":"09:30","close":"13:00"}, # early close
            {"date":"2026-06-02","open":"09:30","close":"16:00"},
        ]
    def quotes(self,symbol,start,end):
        return [{"t":self.quote_time or end,"bp":Decimal(self.bid),"ap":Decimal(self.ask),
                 "bs":Decimal(self.size),"as":Decimal(self.size)}]


class TimeTests(unittest.TestCase):
    def test_nanoseconds_and_equal_order(self):
        a=parse_instant("2026-01-01T00:00:00.123456789Z")
        self.assertEqual(a.ns % 1_000_000_000,123456789)
        s=parse_session({"date":"2026-06-01","open":"09:30","close":"16:00"})
        rows=[Quote.from_api({"t":"2026-06-01T14:00:00.123456789Z","bp":1,"ap":2,"bs":1,"as":1},i) for i in range(2)]
        self.assertEqual(select_quote(rows,datetime(2026,6,1,14,0,1,tzinfo=UTC),s).ordinal,1)
    def test_embargo_edge(self):
        now=datetime(2026,1,1,tzinfo=UTC)
        self.assertEqual(safe_cutoff(now),now-timedelta(minutes=16))
    def test_offset_timestamp_preserves_nanoseconds(self):
        instant=parse_instant("2026-01-01T01:00:00.999999999+01:00")
        self.assertEqual(instant.ns,parse_instant("2026-01-01T00:00:00.999999999Z").ns)


class QuoteTests(unittest.TestCase):
    def setUp(self): self.s=parse_session({"date":"2026-06-01","open":"09:30","close":"13:00"})
    def q(self,bp="1",ap="2",bs="1",ass="1",t="2026-06-01T14:00:00Z"):
        return Quote.from_api({"t":t,"bp":bp,"ap":ap,"bs":bs,"as":ass})
    def test_usable_locked_crossed_zero_stale(self):
        at=datetime(2026,6,1,14,0,20,tzinfo=UTC)
        self.assertIsNotNone(select_quote([self.q("2","2")],at,self.s))
        self.assertIsNone(select_quote([self.q("3","2")],at,self.s))
        self.assertIsNone(select_quote([self.q(bs="0")],at,self.s))
        self.assertIsNone(select_quote([self.q(t="2026-06-01T13:59:00Z")],at,self.s))
    def test_calendar_early_close_and_non_session(self):
        self.assertEqual(self.s.close.hour,17)
        self.assertIsNone(eligibility(datetime(2026,6,1,18,tzinfo=UTC),self.s,250))


class FeeTests(unittest.TestCase):
    def test_effective_dates(self):
        self.assertEqual(sale_fees(Decimal("10000"),100,date(2026,4,3))[0],Decimal("0.019500"))
        self.assertEqual(sale_fees(Decimal("10000"),100,date(2026,4,4))[0],Decimal("0.229500"))
        self.assertTrue(sale_fees(Decimal("100"),1,date(2027,1,1))[1])
        self.assertEqual(sale_fees(Decimal("10000"),100,date(2025,12,1))[0],Decimal("0.016600"))
        breakdown,warnings=sale_fee_breakdown(Decimal("10000"),100,date(2027,1,1))
        self.assertEqual(breakdown["finra_taf"],Decimal("0.023200"))
        self.assertEqual(len(warnings),1)  # SEC rate is not yet verified for 2027.
        exempt,_=sale_fee_breakdown(Decimal("0.0001"),1,date(2026,6,1))
        self.assertEqual(exempt["finra_taf"],Decimal("0.00"))
        threshold,_=sale_fee_breakdown(Decimal("0.000195"),1,date(2026,6,1))
        self.assertEqual(threshold["finra_taf"],Decimal("0.000195"))
        thousand,_=sale_fee_breakdown(Decimal("10000"),1000,date(2026,6,1))
        self.assertEqual(thousand["finra_taf"],Decimal("0.195000"))
        capped,_=sale_fee_breakdown(Decimal("1000000"),100000,date(2026,6,1))
        self.assertEqual(capped["finra_taf"],Decimal("9.79"))


class HTTPTests(unittest.TestCase):
    def test_allowlist_and_repeated_page_token(self):
        client=AlpacaHTTP("key","secret")
        with self.assertRaises(ValueError): client.get("example.com","/v2/stocks/quotes",{})
        calls=0
        def fake_get(host,path,params):
            nonlocal calls
            calls+=1
            return {"quotes":{"AAPL":[]},"next_page_token":"same"}
        client.get=fake_get
        with self.assertRaisesRegex(RuntimeError,"repeated page token"):
            client.quotes("AAPL","start","end")
        self.assertEqual(calls,2)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=self.tmp.name+"/db.sqlite3"
        self.now=datetime(2026,6,2,18,tzinfo=UTC)
        self.s=Service(Database(self.path),FakeProvider(),lambda:self.now)
    def tearDown(self): self.tmp.cleanup()
    def record(self,**kw):
        base=dict(request_id="r1",symbol="AAPL",position_side="long",quantity=10,order_type="market",submitted_at="2026-06-01T14:00:00Z",backtest=True)
        return self.s.record(**(base|kw))
    def test_idempotency_durability_fill_mark_and_delayed_close(self):
        r=self.record(); self.assertEqual(r,self.record())
        with self.assertRaises(ConflictError): self.record(quantity=11)
        evaluation=self.s.evaluate("e1",r["trade_id"])
        self.assertEqual(evaluation["outcome"],"filled_entry_and_marked")
        self.assertEqual(evaluation["outcome_reason"],"entry_filled_then_position_marked_at_requested_as_of")
        self.assertEqual(evaluation["as_of_policy"],"default_now_minus_16_minutes")
        self.assertEqual(evaluation["state_after"],"open")
        self.assertEqual(evaluation["calculations"]["fill"]["fee_breakdown"]["commission_per_order"],"0")
        self.assertEqual(evaluation["calculations"]["position"]["net_liquidation_pnl"],"-10.031950")
        self.assertEqual(Database(self.path).trade(r["trade_id"])["state"],"open")
        c=self.s.close("c1",r["trade_id"],"2026-06-01T14:00:01Z",True)
        self.assertEqual(c["state"],"exit_pending")
        self.assertEqual(self.s.evaluate("e2",r["trade_id"])["outcome"],"filled_exit")
        closed=self.s.get(r["trade_id"])["trade"]
        self.assertEqual(closed["state"],"closed")
        self.assertEqual(len(closed["fills"]),2)
        self.assertIsInstance(closed["fills"][0]["quote"],dict)
        self.assertIsInstance(closed["fills"][0]["fee_breakdown"],dict)
        self.assertIsInstance(closed["evaluations"][0]["calculations"],dict)
        self.assertIsInstance(closed["evaluations"][0]["warnings"],list)
        self.assertEqual(self.s.evaluate("e3",r["trade_id"])["calculations"]["net_realized_pnl"],"-10.031950")
    def test_depth_and_marketable_limit(self):
        self.s.provider.size="2"; r=self.record()
        depth=self.s.evaluate("e",r["trade_id"])
        self.assertEqual(depth["outcome"],"indeterminate_due_to_depth")
        self.assertEqual(depth["state_after"],"indeterminate")
        self.assertEqual(self.s.get(r["trade_id"])["trade"]["state"],"indeterminate")
        self.s.provider.size="100"; r2=self.record(request_id="r2",order_type="limit",limit_price="101")
        self.assertEqual(self.s.evaluate("e2",r2["trade_id"])["outcome"],"filled_entry_and_marked")

    def test_short_fee_and_pnl(self):
        trade=self.record(request_id="short",position_side="short")
        opened=self.s.evaluate("short-open",trade["trade_id"])
        self.assertEqual(opened["calculations"]["fill"]["fees"],"0.031950")
        self.assertEqual(opened["calculations"]["position"]["gross_pnl"],"-10")
        self.assertEqual(opened["calculations"]["position"]["net_liquidation_pnl"],"-10.031950")

    def test_pending_embargo_and_expired_order_state(self):
        recent=self.s.record(request_id="recent",symbol="AAPL",position_side="long",quantity=1,
                             order_type="market",submitted_at="2026-06-02T17:50:00Z",backtest=True)
        pending=self.s.evaluate("recent-eval",recent["trade_id"])
        self.assertEqual(pending["outcome"],"pending_data")
        self.assertIn("earliest_retry_at",pending["calculations"])

        expired=self.s.record(request_id="expired",symbol="AAPL",position_side="long",quantity=1,
                              order_type="market",submitted_at="2026-06-01T18:00:00Z",backtest=True)
        result=self.s.evaluate("expired-eval",expired["trade_id"])
        self.assertEqual(result["outcome"],"expired")
        self.assertEqual(result["outcome_reason"],"day_order_submitted_after_regular_session_close")
        self.assertEqual(result["state_after"],"expired")
        self.assertEqual(self.s.get(expired["trade_id"])["trade"]["state"],"expired")

    def test_validation_and_decimal_snapshot(self):
        with self.assertRaises(DomainError): self.record(request_id="nan",order_type="limit",limit_price="NaN")
        self.assertEqual(canonical({"price":Decimal("12.3400")}),'{"price":"12.3400"}')

    def test_omitted_timestamp_idempotency_survives_clock_advance(self):
        prospective=self.s.record(request_id="prospective",symbol="AAPL",position_side="long",
                                  quantity=1,order_type="market")
        self.now+=timedelta(minutes=1)
        self.assertEqual(prospective,self.s.record(request_id="prospective",symbol="AAPL",position_side="long",
                                                   quantity=1,order_type="market"))

        historical=self.record(request_id="historical")
        evaluated=self.s.evaluate("evaluate-omitted",historical["trade_id"])
        self.now+=timedelta(minutes=1)
        self.assertEqual(evaluated,self.s.evaluate("evaluate-omitted",historical["trade_id"]))

        closed=self.s.close("close-omitted",historical["trade_id"])
        self.now+=timedelta(minutes=1)
        self.assertEqual(closed,self.s.close("close-omitted",historical["trade_id"]))

    def test_execution_chronology_and_quote_provenance(self):
        self.s.provider.quote_time="2026-06-01T14:00:00.100000000Z"
        trade=self.record()
        too_early=self.s.evaluate("too-early",trade["trade_id"],"2026-06-01T14:00:00.200000Z")
        self.assertEqual(too_early["outcome_reason"],"evaluation_as_of_precedes_order_eligibility")
        self.assertEqual(self.s.get(trade["trade_id"])["trade"]["state"],"pending_data")

        opened=self.s.evaluate("open",trade["trade_id"],"2026-06-01T14:00:00.500000Z")
        fill=opened["calculations"]["fill"]
        self.assertEqual(fill["filled_at"],"2026-06-01T14:00:00.250000Z")
        self.assertEqual(fill["quote_as_of"],"2026-06-01T14:00:00.100000000Z")
        with self.assertRaisesRegex(DomainError,"precedes the stored entry"):
            self.s.evaluate("mark-too-early",trade["trade_id"],"2026-06-01T14:00:00.200000Z")
        with self.assertRaisesRegex(DomainError,"must not precede"):
            self.s.close("close-too-early",trade["trade_id"],"2026-06-01T14:00:00.200000Z",True)

        self.s.close("close",trade["trade_id"],"2026-06-01T14:00:00.500000Z",True)
        self.s.evaluate("exit",trade["trade_id"],"2026-06-01T14:00:01Z")
        with self.assertRaisesRegex(DomainError,"predates the stored exit"):
            self.s.evaluate("closed-too-early",trade["trade_id"],"2026-06-01T14:00:00.600000Z")

    def test_entry_fill_remains_visible_when_mark_is_unavailable(self):
        class EntryOnlyProvider(FakeProvider):
            def __init__(self):
                super().__init__(); self.calls=0
            def quotes(self,symbol,start,end):
                self.calls+=1
                return super().quotes(symbol,start,end) if self.calls == 1 else []
        self.s.provider=EntryOnlyProvider()
        trade=self.record()
        result=self.s.evaluate("entry-only",trade["trade_id"],"2026-06-01T14:01:00Z")
        self.assertEqual(result["outcome"],"filled_entry_mark_indeterminate_no_usable_quote")
        self.assertIn("mark",result["calculations"])
        self.assertNotIn("earliest_retry_at",result["calculations"]["mark"])
        self.assertIn("paper_trade_close",result["next_action"])
        stored=self.s.get(trade["trade_id"])["trade"]
        self.assertEqual(stored["state"],"open")
        self.assertEqual(len(stored["fills"]),1)

    def test_entry_fill_with_pending_mark_has_retry_time(self):
        trade=self.record()
        result=self.s.evaluate("entry-pending-mark",trade["trade_id"],"2026-06-02T17:50:00Z")
        self.assertEqual(result["outcome"],"filled_entry_mark_pending_data")
        self.assertIn("earliest_retry_at",result["calculations"]["mark"])
        self.assertIn("earliest_retry_at",result["next_action"])

    def test_second_exit_fill_controls_closed_trade_chronology(self):
        trade=self.record()
        self.s.evaluate("open",trade["trade_id"],"2026-06-01T14:00:01Z")
        self.s.close("first-close",trade["trade_id"],"2026-06-01T14:00:01Z",True)
        original_quotes=self.s.provider.quotes
        self.s.provider.quotes=lambda symbol,start,end: []
        first=self.s.evaluate("first-exit",trade["trade_id"],"2026-06-01T14:00:02Z")
        self.assertEqual(first["state_after"],"open")
        self.s.provider.quotes=original_quotes
        self.s.close("second-close",trade["trade_id"],"2026-06-01T14:01:00Z",True)
        self.s.evaluate("second-exit",trade["trade_id"],"2026-06-01T14:02:00Z")
        with self.assertRaisesRegex(DomainError,"predates the stored exit"):
            self.s.evaluate("before-second-exit",trade["trade_id"],"2026-06-01T14:00:30Z")

    def test_concurrent_evaluations_cannot_double_fill(self):
        barrier=threading.Barrier(2)
        lock=threading.Lock()
        class RacingProvider(FakeProvider):
            def __init__(self):
                super().__init__(); self.calls=0
            def quotes(self,symbol,start,end):
                with lock:
                    self.calls+=1; call=self.calls
                if call <= 2:
                    barrier.wait(timeout=5)
                return super().quotes(symbol,start,end)
        self.s.provider=RacingProvider()
        trade=self.record()
        def evaluate(request_id):
            try:
                return self.s.evaluate(request_id,trade["trade_id"],"2026-06-01T14:00:01Z")
            except Exception as exc:
                return exc
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(evaluate,["race-1","race-2"]))
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertEqual(sum(isinstance(result,DomainError) for result in results),1)
        stored=self.s.get(trade["trade_id"])["trade"]
        self.assertEqual(len(stored["fills"]),1)
        self.assertEqual(stored["state"],"open")


if __name__ == "__main__": unittest.main()
