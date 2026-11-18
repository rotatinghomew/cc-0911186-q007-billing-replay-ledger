import tempfile
import unittest
from pathlib import Path

from lakeapp.billing import (
    adjustments,
    close_bill,
    is_bill_closed,
    monthly_summary,
    resolve_conflict,
    review_rows,
)
from lakeapp.contract import parse_event
from lakeapp.derive import (
    STATUS_ADJUSTABLE,
    STATUS_CLOSED,
    STATUS_PENDING,
    apply_events,
    conflicts,
    facts,
)
from lakeapp.ingest import raw_events, receive
from lakeapp.storage import open_store

DAY1 = "2026-11-01T09:00:00"
DAY1_DATE = "2026-11-01"
DAY2_DATE = "2026-11-02"
DAY3_DATE = "2026-11-03"


def upsert(key, value, occurred_at=DAY1):
    return {"business_key": key, "kind": "upsert", "value": value, "occurred_at": occurred_at}


def retract(key, occurred_at):
    return {"business_key": key, "kind": "retract", "value": 0, "occurred_at": occurred_at}


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = open_store(Path(self.tempdir.name) / "data.db")

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def _ingest(self, source, batch, cursor, records):
        events = receive(self.connection, source, batch, cursor, records)
        return apply_events(self.connection, events)

    def _pending(self, key, date=DAY1_DATE):
        return conflicts(self.connection, "pending")

    def test_same_reading_redelivery_is_idempotent_across_batches(self):
        # 不同采集批次送达完全相同的读数内容，事件身份一致，只计一次费。
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        self.assertEqual(self._ingest("meter", "b2", 2, [upsert("t-1", 10)]), 0)
        rows = facts(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 10.0)
        self.assertEqual(rows[0]["status"], STATUS_ADJUSTABLE)
        # 原始读数只有一条，但两次送达批次都有登记。
        raw = raw_events(self.connection)
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["deliveries"], ["b1", "b2"])

    def test_same_key_different_content_waits_for_review(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        self._ingest("meter", "b2", 2, [upsert("t-1", 12)])
        rows = facts(self.connection)
        # 原计费量不被覆盖，事实进入待核对。
        self.assertEqual(rows[0]["value"], 10.0)
        self.assertEqual(rows[0]["status"], STATUS_PENDING)
        pending = self._pending("t-1")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["candidate_value"], 12.0)

    def test_accept_conflict_while_open_recalculates_fact(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        self._ingest("meter", "b2", 2, [upsert("t-1", 12)])
        candidate = conflicts(self.connection, "pending")[0]
        result = resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "accept", "采集器校准修正",
        )
        self.assertFalse(result["closed"])
        self.assertIsNone(result["adjustment_id"])
        row = facts(self.connection)[0]
        self.assertEqual(row["value"], 12.0)
        self.assertEqual(row["status"], STATUS_ADJUSTABLE)
        self.assertEqual(row["event_id"], candidate["candidate_event_id"])
        self.assertEqual(row["source_batch"], "b2")
        self.assertEqual(adjustments(self.connection), [])

    def test_reject_conflict_keeps_original_fact(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        self._ingest("meter", "b2", 2, [upsert("t-1", 12)])
        candidate = conflicts(self.connection, "pending")[0]
        resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "reject", "重复采集器噪声",
        )
        row = facts(self.connection)[0]
        self.assertEqual(row["value"], 10.0)
        self.assertEqual(row["status"], STATUS_ADJUSTABLE)
        self.assertEqual(conflicts(self.connection, "pending"), [])

    def test_cannot_close_bill_with_pending_conflict(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        self._ingest("meter", "b2", 2, [upsert("t-1", 12)])
        with self.assertRaises(ValueError):
            close_bill(self.connection, DAY1_DATE)
        candidate = conflicts(self.connection, "pending")[0]
        resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "accept", "先核对再关账",
        )
        close_bill(self.connection, DAY1_DATE)
        self.assertTrue(is_bill_closed(self.connection, DAY1_DATE))
        self.assertEqual(facts(self.connection)[0]["status"], STATUS_CLOSED)

    def test_late_correction_after_close_becomes_sourced_adjustment(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        close_bill(self.connection, DAY1_DATE)
        # 关账后到达的更正读数。
        self._ingest("meter", "b2", 2, [upsert("t-1", 12)])
        # 原日结结果纹丝不动。
        fact = facts(self.connection)[0]
        self.assertEqual(fact["value"], 10.0)
        self.assertEqual(fact["status"], STATUS_CLOSED)
        candidate = conflicts(self.connection, "pending")[0]
        result = resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "accept", "账单关闭后采集器补报",
        )
        self.assertTrue(result["closed"])
        ledger = adjustments(self.connection)
        self.assertEqual(len(ledger), 1)
        entry = ledger[0]
        self.assertEqual(entry["original_value"], 10.0)
        self.assertEqual(entry["adjusted_value"], 12.0)
        self.assertEqual(entry["delta"], 2.0)
        self.assertEqual(entry["source_batch"], "b2")
        self.assertEqual(entry["reason"], "账单关闭后采集器补报")
        self.assertEqual(entry["adjustment_event_id"], candidate["candidate_event_id"])
        self.assertEqual(entry["original_event_id"], fact["event_id"])
        # 原事实依然冻结。
        self.assertEqual(facts(self.connection)[0]["value"], 10.0)

    def test_duplicate_redelivery_after_close_is_silent_noop(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        close_bill(self.connection, DAY1_DATE)
        # 同一读数内容由新批次重送：幂等，不产生冲突也不产生调整。
        self.assertEqual(self._ingest("meter", "b2", 2, [upsert("t-1", 10)]), 0)
        self.assertEqual(conflicts(self.connection), [])
        self.assertEqual(adjustments(self.connection), [])

    def test_zero_delta_correction_after_close_creates_no_adjustment(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        close_bill(self.connection, DAY1_DATE)
        # 内容不同（时间戳不同 → 指纹不同）但 value 相同。
        self._ingest("meter", "b2", 2, [upsert("t-1", 10, "2026-11-01T10:00:00")])
        candidate = conflicts(self.connection, "pending")[0]
        result = resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "accept", "时间戳修正，金额无变化",
        )
        self.assertIsNone(result["adjustment_id"])
        self.assertEqual(adjustments(self.connection), [])
        self.assertEqual(facts(self.connection)[0]["value"], 10.0)

    def test_retract_after_close_becomes_adjustment_to_zero(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        close_bill(self.connection, DAY1_DATE)
        self._ingest("meter", "b2", 2, [retract("t-1", DAY1 + "T12:00:00")])
        candidate = conflicts(self.connection, "pending")[0]
        resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "accept", "关账后撤回误报读数",
        )
        ledger = adjustments(self.connection)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["delta"], -10.0)
        self.assertEqual(ledger[0]["adjusted_value"], 0.0)
        # 原日结事实保留，未被删除。
        self.assertEqual(len(facts(self.connection)), 1)
        self.assertEqual(facts(self.connection)[0]["value"], 10.0)

    def test_reject_after_close_keeps_original_without_adjustment(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        close_bill(self.connection, DAY1_DATE)
        self._ingest("meter", "b2", 2, [upsert("t-1", 99)])
        candidate = conflicts(self.connection, "pending")[0]
        resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "reject", "异常采集值，不予采信",
        )
        self.assertEqual(adjustments(self.connection), [])
        self.assertEqual(facts(self.connection)[0]["value"], 10.0)
        self.assertEqual(facts(self.connection)[0]["status"], STATUS_CLOSED)

    def test_replayed_batch_stays_idempotent(self):
        records = [upsert("t-1", 10)]
        first = receive(self.connection, "meter", "b1", 1, records)
        self.assertEqual(apply_events(self.connection, first), 1)
        replay = receive(self.connection, "meter", "b1", 1, records)
        self.assertEqual(apply_events(self.connection, replay), 0)
        self.assertEqual(len(raw_events(self.connection)), 1)
        # 同一批次只登记一次送达。
        self.assertEqual(raw_events(self.connection)[0]["deliveries"], ["b1"])

    def test_review_rows_tie_raw_billing_adjustment_and_bill_status(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        self._ingest("meter", "b2", 2, [upsert("t-1", 10)])  # 重送
        close_bill(self.connection, DAY1_DATE)
        self._ingest("meter", "b3", 3, [upsert("t-1", 12)])  # 关账后更正
        candidate = conflicts(self.connection, "pending")[0]
        resolve_conflict(
            self.connection, "meter", "t-1", DAY1_DATE,
            candidate["candidate_event_id"], "accept", "补报修正",
        )
        review = review_rows(self.connection, DAY1_DATE)
        by_event = {row["event_id"]: row for row in review}
        original_event = parse_event("meter", "b1", 1, upsert("t-1", 10)).event_id
        correction_event = candidate["candidate_event_id"]
        original = by_event[original_event]
        self.assertEqual(original["deliveries"], ["b1", "b2"])
        self.assertEqual(original["billable_value"], 10.0)
        self.assertEqual(original["bill_status"], "closed")
        correction = by_event[correction_event]
        self.assertEqual(correction["adjustment_delta"], 2.0)
        self.assertEqual(correction["adjustment_reason"], "补报修正")
        self.assertEqual(correction["source_batch"], "b3")

    def test_monthly_summary_separates_original_settlement_and_adjustments(self):
        # 11-01：原始 10，关账后调整 +2 → 最终 12
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        close_bill(self.connection, DAY1_DATE)
        self._ingest("meter", "b2", 2, [upsert("t-1", 12)])
        c1 = conflicts(self.connection, "pending")[0]
        resolve_conflict(self.connection, "meter", "t-1", DAY1_DATE,
                         c1["candidate_event_id"], "accept", "补报修正")
        # 11-02：原始 5，账单仍开放
        self._ingest("meter", "b3", 3, [upsert("t-1", 5, f"{DAY2_DATE}T09:00:00")])
        # 11-03：另一个租户原始 7，关账后撤回调整 -7 → 最终 0
        self._ingest("meter", "b4", 4, [upsert("t-2", 7, f"{DAY3_DATE}T09:00:00")])
        close_bill(self.connection, DAY3_DATE)
        self._ingest("meter", "b5", 5, [retract("t-2", f"{DAY3_DATE}T11:00:00")])
        c2 = conflicts(self.connection, "pending")[0]
        resolve_conflict(self.connection, "meter", "t-2", DAY3_DATE,
                         c2["candidate_event_id"], "accept", "误报撤回")

        summary = {row["business_key"]: row for row in monthly_summary(self.connection, "2026-11")}
        t1 = summary["t-1"]
        self.assertEqual(t1["fact_days"], 2)
        self.assertEqual(t1["original_settlement"], 15.0)
        self.assertEqual(t1["adjustment_count"], 1)
        self.assertEqual(t1["adjustment_delta"], 2.0)
        self.assertEqual(t1["final_amount"], 17.0)
        t2 = summary["t-2"]
        self.assertEqual(t2["original_settlement"], 7.0)
        self.assertEqual(t2["adjustment_delta"], -7.0)
        self.assertEqual(t2["final_amount"], 0.0)

    def test_multiple_conflicts_resolve_in_order(self):
        self._ingest("meter", "b1", 1, [upsert("t-1", 10)])
        self._ingest("meter", "b2", 2, [upsert("t-1", 12)])
        self._ingest("meter", "b3", 3, [upsert("t-1", 14)])
        pending = conflicts(self.connection, "pending")
        self.assertEqual(len(pending), 2)
        # 先采纳 12，再采纳 14。
        first = next(row for row in pending if row["candidate_value"] == 12.0)
        resolve_conflict(self.connection, "meter", "t-1", DAY1_DATE,
                         first["candidate_event_id"], "accept", "第一次修正")
        self.assertEqual(facts(self.connection)[0]["value"], 12.0)
        second = conflicts(self.connection, "pending")[0]
        self.assertEqual(second["candidate_value"], 14.0)
        resolve_conflict(self.connection, "meter", "t-1", DAY1_DATE,
                         second["candidate_event_id"], "accept", "第二次修正")
        self.assertEqual(facts(self.connection)[0]["value"], 14.0)
        self.assertEqual(conflicts(self.connection, "pending"), [])


if __name__ == "__main__":
    unittest.main()
