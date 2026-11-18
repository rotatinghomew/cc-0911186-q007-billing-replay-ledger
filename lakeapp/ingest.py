from __future__ import annotations

from .contract import Event, parse_event
from .storage import encode, transaction


def _register_event(connection, event: Event) -> None:
    """登记原始读数及其送达批次。

    raw_events 以事件身份为主键：同一读数内容重送不会重复登记；
    event_deliveries 记录每次送达的采集批次，重复批次重放时
    因主键冲突被忽略，保证整条接收链路幂等。
    """
    connection.execute(
        "INSERT OR IGNORE INTO raw_events(event_id,source,business_key,business_date,kind,value,occurred_at,"
        "first_source_batch,first_seen_at) VALUES(?,?,?,?,?,?,?,?,datetime('now'))",
        (
            event.event_id,
            event.source,
            event.business_key,
            event.business_date,
            event.kind,
            event.value,
            event.occurred_at,
            event.batch_id,
        ),
    )
    connection.execute(
        "INSERT OR IGNORE INTO event_deliveries(event_id,source_batch,delivered_at) VALUES(?,?,datetime('now'))",
        (event.event_id, event.batch_id),
    )


def receive(connection, source: str, batch_id: str, cursor: int, records: list[dict]) -> list[Event]:
    existing = connection.execute(
        "SELECT payload FROM raw_batches WHERE source=? AND batch_id=?", (source, batch_id)
    ).fetchone()
    payload = {"cursor": cursor, "records": records}
    if existing:
        if existing["payload"] != encode(payload):
            raise ValueError("同一批次内容冲突")
        events = [parse_event(source, batch_id, cursor, item) for item in records]
    else:
        events = [parse_event(source, batch_id, cursor, item) for item in records]
        with transaction(connection):
            connection.execute(
                "INSERT INTO raw_batches(source,batch_id,cursor,payload,received_at) VALUES(?,?,?,?,datetime('now'))",
                (source, batch_id, cursor, encode(payload)),
            )
            connection.execute(
                "INSERT INTO source_progress(source,cursor) VALUES(?,?) "
                "ON CONFLICT(source) DO UPDATE SET cursor=MAX(cursor,excluded.cursor)",
                (source, cursor),
            )
            for event in events:
                _register_event(connection, event)
        return events
    # 批次重放：事件身份和送达登记同样保持幂等，不产生重复原始读数。
    with transaction(connection):
        for event in events:
            _register_event(connection, event)
    return events


def progress(connection) -> list[dict]:
    return [dict(row) for row in connection.execute("SELECT source,cursor FROM source_progress ORDER BY source")]


def raw_events(connection) -> list[dict]:
    """去重后的原始读数，附各读数被送达的采集批次清单，供复核追溯。"""
    rows = connection.execute(
        "SELECT event_id,source,business_key,business_date,kind,value,occurred_at,"
        "first_source_batch,first_seen_at FROM raw_events ORDER BY source,business_key,occurred_at,event_id"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["deliveries"] = [
            delivery_row["source_batch"]
            for delivery_row in connection.execute(
                "SELECT source_batch FROM event_deliveries WHERE event_id=? ORDER BY delivered_at,source_batch",
                (item["event_id"],),
            )
        ]
        result.append(item)
    return result
