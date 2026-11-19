"""日报快照测试：已发送版本不可变、版本递增、决策依据留存。"""
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from src.engine import CapacityEngine
from src.models import Event
from src.reports import ReportStore


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


def build_engine():
    return CapacityEngine([
        ev("C1", "capacity", value=100),
        ev("R1", "reservation", rid="R-1", status="approved",
           effective="2026-04-02T08:00:00", value=30),
        ev("O1", "outage", effective="2026-04-03T09:00:00", value=15),
        ev("O2", "outage_recovery", effective="2026-04-05T11:00:00",
           recorded="2026-04-08T09:00:00", value=15),  # 迟到恢复
    ])


class ReportImmutabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ReportStore(Path(self.tmp.name) / "reports.json")
        self.engine = build_engine()
        self.day = D("2026-04-05")

    def test_sent_report_keeps_original_decision_basis(self):
        as_of_send = DT("2026-04-05T23:59:59")
        rows_at_send = self.engine.facts(self.day, as_of_send)
        self.store.send(self.day, rows_at_send, data_as_of=as_of_send)

        # 迟到恢复到达后，当前重算结果改变
        recomputed = self.engine.fact("P1", "gpu", self.day, as_of=DT("2026-04-09"))
        self.assertEqual(recomputed.available_qty, 100 - 30 - 0)

        # 已发送日报仍保留发送时的事实版本
        sent = self.store.get(self.day)
        sent_row = next(r for r in sent.rows if r.park == "P1")
        self.assertEqual(sent_row.downtime_qty, 15)
        self.assertEqual(sent_row.available_qty, 100 - 30 - 15)
        self.assertEqual(sent.data_as_of, as_of_send)

    def test_inputs_lineage_is_stored_with_report(self):
        rows = self.engine.facts(self.day, DT("2026-04-05T23:59:59"))
        self.store.send(self.day, rows, data_as_of=DT("2026-04-05T23:59:59"))
        sent = self.store.get(self.day)
        row = next(r for r in sent.rows if r.park == "P1")
        self.assertEqual(row.inputs["capacity"], ["C1"])
        self.assertEqual(row.inputs["reservations"], ["R1"])
        self.assertEqual(row.inputs["outages"], ["O1"])

    def test_versions_accumulate_and_stay_retrievable(self):
        self.store.send(self.day, self.engine.facts(self.day, DT("2026-04-05T23:59:59")),
                        data_as_of=DT("2026-04-05T23:59:59"))
        self.store.send(self.day, self.engine.facts(self.day, DT("2026-04-09T23:59:59")),
                        data_as_of=DT("2026-04-09T23:59:59"))
        v1 = self.store.get(self.day, version=1)
        v2 = self.store.get(self.day, version=2)
        self.assertEqual(v1.rows[0].available_qty, 55)
        self.assertEqual(v2.rows[0].available_qty, 70)
        # 缺省取最新版本
        self.assertEqual(self.store.get(self.day).version, 2)
        self.assertEqual(len(self.store.list()), 2)

    def test_persistence_roundtrip(self):
        self.store.send(self.day, self.engine.facts(self.day, DT("2026-04-05T23:59:59")),
                        data_as_of=DT("2026-04-05T23:59:59"))
        reloaded = ReportStore(self.store.path)
        sent = reloaded.get(self.day)
        self.assertEqual(sent.rows[0].available_qty, 55)
        # 仅有 INFO 级提示（停机未恢复），无警告/阻塞
        non_info = [c for c in sent.rows[0].conflicts if c.severity != "INFO"]
        self.assertEqual(non_info, [])

    def test_get_missing_report_raises(self):
        with self.assertRaises(KeyError):
            self.store.get(D("2026-05-01"))


if __name__ == "__main__":
    unittest.main()
