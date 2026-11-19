"""输入加载校验测试：脏数据立即报错，不静默跳过。"""
import json
import tempfile
import unittest
from pathlib import Path

from src.loader import load_events

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "events.json"


def write_tmp(rows):
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    with tmp:
        json.dump(rows, tmp, ensure_ascii=False)
    return Path(tmp.name)


class LoaderTest(unittest.TestCase):
    def test_sample_file_loads(self):
        events = load_events(DATA_FILE)
        self.assertTrue(events)
        self.assertTrue(all(e.kind and e.effective_at for e in events))

    def test_missing_kind_raises(self):
        path = write_tmp([{"park": "P1", "effective_at": "2026-04-01T00:00:00", "value": 1}])
        with self.assertRaisesRegex(ValueError, "kind"):
            load_events(path)

    def test_missing_effective_at_raises(self):
        path = write_tmp([{"park": "P1", "kind": "capacity", "value": 1}])
        with self.assertRaisesRegex(ValueError, "effective_at"):
            load_events(path)

    def test_bad_datetime_raises(self):
        path = write_tmp([
            {"park": "P1", "kind": "capacity", "effective_at": "not-a-date", "value": 1}
        ])
        with self.assertRaisesRegex(ValueError, "ISO"):
            load_events(path)

    def test_revoke_requires_target(self):
        path = write_tmp([
            {"kind": "revoke", "effective_at": "2026-04-01T00:00:00"}
        ])
        with self.assertRaisesRegex(ValueError, "target"):
            load_events(path)

    def test_business_event_requires_park_and_value(self):
        path = write_tmp([
            {"kind": "capacity", "effective_at": "2026-04-01T00:00:00", "value": 10}
        ])
        with self.assertRaisesRegex(ValueError, "park"):
            load_events(path)
        path = write_tmp([
            {"kind": "outage", "park": "P1", "effective_at": "2026-04-01T00:00:00"}
        ])
        with self.assertRaisesRegex(ValueError, "value"):
            load_events(path)

    def test_recorded_at_defaults_to_effective_at(self):
        path = write_tmp([
            {"park": "P1", "kind": "capacity",
             "effective_at": "2026-04-01T00:00:00", "value": 10}
        ])
        (event,) = load_events(path)
        self.assertEqual(event.recorded_at, event.effective_at)

    def test_negative_value_raises(self):
        path = write_tmp([
            {"park": "P1", "kind": "outage",
             "effective_at": "2026-04-01T00:00:00", "value": -3}
        ])
        with self.assertRaisesRegex(ValueError, "非负整数"):
            load_events(path)

    def test_ids_are_auto_assigned_in_file_order(self):
        path = write_tmp([
            {"park": "P1", "kind": "capacity",
             "effective_at": "2026-04-01T00:00:00", "value": 10},
            {"park": "P1", "kind": "outage",
             "effective_at": "2026-04-02T00:00:00", "value": 1},
        ])
        events = load_events(path)
        self.assertEqual([e.id for e in events], ["E1", "E2"])


if __name__ == "__main__":
    unittest.main()
