"""领域模型：输入事件与常量。

双时间口径：
- ``effective_at``：业务生效时间（事实属于哪一天/哪一时刻）；
- ``recorded_at``：知晓时间（系统何时得知该事实）。迟到事件的
  ``recorded_at`` 晚于 ``effective_at``；缺省时与 ``effective_at`` 相同。

按历史日期重算时，``effective_at`` 决定事实落在哪一天，
``recorded_at`` 决定某个知晓时点（as-of）能否看到它。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

#: 未标注资源类型时归入的缺省资源
DEFAULT_RESOURCE = "default"

# 事件种类
KIND_CAPACITY = "capacity"            # 容量台账：安装规模生效/变更
KIND_RESERVATION = "reservation"      # 预留单状态上报
KIND_OUTAGE = "outage"                # 停机开始
KIND_OUTAGE_RECOVERY = "outage_recovery"  # 停机恢复
KIND_REVOKE = "revoke"                # 撤销某条事件（如误报）
KIND_CORRECT = "correct"              # 更正某条事件（重述生效时间/数量）

#: 可被撤销/更正的业务事件
BUSINESS_KINDS = frozenset(
    {KIND_CAPACITY, KIND_RESERVATION, KIND_OUTAGE, KIND_OUTAGE_RECOVERY}
)
ALL_KINDS = BUSINESS_KINDS | {KIND_REVOKE, KIND_CORRECT}

#: 预留单业务状态：计入对外承诺（从可用量中扣减）
COMMITTING_STATUSES = frozenset({"approved", "active"})
#: 预留单业务状态：不计入承诺
NON_COMMITTING_STATUSES = frozenset(
    {"submitted", "pending", "released", "cancelled", "rejected", "expired"}
)
KNOWN_STATUSES = COMMITTING_STATUSES | NON_COMMITTING_STATUSES


@dataclass(frozen=True)
class Event:
    """一条输入事件（不可变）。

    - ``capacity``：``value`` 为安装规模，自 ``effective_at`` 起生效；
    - ``reservation``：``reservation_id`` + ``status`` 表示一次状态上报，
      ``value`` 为该预留单当前承诺数量（每次上报需重述全量）；
    - ``outage`` / ``outage_recovery``：``value`` 为停机/恢复数量；
    - ``revoke``：``target`` 指向被撤销事件 id，该事件自知晓后起不再生效；
    - ``correct``：``target`` 指向被更正事件 id，``effective_at`` 重述生效时间，
      ``value`` 缺省时沿用原值。
    """

    id: str
    kind: str
    park: Optional[str]
    resource: str
    effective_at: datetime
    recorded_at: datetime
    value: Optional[int] = None
    reservation_id: Optional[str] = None
    status: Optional[str] = None
    target: Optional[str] = None
    source: str = ""
    note: str = ""
    seq: int = 0

    def same_content(self, other: "Event") -> bool:
        """内容一致（忽略 id 与序号），用于识别幂等重投。"""
        return self._content_key() == other._content_key()

    def _content_key(self):
        return (
            self.kind,
            self.park,
            self.resource,
            self.effective_at,
            self.recorded_at,
            self.value,
            self.reservation_id,
            self.status,
            self.target,
            self.source,
            self.note,
        )
