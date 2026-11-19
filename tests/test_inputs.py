import json
from pathlib import Path
import unittest


class InputShapeTest(unittest.TestCase):
    def test_events_have_effective_time_and_kind(self):
        rows = json.loads((Path(__file__).parents[1] / "data" / "events.json").read_text(encoding="utf-8"))
        self.assertTrue(rows)
        self.assertTrue(all(row["effective_at"] and row["kind"] for row in rows))


if __name__ == "__main__":
    unittest.main()
