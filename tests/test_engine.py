"""日事实引擎的核心语义测试：分量合成、as-of 过滤、容量冲突、超承诺。"""
import unittest
from datetime import date, datetime
from pathlib import Path

from src.conflicts import (
    CAPACITY_CONFLICT,
    CAPACITY_MISSING,
    OVERCOMMITTED,
)
from src.engine import STATUS_BLOCKED, STATUS_OK, STATUS_WARNING, CapacityEngine
from src.loader import load_events
from src.models import Event

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "events.json"


def D(text):
    return date.fromisoformat(text)


def DT(text):
    return datetime.fromisoformat(text)


def ev(id, kind, park="P1", resource="gpu", effective="2026-04-01T00:00:00",
       recorded=None, value=None, rid=None, status=None, target=None, seq=0):
    return Event(
        id=id,
        kind=kind,
        park=park,
        resource=resource,
        effective_at=DT(effective),
        recorded_at=DT(recorded) if recorded else DT(effective),
        value=value,
        reservation_id=rid,
        status=status,
        target=target,
        seq=seq,
    )


class CompositionTest(unittest.TestCase):
    def test_available_equals_capacity_minus_reserved_minus_downtime(self):
        engine = CapacityEngine([
            ev("C1", "capacity", value=100),
            ev("R1", "reservation", effective="2026-04-02T08:00:00",
               rid="R-1", status="approved", value=40),
            ev("O1", "outage", effective="2026-04-02T10:00:00", value=10),
        ])
        row = engine.fact("P1", "gpu", D("2026-04-02"))
        self.assertEqual(row.installed_capacity, 100)
        self.assertEqual(row.reserved_qty, 40)
        self.assertEqual(row.downtime_qty, 10)
        self.assertEqual(row.available_qty, 50)
        self.assertEqual(row.status, STATUS_OK)

    def test_as_of_filters_records_not_yet_known(self):
        engine = CapacityEngine([
            ev("C1", "capacity", value=100,
               effective="2026-04-01T00:00:00", recorded="2026-04-05T09:00:00"),
        ])
        early = engine.fact("P1", "gpu", D("2026-04-02"), as_of=DT("2026-04-04T23:59:59"))
        self.assertIsNone(early.installed_capacity)
        self.assertEqual(early.status, STATUS_BLOCKED)  # 容量台账尚未知晓
        late = engine.fact("P1", "gpu", D("2026-04-02"), as_of=DT("2026-04-05T10:00:00"))
        self.assertEqual(late.installed_capacity, 100)
        self.assertEqual(late.status, STATUS_OK)

    def test_resource_types_form_independent_facts(self):
        engine = CapacityEngine([
            ev("C1", "capacity", resource="gpu", value=50),
            ev("C2", "capacity", resource="cpu", value=200),
            ev("R1", "reservation", resource="cpu", effective="2026-04-02T09:00:00",
               rid="R-1", status="approved", value=80),
        ])
        rows = {(r.park, r.resource): r for r in engine.facts(D("2026-04-02"))}
        self.assertEqual(rows[("P1", "gpu")].available_qty, 50)
        self.assertEqual(rows[("P1", "cpu")].available_qty, 120)

    def test_capacity_upgrade_applies_from_effective_time(self):
        engine = CapacityEngine([
            ev("C1", "capacity", value=50),
            ev("C2", "capacity", effective="2026-04-10T00:00:00", value=70),
        ])
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-09")).installed_capacity, 50)
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-10")).installed_capacity, 70)


class CapacityConflictTest(unittest.TestCase):
    def test_conflicting_capacity_blocks_without_picking_optimistic(self):
        engine = CapacityEngine([
            ev("C1", "capacity", value=60),
            ev("C2", "capacity", value=80),  # 同一生效时刻，不同取值
        ])
        row = engine.fact("P1", "gpu", D("2026-04-01"))
        self.assertEqual(row.status, STATUS_BLOCKED)
        self.assertIsNone(row.installed_capacity)
        self.assertIsNone(row.available_qty)
        conflict = next(c for c in row.conflicts if c.kind == CAPACITY_CONFLICT)
        values = {e["value"] for e in conflict.details["entries"]}
        self.assertEqual(values, {60, 80})  # 两个取值都呈现，未静默选择

    def test_identical_capacity_restatements_are_not_conflicts(self):
        engine = CapacityEngine([
            ev("C1", "capacity", value=100),
            ev("C2", "capacity", value=100),
        ])
        row = engine.fact("P1", "gpu", D("2026-04-01"))
        self.assertEqual(row.installed_capacity, 100)
        self.assertEqual(row.status, STATUS_OK)

    def test_missing_capacity_blocks_when_other_activity_exists(self):
        engine = CapacityEngine([
            ev("R1", "reservation", rid="R-1", status="approved", value=10),
        ])
        row = engine.fact("P1", "gpu", D("2026-04-01"))
        self.assertEqual(row.status, STATUS_BLOCKED)
        self.assertIsNone(row.available_qty)
        self.assertTrue(any(c.kind == CAPACITY_MISSING for c in row.conflicts))


class OvercommitTest(unittest.TestCase):
    def test_overcommit_keeps_negative_available_and_warns(self):
        engine = CapacityEngine([
            ev("C1", "capacity", value=50),
            ev("R1", "reservation", rid="R-1", status="approved", value=30),
            ev("R2", "reservation", rid="R-2", status="approved", value=25),
        ])
        row = engine.fact("P1", "gpu", D("2026-04-01"))
        self.assertEqual(row.available_qty, -5)  # 不截断，暴露缺口
        self.assertEqual(row.status, STATUS_WARNING)
        self.assertTrue(any(c.kind == OVERCOMMITTED for c in row.conflicts))


class SampleDataTest(unittest.TestCase):
    """基于 data/events.json 的端到端核对。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = CapacityEngine(load_events(DATA_FILE))

    def row(self, park, resource, day, as_of=None):
        if as_of and len(as_of) == 10:  # 裸日期按当日结束理解
            as_of = as_of + "T23:59:59"
        return self.engine.fact(park, resource, D(day), as_of and DT(as_of))

    def test_intraday_outage_shows_peak_but_recovered_by_day_end(self):
        row = self.row("P01", "default", "2026-04-02")
        self.assertEqual(row.installed_capacity, 100)
        self.assertEqual(row.reserved_qty, 30)
        self.assertEqual(row.downtime_qty, 0)       # 当日 12:00 已恢复
        self.assertEqual(row.downtime_peak_qty, 20)  # 但当日曾有 20 暂不可用
        self.assertEqual(row.available_qty, 70)

    def test_late_recovery_changes_recompute_only(self):
        early = self.row("P01", "default", "2026-04-05", as_of="2026-04-05")
        self.assertEqual((early.downtime_qty, early.available_qty), (15, 55))
        late = self.row("P01", "default", "2026-04-05", as_of="2026-04-08")
        self.assertEqual((late.downtime_qty, late.available_qty), (0, 70))

    def test_capacity_conflict_blocks_p03(self):
        row = self.row("P03", "gpu", "2026-04-02")
        self.assertEqual(row.status, STATUS_BLOCKED)
        self.assertIsNone(row.available_qty)
        self.assertEqual(row.reserved_qty, 10)  # 分量仍如实呈现

    def test_overcommit_on_p02_gpu(self):
        row = self.row("P02", "gpu", "2026-04-04")
        self.assertEqual(row.reserved_qty, 55)
        self.assertEqual(row.available_qty, -15)
        self.assertEqual(row.status, STATUS_WARNING)

    def test_cpu_resource_is_independent(self):
        row = self.row("P02", "cpu", "2026-04-02")
        self.assertEqual(row.available_qty, 120)
        self.assertEqual(row.status, STATUS_OK)


if __name__ == "__main__":
    unittest.main()
