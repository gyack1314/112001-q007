"""预留单生命周期测试：生效与释放遵循业务状态。"""
import unittest
from datetime import date, datetime

from src.conflicts import RESERVATION_STATUS_UNKNOWN
from src.engine import STATUS_OK, STATUS_WARNING, CapacityEngine
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


CAP = [ev("C0", "capacity", value=100)]


class ReservationLifecycleTest(unittest.TestCase):
    def test_bare_reservation_counts_as_commitment(self):
        # 未标注状态的预留单按已生效处理：不把已承诺资源再次对外承诺
        engine = CapacityEngine(CAP + [
            ev("R1", "reservation", rid="R-1", value=30),
        ])
        row = engine.fact("P1", "gpu", D("2026-04-01"))
        self.assertEqual(row.reserved_qty, 30)
        self.assertEqual(row.available_qty, 70)

    def test_submitted_does_not_count_until_approved(self):
        engine = CapacityEngine(CAP + [
            ev("R1", "reservation", rid="R-1", status="submitted",
               effective="2026-04-01T09:00:00", value=30),
            ev("R2", "reservation", rid="R-1", status="approved",
               effective="2026-04-03T09:00:00", value=30),
        ])
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-02")).reserved_qty, 0)
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-03")).reserved_qty, 30)

    def test_released_and_cancelled_do_not_count(self):
        engine = CapacityEngine(CAP + [
            ev("R1", "reservation", rid="R-1", status="approved",
               effective="2026-04-01T09:00:00", value=30),
            ev("R2", "reservation", rid="R-1", status="released",
               effective="2026-04-05T18:00:00", value=30),
        ])
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-04")).reserved_qty, 30)
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-05")).reserved_qty, 0)

    def test_late_cancellation_only_changes_current_recompute(self):
        engine = CapacityEngine(CAP + [
            ev("R1", "reservation", rid="R-1", status="approved",
               effective="2026-04-03T09:00:00", value=25),
            ev("R2", "reservation", rid="R-1", status="cancelled",
               effective="2026-04-07T10:00:00", recorded="2026-04-09T09:00:00",
               value=25),
        ])
        # 撤销迟到两天：as-of 04-08 时 04-07 仍计入承诺
        early = engine.fact("P1", "gpu", D("2026-04-07"), as_of=DT("2026-04-08T23:59:59"))
        self.assertEqual(early.reserved_qty, 25)
        # as-of 04-09 起，同一业务日期重算为已释放
        late = engine.fact("P1", "gpu", D("2026-04-07"), as_of=DT("2026-04-09T10:00:00"))
        self.assertEqual(late.reserved_qty, 0)

    def test_unknown_status_counts_conservatively_and_warns(self):
        engine = CapacityEngine(CAP + [
            ev("R1", "reservation", rid="R-1", status="weird",
               effective="2026-04-01T09:00:00", value=20),
        ])
        row = engine.fact("P1", "gpu", D("2026-04-01"))
        self.assertEqual(row.reserved_qty, 20)  # 保守计入，不对外重复承诺
        self.assertEqual(row.status, STATUS_WARNING)
        self.assertTrue(
            any(c.kind == RESERVATION_STATUS_UNKNOWN for c in row.conflicts)
        )

    def test_multiple_reservations_sum(self):
        engine = CapacityEngine(CAP + [
            ev("R1", "reservation", rid="R-1", status="approved", value=20),
            ev("R2", "reservation", rid="R-2", status="active", value=15),
            ev("R3", "reservation", rid="R-3", status="pending", value=999),
        ])
        row = engine.fact("P1", "gpu", D("2026-04-01"))
        self.assertEqual(row.reserved_qty, 35)
        self.assertEqual(row.status, STATUS_OK)

    def test_reservation_before_first_status_event_does_not_count(self):
        engine = CapacityEngine(CAP + [
            ev("R1", "reservation", rid="R-1", status="approved",
               effective="2026-04-10T09:00:00", value=20),
        ])
        self.assertEqual(engine.fact("P1", "gpu", D("2026-04-09")).reserved_qty, 0)


if __name__ == "__main__":
    unittest.main()
