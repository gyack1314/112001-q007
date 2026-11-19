"""日事实引擎：按（园区, 资源类型, 日期）重算容量、预留、停机与可用量。

核心原则：
- 一切日事实都从事件日志整体重算，不做增量打补丁——因此迟到的恢复、
  撤销、更正只会改变“当前重算”的结果，不会产生负容量或重复回补；
- 可用量 = 安装容量 - 预留承诺 - 日终在停；停机池不允许为负，
  超出在停数量的恢复被截断并登记冲突（防止重复回补）；
- 来源冲突不静默取乐观值：阻塞型冲突使可用量不给数值，警告型冲突
  保留数值但标注可靠性存疑。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Dict, List, Optional, Tuple

from .conflicts import (
    BLOCKING,
    INFO,
    WARNING,
    CAPACITY_CONFLICT,
    CAPACITY_MISSING,
    CORRECT_OF_REVOKED,
    CORRECT_UNKNOWN_TARGET,
    DUPLICATE_CORRECTION,
    EVENT_ID_CONFLICT,
    INVALID_TARGET_KIND,
    OPEN_OUTAGE,
    OVERCOMMITTED,
    RECOVERY_EXCEEDS_OUTAGE,
    RESERVATION_STATUS_UNKNOWN,
    REVOKE_UNKNOWN_TARGET,
    SCOPE_DATE,
    SCOPE_ONWARD,
    Conflict,
    severity_rank,
)
from .models import (
    BUSINESS_KINDS,
    COMMITTING_STATUSES,
    KNOWN_STATUSES,
    KIND_CAPACITY,
    KIND_CORRECT,
    KIND_OUTAGE,
    KIND_OUTAGE_RECOVERY,
    KIND_RESERVATION,
    KIND_REVOKE,
    Event,
)

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_BLOCKED = "BLOCKED"


@dataclass
class FactRow:
    """某园区某资源类型某一天的日事实。

    - ``downtime_qty``：日终仍在停机的数量（驱动可用量）；
    - ``downtime_peak_qty``：当日在停峰值（含前一日结转），用于呈现
      “当天曾有多少容量暂不可用”；
    - ``available_qty``：``安装容量 - 预留 - 日终在停``；为负表示超承诺，
      不截断以便暴露缺口；阻塞型冲突存在时为 ``None``（不给数值）；
    - ``inputs``：构成本行的事实的输入事件 id 清单（决策依据）。
    """

    date: date
    park: str
    resource: str
    installed_capacity: Optional[int]
    reserved_qty: int
    downtime_qty: int
    downtime_peak_qty: int
    available_qty: Optional[int]
    status: str
    conflicts: List[Conflict] = field(default_factory=list)
    inputs: Dict[str, List[str]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "park": self.park,
            "resource": self.resource,
            "installed_capacity": self.installed_capacity,
            "reserved_qty": self.reserved_qty,
            "downtime_qty": self.downtime_qty,
            "downtime_peak_qty": self.downtime_peak_qty,
            "available_qty": self.available_qty,
            "status": self.status,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "inputs": self.inputs,
        }

    @staticmethod
    def from_dict(d: dict) -> "FactRow":
        return FactRow(
            date=date.fromisoformat(d["date"]),
            park=d["park"],
            resource=d["resource"],
            installed_capacity=d["installed_capacity"],
            reserved_qty=d["reserved_qty"],
            downtime_qty=d["downtime_qty"],
            downtime_peak_qty=d["downtime_peak_qty"],
            available_qty=d["available_qty"],
            status=d["status"],
            conflicts=[Conflict.from_dict(c) for c in d.get("conflicts", [])],
            inputs=d.get("inputs") or {},
        )


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min)


def _day_end(day: date) -> datetime:
    return datetime.combine(day, time.max)


class CapacityEngine:
    """从事件日志重算日事实。``as_of`` 为知晓时点：仅纳入
    ``recorded_at <= as_of`` 的记录；``None`` 表示纳入全部已知记录。"""

    def __init__(self, events: List[Event]):
        self._events = sorted(events, key=lambda e: e.seq)
        self._resolve_cache: Dict[datetime, Tuple[List[Event], List[Conflict]]] = {}

    # ------------------------------------------------------------------
    # 事件解析：撤销 / 更正 / 重复 id
    # ------------------------------------------------------------------
    def _resolve(self, as_of: Optional[datetime]) -> Tuple[List[Event], List[Conflict]]:
        key = as_of or datetime.max
        if key in self._resolve_cache:
            return self._resolve_cache[key]

        known = [e for e in self._events if e.recorded_at <= key]
        conflicts: List[Conflict] = []

        # 1) 同一 id 内容不同的版本：全部剔除并登记阻塞型冲突（不静默挑选）
        by_id: Dict[str, Event] = {}
        duplicate_ids = set()
        for e in known:
            prev = by_id.get(e.id)
            if prev is None:
                by_id[e.id] = e
            elif prev.same_content(e):
                continue  # 幂等重投，忽略
            else:
                duplicate_ids.add(e.id)
        for did in sorted(duplicate_ids):
            variants = [e for e in known if e.id == did]
            parks = sorted({v.park for v in variants if v.park} or {"(未知)"})
            resources = sorted({v.resource for v in variants} or {"(未知)"})
            conflicts.append(
                Conflict(
                    kind=EVENT_ID_CONFLICT,
                    severity=BLOCKING,
                    park="、".join(parks),
                    resource="、".join(resources),
                    date=min(v.effective_at.date() for v in variants),
                    scope=SCOPE_ONWARD,
                    message=(
                        f"事件 {did} 存在 {len(variants)} 个内容不同的版本，"
                        "已全部剔除，未静默挑选"
                    ),
                    details={
                        "variants": [
                            {
                                "seq": v.seq,
                                "kind": v.kind,
                                "value": v.value,
                                "effective_at": v.effective_at.isoformat(),
                                "recorded_at": v.recorded_at.isoformat(),
                                "source": v.source,
                            }
                            for v in variants
                        ]
                    },
                )
            )

        # 2) 撤销：先收集全部有效撤销，保证与文件顺序无关
        revoked: set = set()
        for e in known:
            if e.kind != KIND_REVOKE or e.id in duplicate_ids:
                continue
            target = by_id.get(e.target)
            if e.target in duplicate_ids or target is None:
                conflicts.append(
                    Conflict(
                        kind=REVOKE_UNKNOWN_TARGET,
                        severity=WARNING,
                        park=e.park or "(未知)",
                        resource=e.resource,
                        date=e.recorded_at.date(),
                        scope=SCOPE_ONWARD,
                        message=f"撤销事件 {e.id} 指向不存在的事件 {e.target!r}",
                        details={"event_id": e.id, "target": e.target},
                    )
                )
            elif target.kind not in BUSINESS_KINDS:
                conflicts.append(
                    Conflict(
                        kind=INVALID_TARGET_KIND,
                        severity=WARNING,
                        park=target.park or "(未知)",
                        resource=target.resource,
                        date=e.recorded_at.date(),
                        scope=SCOPE_ONWARD,
                        message=(
                            f"撤销事件 {e.id} 指向 {target.kind} 类事件 {e.target!r}，"
                            "仅业务事件可被撤销"
                        ),
                        details={"event_id": e.id, "target": e.target},
                    )
                )
            else:
                revoked.add(e.target)

        # 3) 更正：重述目标事件的生效时间与数量
        corrections: Dict[str, Event] = {}
        for e in known:
            if e.kind != KIND_CORRECT or e.id in duplicate_ids:
                continue
            target = by_id.get(e.target)
            if e.target in duplicate_ids or target is None:
                conflicts.append(
                    Conflict(
                        kind=CORRECT_UNKNOWN_TARGET,
                        severity=WARNING,
                        park=e.park or "(未知)",
                        resource=e.resource,
                        date=e.recorded_at.date(),
                        scope=SCOPE_ONWARD,
                        message=f"更正事件 {e.id} 指向不存在的事件 {e.target!r}",
                        details={"event_id": e.id, "target": e.target},
                    )
                )
            elif target.kind not in BUSINESS_KINDS:
                conflicts.append(
                    Conflict(
                        kind=INVALID_TARGET_KIND,
                        severity=WARNING,
                        park=target.park or "(未知)",
                        resource=target.resource,
                        date=e.recorded_at.date(),
                        scope=SCOPE_ONWARD,
                        message=(
                            f"更正事件 {e.id} 指向 {target.kind} 类事件 {e.target!r}，"
                            "仅业务事件可被更正"
                        ),
                        details={"event_id": e.id, "target": e.target},
                    )
                )
            elif e.target in revoked:
                conflicts.append(
                    Conflict(
                        kind=CORRECT_OF_REVOKED,
                        severity=WARNING,
                        park=target.park or "(未知)",
                        resource=target.resource,
                        date=e.recorded_at.date(),
                        scope=SCOPE_ONWARD,
                        message=f"更正事件 {e.id} 指向已撤销的事件 {e.target!r}，已忽略",
                        details={"event_id": e.id, "target": e.target},
                    )
                )
            elif e.target in corrections:
                prev = corrections[e.target]
                if (e.recorded_at, e.seq) > (prev.recorded_at, prev.seq):
                    corrections[e.target] = e
                conflicts.append(
                    Conflict(
                        kind=DUPLICATE_CORRECTION,
                        severity=WARNING,
                        park=target.park or "(未知)",
                        resource=target.resource,
                        date=e.recorded_at.date(),
                        scope=SCOPE_ONWARD,
                        message=(
                            f"事件 {e.target!r} 被多次更正，采用知晓时间最晚的 "
                            f"{corrections[e.target].id}"
                        ),
                        details={"event_id": e.id, "target": e.target},
                    )
                )
            else:
                corrections[e.target] = e

        # 4) 有效事件集 = 业务事件 - 重复 - 已撤销 - 被更正取代 + 更正版本
        effective: List[Event] = []
        emitted: set = set()
        for e in known:
            if e.kind not in BUSINESS_KINDS:
                continue
            if e.id in duplicate_ids or e.id in revoked or e.id in corrections:
                continue
            if e.id in emitted:
                continue  # 内容一致的幂等重投，只计一次
            emitted.add(e.id)
            effective.append(e)
        for target_id, corr in corrections.items():
            orig = by_id[target_id]
            effective.append(
                replace(
                    orig,
                    id=corr.id,
                    effective_at=corr.effective_at,
                    value=corr.value if corr.value is not None else orig.value,
                    recorded_at=corr.recorded_at,
                    note=corr.note or orig.note,
                    seq=corr.seq,
                )
            )

        result = (effective, conflicts)
        self._resolve_cache[key] = result
        return result

    # ------------------------------------------------------------------
    # 分量计算
    # ------------------------------------------------------------------
    @staticmethod
    def _capacity_at(
        pair_events: List[Event], day_end: datetime, park: str, resource: str, day: date
    ) -> Tuple[Optional[int], List[Conflict], List[str]]:
        caps = [
            e
            for e in pair_events
            if e.kind == KIND_CAPACITY and e.effective_at <= day_end
        ]
        if not caps:
            # 无任何有效容量台账记录：无法确定安装容量，阻塞而非按 0 处理
            return (
                None,
                [
                    Conflict(
                        kind=CAPACITY_MISSING,
                        severity=BLOCKING,
                        park=park,
                        resource=resource,
                        date=day,
                        scope=SCOPE_DATE,
                        message="该日期之前没有有效的容量台账记录，无法确定安装容量",
                        details={},
                    )
                ],
                [],
            )
        latest = max(e.effective_at for e in caps)
        winners = [e for e in caps if e.effective_at == latest]
        values = {e.value for e in winners}
        if len(values) > 1:
            return (
                None,
                [
                    Conflict(
                        kind=CAPACITY_CONFLICT,
                        severity=BLOCKING,
                        park=park,
                        resource=resource,
                        date=day,
                        scope=SCOPE_DATE,
                        message=(
                            f"容量台账在 {latest.isoformat()} 存在 "
                            f"{len(values)} 个冲突取值 {sorted(values)}，未静默选择"
                        ),
                        details={
                            "entries": [
                                {
                                    "id": e.id,
                                    "value": e.value,
                                    "source": e.source,
                                    "recorded_at": e.recorded_at.isoformat(),
                                }
                                for e in winners
                            ]
                        },
                    )
                ],
                [],
            )
        return winners[0].value, [], [e.id for e in winners]

    @staticmethod
    def _reserved_at(
        pair_events: List[Event], day_end: datetime, park: str, resource: str, day: date
    ) -> Tuple[int, List[Conflict], List[str]]:
        # 每张预留单取日终时点最新的一次状态上报
        latest_by_rid: Dict[str, Event] = {}
        for e in pair_events:
            if e.kind != KIND_RESERVATION or e.effective_at > day_end:
                continue
            cur = latest_by_rid.get(e.reservation_id)
            if cur is None or (e.effective_at, e.recorded_at, e.seq) >= (
                cur.effective_at,
                cur.recorded_at,
                cur.seq,
            ):
                latest_by_rid[e.reservation_id] = e

        total, conflicts, ids = 0, [], []
        for rid, e in sorted(latest_by_rid.items()):
            # 未标注状态的预留单按已生效（承诺）处理：宁可少报可用量，
            # 也不把已承诺资源再次对外承诺
            status = e.status or "approved"
            if status in COMMITTING_STATUSES:
                total += e.value
                ids.append(e.id)
            elif status in KNOWN_STATUSES:
                continue  # submitted/released/cancelled 等：不计入承诺
            else:
                total += e.value  # 无法识别的状态：保守计入并告警
                ids.append(e.id)
                conflicts.append(
                    Conflict(
                        kind=RESERVATION_STATUS_UNKNOWN,
                        severity=WARNING,
                        park=park,
                        resource=resource,
                        date=day,
                        scope=SCOPE_DATE,
                        message=(
                            f"预留单 {rid} 状态 {status!r} 无法识别，"
                            "已按保守口径计入承诺"
                        ),
                        details={"reservation_id": rid, "status": status},
                    )
                )
        return total, conflicts, ids

    @staticmethod
    def _outage_series(
        pair_events: List[Event], park: str, resource: str
    ) -> Tuple[List[datetime], List[int], List[Conflict], List[Event]]:
        """按时间顺序走查停机/恢复事件，返回 (时刻, 事后在停数) 序列。

        在停数不允许为负：恢复超出在停的部分被截断并登记冲突，
        从机制上防止重复回补与负容量。
        """
        evs = sorted(
            (
                e
                for e in pair_events
                if e.kind in (KIND_OUTAGE, KIND_OUTAGE_RECOVERY)
            ),
            key=lambda e: (e.effective_at, e.recorded_at, e.seq),
        )
        times: List[datetime] = []
        pools: List[int] = []
        conflicts: List[Conflict] = []
        pool = 0
        for e in evs:
            if e.kind == KIND_OUTAGE:
                pool += e.value
            else:
                if e.value > pool:
                    conflicts.append(
                        Conflict(
                            kind=RECOVERY_EXCEEDS_OUTAGE,
                            severity=WARNING,
                            park=park,
                            resource=resource,
                            date=e.effective_at.date(),
                            scope=SCOPE_ONWARD,
                            message=(
                                f"恢复事件 {e.id} 数量 {e.value} 超过当时在停 "
                                f"{pool}，超出部分未回补（防止重复回补/负容量）"
                            ),
                            details={
                                "event_id": e.id,
                                "recovery": e.value,
                                "open": pool,
                            },
                        )
                    )
                pool = max(0, pool - e.value)
            times.append(e.effective_at)
            pools.append(pool)
        if pool > 0 and evs:
            conflicts.append(
                Conflict(
                    kind=OPEN_OUTAGE,
                    severity=INFO,
                    park=park,
                    resource=resource,
                    date=evs[-1].effective_at.date(),
                    scope=SCOPE_ONWARD,
                    message=f"截至最后一条事件仍有 {pool} 未恢复",
                    details={"open": pool},
                )
            )
        return times, pools, conflicts, evs

    # ------------------------------------------------------------------
    # 日事实
    # ------------------------------------------------------------------
    def fact(
        self,
        park: str,
        resource: str,
        day: date,
        as_of: Optional[datetime] = None,
    ) -> FactRow:
        day_end = _day_end(day)
        day_start = _day_start(day)
        effective, resolution_conflicts = self._resolve(as_of)
        pair_events = [
            e for e in effective if e.park == park and e.resource == resource
        ]

        installed, cap_conflicts, cap_ids = self._capacity_at(
            pair_events, day_end, park, resource, day
        )
        reserved, res_conflicts, res_ids = self._reserved_at(
            pair_events, day_end, park, resource, day
        )
        times, pools, outage_conflicts, outage_events = self._outage_series(
            pair_events, park, resource
        )

        idx = bisect_right(times, day_end) - 1
        downtime = pools[idx] if idx >= 0 else 0
        i0 = bisect_left(times, day_start)
        i1 = bisect_right(times, day_end)
        carry = pools[i0 - 1] if i0 > 0 else 0
        peak = max([carry] + pools[i0:i1])

        pair_conflicts = [
            c
            for c in resolution_conflicts
            if c.park == park and c.resource == resource and c.applies_to(day)
        ]
        conflicts = cap_conflicts + res_conflicts + pair_conflicts + [
            c for c in outage_conflicts if c.applies_to(day)
        ]

        available: Optional[int] = None
        if installed is not None:
            available = installed - reserved - downtime
            if available < 0:
                conflicts.append(
                    Conflict(
                        kind=OVERCOMMITTED,
                        severity=WARNING,
                        park=park,
                        resource=resource,
                        date=day,
                        scope=SCOPE_DATE,
                        message=(
                            f"承诺 {reserved} + 停机 {downtime} 超出安装容量 "
                            f"{installed}，可用量为负（超承诺）"
                        ),
                        details={
                            "installed": installed,
                            "reserved": reserved,
                            "downtime": downtime,
                        },
                    )
                )

        blocked = any(c.severity == BLOCKING for c in conflicts)
        if blocked:
            available = None  # 阻塞可靠判断：不给出可用量数值
        warning = any(c.severity == WARNING for c in conflicts)
        status = (
            STATUS_BLOCKED
            if blocked
            else (STATUS_WARNING if warning else STATUS_OK)
        )

        return FactRow(
            date=day,
            park=park,
            resource=resource,
            installed_capacity=installed,
            reserved_qty=reserved,
            downtime_qty=downtime,
            downtime_peak_qty=peak,
            available_qty=available,
            status=status,
            conflicts=conflicts,
            inputs={
                "capacity": cap_ids,
                "reservations": res_ids,
                "outages": [
                    e.id for e in outage_events if e.effective_at <= day_end
                ],
            },
        )

    def facts(
        self, day: date, as_of: Optional[datetime] = None
    ) -> List[FactRow]:
        """某一日全部（园区, 资源类型）的日事实。"""
        day_end = _day_end(day)
        effective, _ = self._resolve(as_of)
        pairs = sorted(
            {
                (e.park, e.resource)
                for e in effective
                if e.park and e.effective_at <= day_end
            }
        )
        return [self.fact(park, resource, day, as_of) for park, resource in pairs]

    def conflicts(self, as_of: Optional[datetime] = None) -> List[Conflict]:
        """当前知晓时点下的全量冲突清单（跨日期去重，保留首发日期）。"""
        effective, resolution_conflicts = self._resolve(as_of)
        collected: List[Conflict] = list(resolution_conflicts)

        pairs = sorted({(e.park, e.resource) for e in effective if e.park})
        for park, resource in pairs:
            pair_events = [
                e for e in effective if e.park == park and e.resource == resource
            ]
            _, _, outage_conflicts, _ = self._outage_series(
                pair_events, park, resource
            )
            collected.extend(outage_conflicts)

        days = sorted({e.effective_at.date() for e in effective})
        for day in days:
            for row in self.facts(day, as_of):
                collected.extend(
                    c for c in row.conflicts if c.scope == SCOPE_DATE
                )

        merged: Dict[tuple, Conflict] = {}
        for c in collected:
            key = (c.kind, c.park, c.resource, c.message)
            if key in merged:
                merged[key].details["occurrences"] = (
                    merged[key].details.get("occurrences", 1) + 1
                )
            else:
                merged[key] = c
        return sorted(
            merged.values(),
            key=lambda c: (
                severity_rank(c.severity),
                c.date or date.min,
                c.park,
                c.resource,
            ),
        )
