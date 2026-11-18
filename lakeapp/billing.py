from __future__ import annotations


def billable_status(closed: bool, duplicate: bool, conflict: bool) -> str:
    if conflict:
        return "待核对"
    if duplicate:
        return "已抵扣"
    return "已关闭" if closed else "可调整"


def adjustment_required(closed: bool, changed: bool) -> bool:
    return closed and changed
