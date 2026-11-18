from __future__ import annotations

from .contract import Event
from .storage import encode, transaction

# 计费事实状态
STATUS_ADJUSTABLE = "可调整"   # 账单开放，事实仍可重新计算
STATUS_PENDING = "待核对"      # 存在业务键相同但内容不同的候选，等待核对
STATUS_CLOSED = "已关闭"       # 账单已关闭，原日结结果冻结
STATUS_WRITTEN_OFF = "已抵扣"  # 读数已被撤回/抵扣

BILL_OPEN = "open"
BILL_CLOSED = "closed"


def _find_fact(connection, event: Event):
    return connection.execute(
        "SELECT source,business_key,business_date,event_id,value,status,source_batch,version "
        "FROM billing_facts WHERE source=? AND business_key=? AND business_date=?",
        (event.source, event.business_key, event.business_date),
    ).fetchone()


def _find_retract_target(connection, event: Event):
    """retract 优先撤回同业务日期的事实，没有则回退到该业务键最近一条事实。"""
    fact = _find_fact(connection, event)
    if fact:
        return fact
    return connection.execute(
        "SELECT source,business_key,business_date,event_id,value,status,source_batch,version "
        "FROM billing_facts WHERE source=? AND business_key=? ORDER BY business_date DESC LIMIT 1",
        (event.source, event.business_key),
    ).fetchone()


def _pending_conflict_exists(connection, event: Event) -> bool:
    row = connection.execute(
        "SELECT 1 FROM fact_conflicts WHERE source=? AND business_key=? AND business_date=? "
        "AND candidate_event_id=? AND status='pending'",
        (event.source, event.business_key, event.business_date, event.event_id),
    ).fetchone()
    return row is not None


def _register_conflict(connection, event: Event) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO fact_conflicts(source,business_key,business_date,candidate_event_id,"
        "candidate_kind,candidate_value,source_batch,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,'pending',datetime('now'))",
        (
            event.source,
            event.business_key,
            event.business_date,
            event.event_id,
            event.kind,
            event.value,
            event.batch_id,
        ),
    )


def _merge_event(connection, event: Event) -> int:
    fact = _find_fact(connection, event)

    if event.kind == "retract":
        target = _find_retract_target(connection, event)
        if target is None:
            return 0
        if target["status"] == STATUS_CLOSED:
            # 账单关闭：撤回不能删原结果，只能作为候选等待核对后转调整。
            if _pending_conflict_exists(connection, event):
                return 0
            _register_conflict(connection, event)
            return 1
        if target["event_id"] == event.event_id:
            return 0
        if _pending_conflict_exists(connection, event):
            return 0
        # 账单开放：直接撤回，并把该键上其余待核对候选作废。
        connection.execute(
            "DELETE FROM billing_facts WHERE source=? AND business_key=? AND business_date=?",
            (target["source"], target["business_key"], target["business_date"]),
        )
        connection.execute(
            "UPDATE fact_conflicts SET status='rejected', decided_at=datetime('now'), "
            "decision='reject', reason=? WHERE source=? AND business_key=? AND business_date=? AND status='pending'",
            ("读数已撤回，候选自动作废", target["source"], target["business_key"], target["business_date"]),
        )
        return 1

    if fact is None:
        bill = connection.execute("SELECT status FROM bills WHERE business_date=?", (event.business_date,)).fetchone()
        if bill is not None and bill["status"] == BILL_CLOSED:
            # 关账后到达的新读数：不补写原日结，进核对，确认后只留调整。
            if _pending_conflict_exists(connection, event):
                return 0
            _register_conflict(connection, event)
            return 1
        connection.execute(
            "INSERT INTO billing_facts(source,business_key,business_date,event_id,value,status,"
            "source_batch,version,updated_at) VALUES(?,?,?,?,?,?,?,1,datetime('now'))",
            (
                event.source,
                event.business_key,
                event.business_date,
                event.event_id,
                event.value,
                STATUS_ADJUSTABLE,
                event.batch_id,
            ),
        )
        return 1

    # 同一读数内容的重送：事件身份相同，保持幂等，不重新计费。
    if fact["event_id"] == event.event_id:
        return 0

    # 业务键相同但内容不同：保留候选等待核对，绝不覆盖当前计费量。
    if _pending_conflict_exists(connection, event):
        return 0
    _register_conflict(connection, event)
    if fact["status"] == STATUS_ADJUSTABLE:
        connection.execute(
            "UPDATE billing_facts SET status=? WHERE source=? AND business_key=? AND business_date=?",
            (STATUS_PENDING, fact["source"], fact["business_key"], fact["business_date"]),
        )
    return 1


def apply_events(connection, events: list[Event]) -> int:
    """把事件归并进计费事实，返回实际发生变化的数量。

    重送（事件身份已在事实或候选中）不计变化，保证幂等；
    同键异内容登记为待核对冲突；关账日期的变更只进冲突队列。
    """
    changed = 0
    with transaction(connection):
        for event in events:
            changed += _merge_event(connection, event)
    return changed


def facts(connection) -> list[dict]:
    """当前计费事实视图（保持基线字段形状，并补充事件身份与状态）。"""
    rows = connection.execute(
        "SELECT source,business_key,business_date,event_id,value,status,source_batch,version,updated_at "
        "FROM billing_facts ORDER BY source,business_key,business_date"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = encode({"value": row["value"], "business_date": row["business_date"]})
        result.append(item)
    return result


def conflicts(connection, status: str | None = None) -> list[dict]:
    sql = (
        "SELECT source,business_key,business_date,candidate_event_id,candidate_kind,candidate_value,"
        "source_batch,status,created_at,decided_at,decision,reason "
        "FROM fact_conflicts"
    )
    if status is not None:
        sql += " WHERE status=?"
        rows = connection.execute(sql + " ORDER BY source,business_key,business_date,created_at", (status,))
    else:
        rows = connection.execute(sql + " ORDER BY source,business_key,business_date,created_at")
    return [dict(row) for row in rows]
