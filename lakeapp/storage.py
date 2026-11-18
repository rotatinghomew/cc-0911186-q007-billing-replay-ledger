from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_batches (
    source TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    cursor INTEGER NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (source, batch_id)
);
CREATE TABLE IF NOT EXISTS source_progress (
    source TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL
);
-- 去重后的原始读数：以事件身份（读数内容指纹）为主键，保留首次送达来源。
CREATE TABLE IF NOT EXISTS raw_events (
    event_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    business_date TEXT NOT NULL,
    kind TEXT NOT NULL,
    value REAL NOT NULL,
    occurred_at TEXT NOT NULL,
    first_source_batch TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
-- 事件身份与采集批次的送达登记：同一事件重送只新增送达记录，不产生新事实。
CREATE TABLE IF NOT EXISTS event_deliveries (
    event_id TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (event_id, source_batch)
);
-- 计费事实：按来源、业务键、业务日期归并后的当前计费量。
CREATE TABLE IF NOT EXISTS billing_facts (
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    business_date TEXT NOT NULL,
    event_id TEXT NOT NULL,
    value REAL NOT NULL,
    status TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, business_key, business_date)
);
-- 业务键相同但内容不同的候选读数，等待人工核对。
CREATE TABLE IF NOT EXISTS fact_conflicts (
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    business_date TEXT NOT NULL,
    candidate_event_id TEXT NOT NULL,
    candidate_kind TEXT NOT NULL,
    candidate_value REAL NOT NULL,
    source_batch TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision TEXT,
    reason TEXT,
    PRIMARY KEY (source, business_key, business_date, candidate_event_id)
);
-- 日结账单：按业务日期关闭，关闭后事实冻结。
CREATE TABLE IF NOT EXISTS bills (
    business_date TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    closed_at TEXT
);
-- 关账后留下的调整：带来源依据，不改动原日结结果。
CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    business_key TEXT NOT NULL,
    business_date TEXT NOT NULL,
    original_event_id TEXT,
    adjustment_event_id TEXT NOT NULL,
    source_batch TEXT NOT NULL,
    kind TEXT NOT NULL,
    original_value REAL NOT NULL,
    adjusted_value REAL NOT NULL,
    delta REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manifests (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (name, version)
);
"""


def open_store(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection):
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode(value: str):
    return json.loads(value)
