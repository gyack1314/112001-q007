"""运行事件机制测试：迟到、撤销、更正、重复恢复、异常事件流。"""
import unittest
from datetime import date, datetime

from src.conflicts import (
    CORRECT_UNKNOWN_TARGET,
    EVENT_ID_CONFLICT,
    RECOVERY_EXCEEDS_OUTAGE,
    REVOKE_UNKNOWN_TARGET,
)
from src.engine import STATUS_BLOCKED, STATUS_OK, STATUS_WARNING, CapacityEngine
from src.models import Event


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


def with_capacity(events):
    return [ev("C0", "capacity", value=100)] + events


class OutageIntervalTest(unittest.TestCase):
    def test_downtime_spans_until_recovery_day(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-03T09:00:00", value=10),
            ev("O2", "outage_recovery", effective="2026-04-05T11:00:00", value=10),
        ]))
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-03")).downtime_qty, 10)
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-04")).downtime_qty, 10)
        # 恢复当日日终已可用
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-05")).downtime_qty, 0)

    def test_late_recovery_rewrites_recompute_but_not_earlier_view(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-03T09:00:00", value=10),
            ev("O2", "outage_recovery", effective="2026-04-05T11:00:00",
               recorded="2026-04-08T09:00:00", value=10),  # 迟到 3 天
        ]))
        before = engine.fact("P1", "gpu", D("2026-04-05"), as_of=DT("2026-04-07T23:59:59"))
        self.assertEqual(before.downtime_qty, 10)
        self.assertEqual(before.available_qty, 90)
        after = engine.fact("P1", "gpu", D("2026-04-05"), as_of=DT("2026-04-08T10:00:00"))
        self.assertEqual(after.downtime_qty, 0)
        self.assertEqual(after.available_qty, 100)
        # 更早的知晓时点视图不被改写
        again = engine.fact("P1", "gpu", D("2026-04-05"), as_of=DT("2026-04-07T23:59:59"))
        self.assertEqual(again.downtime_qty, 10)


class RevokeAndCorrectTest(unittest.TestCase):
    def test_revoke_removes_effect_retroactively(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-10T10:00:00", value=8),
            ev("V1", "revoke", park=None, target="O1",
               effective="2026-04-12T09:00:00", recorded="2026-04-12T09:00:00"),
        ]))
        before = engine.fact("P1", "gpu", D("2026-04-10"), as_of=DT("2026-04-11T23:59:59"))
        self.assertEqual(before.downtime_qty, 8)
        after = engine.fact("P1", "gpu", D("2026-04-10"), as_of=DT("2026-04-12T10:00:00"))
        self.assertEqual(after.downtime_qty, 0)
        self.assertEqual(after.available_qty, 100)

    def test_correct_restates_quantity(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-15T10:00:00", value=3),
            ev("F1", "correct", park=None, target="O1", value=6,
               effective="2026-04-15T10:00:00", recorded="2026-04-16T08:00:00"),
        ]))
        before = engine.fact("P1", "gpu", D("2026-04-15"), as_of=DT("2026-04-15T23:59:59"))
        self.assertEqual(before.downtime_qty, 3)
        after = engine.fact("P1", "gpu", D("2026-04-15"), as_of=DT("2026-04-16T09:00:00"))
        self.assertEqual(after.downtime_qty, 6)
        self.assertEqual(after.available_qty, 94)

    def test_correct_can_move_effective_time(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-15T10:00:00", value=5),
            ev("F1", "correct", park=None, target="O1",
               effective="2026-04-16T10:00:00", recorded="2026-04-17T08:00:00"),
        ]))
        day15 = engine.fact("P1", "gpu", D("2026-04-15"), as_of=DT("2026-04-18"))
        day16 = engine.fact("P1", "gpu", D("2026-04-16"), as_of=DT("2026-04-18"))
        self.assertEqual(day15.downtime_qty, 0)   # 停机被移到 16 日
        self.assertEqual(day16.downtime_qty, 5)


class DuplicateRecoveryTest(unittest.TestCase):
    def test_double_recovery_does_not_replenish_twice(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-04T10:00:00", value=10),
            ev("O2", "outage_recovery", effective="2026-04-06T09:00:00", value=10),
            ev("O3", "outage_recovery", effective="2026-04-06T09:30:00", value=10),
        ]))
        row = engine.fact("P1", "gpu", D("2026-04-06"))
        self.assertEqual(row.downtime_qty, 0)          # 在停数不为负
        self.assertEqual(row.available_qty, 100)        # 不会回补出超额可用量
        self.assertEqual(row.status, STATUS_WARNING)
        kinds = {c.kind for c in row.conflicts}
        self.assertIn(RECOVERY_EXCEEDS_OUTAGE, kinds)

    def test_recovery_without_outage_is_flagged_and_clamped(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage_recovery", effective="2026-04-02T09:00:00", value=5),
        ]))
        row = engine.fact("P1", "gpu", D("2026-04-02"))
        self.assertEqual(row.downtime_qty, 0)
        self.assertEqual(row.available_qty, 100)
        self.assertTrue(any(c.kind == RECOVERY_EXCEEDS_OUTAGE for c in row.conflicts))


class AbnormalFlowTest(unittest.TestCase):
    def test_revoke_unknown_target_warns_without_changing_numbers(self):
        engine = CapacityEngine(with_capacity([
            ev("V1", "revoke", park=None, target="NOPE",
               effective="2026-04-02T09:00:00"),
        ]))
        row = engine.fact("P1", "gpu", D("2026-04-02"))
        self.assertEqual(row.available_qty, 100)
        conflicts = engine.conflicts()
        self.assertTrue(any(c.kind == REVOKE_UNKNOWN_TARGET for c in conflicts))

    def test_correct_unknown_target_warns(self):
        engine = CapacityEngine(with_capacity([
            ev("F1", "correct", park=None, target="NOPE", value=5,
               effective="2026-04-02T09:00:00"),
        ]))
        conflicts = engine.conflicts()
        self.assertTrue(any(c.kind == CORRECT_UNKNOWN_TARGET for c in conflicts))

    def test_same_id_different_content_blocks_and_excludes_both(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-02T10:00:00", value=5, seq=1),
            ev("O1", "outage", effective="2026-04-02T10:00:00", value=9, seq=2),
        ]))
        row = engine.fact("P1", "gpu", D("2026-04-02"))
        self.assertEqual(row.status, STATUS_BLOCKED)
        self.assertIsNone(row.available_qty)
        self.assertEqual(row.downtime_qty, 0)  # 两个版本都被剔除，未静默挑选
        self.assertTrue(any(c.kind == EVENT_ID_CONFLICT for c in row.conflicts))

    def test_same_id_identical_content_is_idempotent_retry(self):
        engine = CapacityEngine(with_capacity([
            ev("O1", "outage", effective="2026-04-02T10:00:00", value=5, seq=1),
            ev("O1", "outage", effective="2026-04-02T10:00:00", value=5, seq=2),
        ]))
        row = engine.fact("P1", "gpu", D("2026-04-02"))
        self.assertEqual(row.downtime_qty, 5)  # 只计一次
        self.assertEqual(row.status, STATUS_OK)


if __name__ == "__main__":
    unittest.main()
