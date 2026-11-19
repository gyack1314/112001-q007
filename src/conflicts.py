"""冲突模型：来源状态冲突不静默合并，全部显式登记。

严重级别：
- ``BLOCKING``：阻塞可靠判断，对应日事实的可用量不给数值（状态为“阻塞”）；
- ``WARNING``：数值照常给出但可靠性存疑（状态为“警告”）；
- ``INFO``：提示信息，不影响状态。

作用范围：
- ``SCOPE_DATE``：仅影响 ``date`` 当日；
- ``SCOPE_ONWARD``：自 ``date`` 起持续影响后续日期。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

BLOCKING = "BLOCKING"
WARNING = "WARNING"
INFO = "INFO"

SCOPE_DATE = "date"
SCOPE_ONWARD = "onward"

# 冲突种类
CAPACITY_CONFLICT = "CAPACITY_CONFLICT"            # 容量台账同一生效时刻取值冲突
CAPACITY_MISSING = "CAPACITY_MISSING"              # 有业务活动但无有效容量台账
EVENT_ID_CONFLICT = "EVENT_ID_CONFLICT"            # 同一事件 id 出现内容不同的版本
RECOVERY_EXCEEDS_OUTAGE = "RECOVERY_EXCEEDS_OUTAGE"  # 恢复数量超过在停数量（疑似重复回补）
REVOKE_UNKNOWN_TARGET = "REVOKE_UNKNOWN_TARGET"    # 撤销指向不存在的事件
CORRECT_UNKNOWN_TARGET = "CORRECT_UNKNOWN_TARGET"  # 更正指向不存在的事件
CORRECT_OF_REVOKED = "CORRECT_OF_REVOKED"          # 更正指向已撤销的事件
DUPLICATE_CORRECTION = "DUPLICATE_CORRECTION"      # 同一事件被多次更正
INVALID_TARGET_KIND = "INVALID_TARGET_KIND"        # 撤销/更正指向非业务事件
OVERCOMMITTED = "OVERCOMMITTED"                    # 承诺+停机超出安装容量（可用量为负）
RESERVATION_STATUS_UNKNOWN = "RESERVATION_STATUS_UNKNOWN"  # 预留单状态无法识别
OPEN_OUTAGE = "OPEN_OUTAGE"                        # 截至最后事件仍有未恢复停机

_SEVERITY_RANK = {BLOCKING: 0, WARNING: 1, INFO: 2}


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, 99)


@dataclass(frozen=True)
class Conflict:
    kind: str
    severity: str
    park: str
    resource: str
    date: Optional[date]
    scope: str
    message: str
    details: dict = field(default_factory=dict)

    def applies_to(self, day: date) -> bool:
        if self.date is None:
            return True
        if self.scope == SCOPE_DATE:
            return self.date == day
        return self.date <= day

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "park": self.park,
            "resource": self.resource,
            "date": self.date.isoformat() if self.date else None,
            "scope": self.scope,
            "message": self.message,
            "details": self.details,
        }

    @staticmethod
    def from_dict(d: dict) -> "Conflict":
        return Conflict(
            kind=d["kind"],
            severity=d["severity"],
            park=d["park"],
            resource=d["resource"],
            date=date.fromisoformat(d["date"]) if d.get("date") else None,
            scope=d["scope"],
            message=d["message"],
            details=d.get("details") or {},
        )
