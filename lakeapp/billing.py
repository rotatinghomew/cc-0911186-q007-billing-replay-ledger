from __future__ import annotations

from .derive import (
    BILL_CLOSED,
    BILL_OPEN,
    STATUS_ADJUSTABLE,
    STATUS_CLOSED,
    STATUS_PENDING,
    STATUS_WRITTEN_OFF,
)
from .storage import transaction


def billable_status(closed: bool, duplicate: bool, conflict: bool) -> str:
    """三要素到计费事实状态的纯判定，保留为领域规则基线。"""
    if conflict:
        return STATUS_PENDING
    if duplicate:
        return STATUS_WRITTEN_OFF
    return STATUS_CLOSED if closed else STATUS_ADJUSTABLE


def adjustment_required(closed: bool, changed: bool) -> bool:
    """账单关闭且事实发生变更时，必须以调整而不是改写来处理。"""
    return closed and changed


def is_bill_closed(connection, business_date: str) -> bool:
    row = connection.execute("SELECT status FROM bills WHERE business_date=?", (business_date,)).fetchone()
    return row is not None and row["status"] == BILL_CLOSED


def close_bill(connection, business_date: str) -> None:
    """关闭业务日期的日结账单。

    仍有待核对冲突时拒绝关账，迫使财务先完成核对；
    关闭后计费事实冻结为“已关闭”，之后到达的更正只能产生调整。
    """
    pending = connection.execute(
        "SELECT 1 FROM fact_conflicts WHERE business_date=? AND status='pending' LIMIT 1",
        (business_date,),
    ).fetchone()
    if pending:
        raise ValueError("仍有待核对冲突，不能关账")
    with transaction(connection):
        connection.execute(
            "INSERT INTO bills(business_date,status,closed_at) VALUES(?, ?, datetime('now')) "
            "ON CONFLICT(business_date) DO UPDATE SET status=excluded.status, closed_at=excluded.closed_at",
            (business_date, BILL_CLOSED),
        )
        connection.execute(
            "UPDATE billing_facts SET status=? WHERE business_date=? AND status!=?",
            (STATUS_CLOSED, business_date, STATUS_WRITTEN_OFF),
        )


def _other_pending_exists(connection, source: str, business_key: str, business_date: str, candidate_event_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM fact_conflicts WHERE source=? AND business_key=? AND business_date=? "
        "AND status='pending' AND candidate_event_id!=? LIMIT 1",
        (source, business_key, business_date, candidate_event_id),
    ).fetchone()
    return row is not None


def _restore_fact_status(connection, source: str, business_key: str, business_date: str) -> None:
    """冲突清空后，开放账单下的事实恢复为可调整；已关闭账单保持冻结。"""
    if _other_pending_exists(connection, source, business_key, business_date, ""):
        return
    connection.execute(
        "UPDATE billing_facts SET status=? WHERE source=? AND business_key=? AND business_date=? AND status=?",
        (STATUS_ADJUSTABLE, source, business_key, business_date, STATUS_PENDING),
    )


def _write_adjustment(connection, conflict, fact, adjusted_value: float, reason: str) -> int:
    original_value = fact["value"] if fact is not None else 0.0
    original_event_id = fact["event_id"] if fact is not None else None
    delta = adjusted_value - original_value
    if delta == 0:
        # 与原日结结果无差额的“更正”不产生调整行。
        return None
    cursor = connection.execute(
        "INSERT INTO adjustments(source,business_key,business_date,original_event_id,adjustment_event_id,"
        "source_batch,kind,original_value,adjusted_value,delta,reason,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (
            conflict["source"],
            conflict["business_key"],
            conflict["business_date"],
            original_event_id,
            conflict["candidate_event_id"],
            conflict["source_batch"],
            conflict["candidate_kind"],
            original_value,
            adjusted_value,
            delta,
            reason,
        ),
    )
    return cursor.lastrowid


def resolve_conflict(
    connection,
    source: str,
    business_key: str,
    business_date: str,
    candidate_event_id: str,
    decision: str,
    reason: str,
) -> dict:
    """核对一条同键异内容候选。

    decision='accept'：账单开放则重新计算事实；账单已关闭则原结果不动，
    只写入带事件身份、采集批次和缘由的调整。
    decision='reject'：候选作废，事实恢复为可调整（开放账单）。
    """
    if decision not in {"accept", "reject"}:
        raise ValueError("核对决策只能是 accept 或 reject")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("核对必须填写缘由")
    conflict = connection.execute(
        "SELECT * FROM fact_conflicts WHERE source=? AND business_key=? AND business_date=? "
        "AND candidate_event_id=?",
        (source, business_key, business_date, candidate_event_id),
    ).fetchone()
    if conflict is None:
        raise KeyError("冲突不存在")
    if conflict["status"] != "pending":
        raise ValueError("该冲突已核对")

    closed = is_bill_closed(connection, business_date)
    result = {"decision": decision, "closed": closed, "adjustment_id": None}
    with transaction(connection):
        fact = connection.execute(
            "SELECT * FROM billing_facts WHERE source=? AND business_key=? AND business_date=?",
            (source, business_key, business_date),
        ).fetchone()
        if fact is None and conflict["candidate_kind"] == "retract":
            # 跨日撤回：与归并逻辑一致，回退到该业务键最近一条事实作为原日结依据。
            fact = connection.execute(
                "SELECT * FROM billing_facts WHERE source=? AND business_key=? "
                "ORDER BY business_date DESC LIMIT 1",
                (source, business_key),
            ).fetchone()

        if decision == "reject":
            connection.execute(
                "UPDATE fact_conflicts SET status='rejected', decided_at=datetime('now'), decision='reject', reason=? "
                "WHERE source=? AND business_key=? AND business_date=? AND candidate_event_id=?",
                (reason, source, business_key, business_date, candidate_event_id),
            )
            if not closed:
                _restore_fact_status(connection, source, business_key, business_date)
            return result

        # decision == 'accept'
        if conflict["candidate_kind"] == "retract":
            adjusted_value = 0.0
            if closed:
                result["adjustment_id"] = _write_adjustment(connection, conflict, fact, adjusted_value, reason)
            else:
                if fact is not None:
                    connection.execute(
                        "DELETE FROM billing_facts WHERE source=? AND business_key=? AND business_date=?",
                        (source, business_key, business_date),
                    )
                connection.execute(
                    "UPDATE fact_conflicts SET status='rejected', decided_at=datetime('now'), "
                    "decision='reject', reason=? WHERE source=? AND business_key=? AND business_date=? "
                    "AND status='pending' AND candidate_event_id!=?",
                    ("读数已撤回，其余候选自动作废", source, business_key, business_date, candidate_event_id),
                )
        else:
            adjusted_value = conflict["candidate_value"]
            if closed:
                result["adjustment_id"] = _write_adjustment(connection, conflict, fact, adjusted_value, reason)
            elif fact is None:
                connection.execute(
                    "INSERT INTO billing_facts(source,business_key,business_date,event_id,value,status,"
                    "source_batch,version,updated_at) VALUES(?,?,?,?,?,?,?,1,datetime('now'))",
                    (source, business_key, business_date, candidate_event_id, adjusted_value,
                     STATUS_ADJUSTABLE, conflict["source_batch"]),
                )
            else:
                connection.execute(
                    "UPDATE billing_facts SET event_id=?, value=?, source_batch=?, version=version+1, "
                    "status=?, updated_at=datetime('now') WHERE source=? AND business_key=? AND business_date=?",
                    (candidate_event_id, adjusted_value, conflict["source_batch"], STATUS_ADJUSTABLE,
                     source, business_key, business_date),
                )

        connection.execute(
            "UPDATE fact_conflicts SET status='accepted', decided_at=datetime('now'), decision='accept', reason=? "
            "WHERE source=? AND business_key=? AND business_date=? AND candidate_event_id=?",
            (reason, source, business_key, business_date, candidate_event_id),
        )
        if not closed and conflict["candidate_kind"] == "upsert":
            _restore_fact_status(connection, source, business_key, business_date)
    return result


def adjustments(connection, business_date: str | None = None, business_key: str | None = None) -> list[dict]:
    sql = (
        "SELECT adjustment_id,source,business_key,business_date,original_event_id,adjustment_event_id,"
        "source_batch,kind,original_value,adjusted_value,delta,reason,created_at "
        "FROM adjustments WHERE 1=1"
    )
    params: list = []
    if business_date is not None:
        sql += " AND business_date=?"
        params.append(business_date)
    if business_key is not None:
        sql += " AND business_key=?"
        params.append(business_key)
    sql += " ORDER BY business_date,adjustment_id"
    return [dict(row) for row in connection.execute(sql, params)]


def monthly_summary(connection, month: str) -> list[dict]:
    """月度汇总：原始结算与后续调整分列，给出最终金额的完整解释链。

    month 形如 '2026-11'。原始结算取日结事实（关账后冻结的值），
    调整取 adjustments 中带依据的差额，二者相加为租户最终看到的金额。
    """
    pattern = month + "%"
    original_rows = connection.execute(
        "SELECT source,business_key, COUNT(*) AS fact_days, COALESCE(SUM(value),0) AS original_settlement "
        "FROM billing_facts WHERE business_date LIKE ? GROUP BY source,business_key",
        (pattern,),
    ).fetchall()
    adjustment_rows = connection.execute(
        "SELECT source,business_key, COUNT(*) AS adjustment_count, COALESCE(SUM(delta),0) AS adjustment_delta "
        "FROM adjustments WHERE business_date LIKE ? GROUP BY source,business_key",
        (pattern,),
    ).fetchall()
    merged: dict[tuple, dict] = {}
    for row in original_rows:
        merged[(row["source"], row["business_key"])] = {
            "source": row["source"],
            "business_key": row["business_key"],
            "month": month,
            "fact_days": row["fact_days"],
            "original_settlement": row["original_settlement"],
            "adjustment_count": 0,
            "adjustment_delta": 0.0,
            "final_amount": row["original_settlement"],
        }
    for row in adjustment_rows:
        key = (row["source"], row["business_key"])
        item = merged.setdefault(key, {
            "source": row["source"],
            "business_key": row["business_key"],
            "month": month,
            "fact_days": 0,
            "original_settlement": 0.0,
            "adjustment_count": 0,
            "adjustment_delta": 0.0,
            "final_amount": 0.0,
        })
        item["adjustment_count"] = row["adjustment_count"]
        item["adjustment_delta"] = row["adjustment_delta"]
        item["final_amount"] = item["original_settlement"] + row["adjustment_delta"]
    return [merged[key] for key in sorted(merged)]


def review_rows(connection, business_date: str | None = None) -> list[dict]:
    """复核视图：把原始读数、送达批次、去重后计费量、调整缘由、账单状态对上。"""
    sql = (
        "SELECT e.event_id,e.source,e.business_key,e.business_date,e.kind,e.value,e.occurred_at,"
        "e.first_source_batch AS source_batch, "
        "(SELECT GROUP_CONCAT(source_batch) FROM event_deliveries d WHERE d.event_id=e.event_id) AS deliveries "
        "FROM raw_events e"
    )
    params: list = []
    if business_date is not None:
        sql += " WHERE e.business_date=?"
        params.append(business_date)
    sql += " ORDER BY e.source,e.business_key,e.business_date,e.event_id"
    result = []
    for row in connection.execute(sql, params):
        item = dict(row)
        item["deliveries"] = item["deliveries"].split(",") if item["deliveries"] else []
        fact = connection.execute(
            "SELECT value,status,event_id FROM billing_facts WHERE source=? AND business_key=? AND business_date=?",
            (item["source"], item["business_key"], item["business_date"]),
        ).fetchone()
        item["billable_value"] = fact["value"] if fact else None
        item["fact_status"] = fact["status"] if fact else None
        bill = connection.execute("SELECT status FROM bills WHERE business_date=?", (item["business_date"],)).fetchone()
        item["bill_status"] = bill["status"] if bill else BILL_OPEN
        adjustment = connection.execute(
            "SELECT adjustment_id,delta,reason FROM adjustments WHERE adjustment_event_id=?",
            (item["event_id"],),
        ).fetchone()
        item["adjustment_id"] = adjustment["adjustment_id"] if adjustment else None
        item["adjustment_delta"] = adjustment["delta"] if adjustment else None
        item["adjustment_reason"] = adjustment["reason"] if adjustment else None
        result.append(item)
    return result
