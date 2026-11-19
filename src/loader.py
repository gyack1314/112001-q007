"""输入加载与校验：events.json -> list[Event]。

输入契约（与 tests/test_inputs.py 一致）：每行必须有 ``kind`` 与 ``effective_at``。
校验失败立即抛错（带行号），不静默跳过脏数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Union

from .models import (
    ALL_KINDS,
    BUSINESS_KINDS,
    DEFAULT_RESOURCE,
    KIND_CAPACITY,
    KIND_OUTAGE,
    KIND_OUTAGE_RECOVERY,
    KIND_RESERVATION,
    Event,
)


def _parse_dt(raw, row_no: int, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise ValueError(f"第 {row_no} 行：{field} 不是合法 ISO 时间：{raw!r}") from None
    if dt.tzinfo is not None:  # 统一为朴素 UTC，避免与朴素时间比较出错
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fail(row_no: int, message: str) -> ValueError:
    return ValueError(f"第 {row_no} 行：{message}")


def _parse_event(row: dict, row_no: int) -> Event:
    if not isinstance(row, dict):
        raise _fail(row_no, "事件必须是 JSON 对象")
    kind = row.get("kind")
    if not kind:
        raise _fail(row_no, "缺少 kind")
    if kind not in ALL_KINDS:
        raise _fail(row_no, f"未知 kind：{kind!r}")
    if not row.get("effective_at"):
        raise _fail(row_no, "缺少 effective_at")
    effective_at = _parse_dt(row["effective_at"], row_no, "effective_at")
    if row.get("recorded_at"):
        recorded_at = _parse_dt(row["recorded_at"], row_no, "recorded_at")
    else:
        recorded_at = effective_at

    value = row.get("value")
    if value is not None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _fail(row_no, f"value 必须是非负整数：{value!r}")

    if kind in BUSINESS_KINDS:
        if not row.get("park"):
            raise _fail(row_no, f"{kind} 事件缺少 park")
        if kind == KIND_CAPACITY:
            if value is None:
                raise _fail(row_no, "capacity 事件缺少 value（安装规模）")
        elif kind in (KIND_OUTAGE, KIND_OUTAGE_RECOVERY):
            if not value:
                raise _fail(row_no, f"{kind} 事件缺少正整数 value（数量）")
        elif kind == KIND_RESERVATION:
            if not row.get("reservation_id"):
                raise _fail(row_no, "reservation 事件缺少 reservation_id")
            if not value:
                raise _fail(row_no, "reservation 事件缺少正整数 value（承诺数量）")
    else:  # revoke / correct
        if not row.get("target"):
            raise _fail(row_no, f"{kind} 事件缺少 target（目标事件 id）")

    return Event(
        id=row.get("id") or f"E{row_no}",
        kind=kind,
        park=row.get("park"),
        resource=row.get("resource") or DEFAULT_RESOURCE,
        effective_at=effective_at,
        recorded_at=recorded_at,
        value=value,
        reservation_id=row.get("reservation_id"),
        status=row.get("status"),
        target=row.get("target"),
        source=row.get("source") or "",
        note=row.get("note") or "",
        seq=row_no,
    )


def load_events(path: Union[str, Path]) -> List[Event]:
    """加载事件文件，返回按文件顺序排列的事件列表。"""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("事件文件必须是 JSON 数组")
    return [_parse_event(row, i) for i, row in enumerate(rows, start=1)]
