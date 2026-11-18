from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from .storage import encode


@dataclass(frozen=True)
class Event:
    source: str
    batch_id: str
    cursor: int
    business_key: str
    kind: str
    value: float
    occurred_at: str

    @property
    def business_date(self) -> str:
        """读数归属的业务日期，取自 occurred_at 的日期部分。"""
        return self.occurred_at[:10]

    @property
    def event_id(self) -> str:
        """事件身份：对读数内容计算指纹。

        同一读数内容无论由哪个采集批次、重送多少次，指纹都相同，
        因此重送在归并时天然幂等；业务键相同但内容不同则指纹不同，
        会进入待核对流程而不是覆盖既有事实。
        """
        fingerprint = hashlib.sha256()
        fingerprint.update(
            encode(
                {
                    "source": self.source,
                    "business_key": self.business_key,
                    "kind": self.kind,
                    "value": self.value,
                    "occurred_at": self.occurred_at,
                }
            ).encode("utf-8")
        )
        return fingerprint.hexdigest()


def parse_event(source: str, batch_id: str, cursor: int, raw: dict) -> Event:
    required = {"business_key", "kind", "value", "occurred_at"}
    missing = required - raw.keys()
    if missing:
        raise ValueError("缺少字段: " + ",".join(sorted(missing)))
    if not isinstance(raw["business_key"], str) or not raw["business_key"]:
        raise ValueError("业务键不能为空")
    if raw["kind"] not in {"upsert", "retract"}:
        raise ValueError("事件类型不受支持")
    occurred_at = str(raw["occurred_at"])
    try:
        date.fromisoformat(occurred_at[:10])
    except ValueError as exc:
        raise ValueError("occurred_at 不是合法日期") from exc
    return Event(source, batch_id, int(cursor), raw["business_key"], raw["kind"], float(raw["value"]), occurred_at)
