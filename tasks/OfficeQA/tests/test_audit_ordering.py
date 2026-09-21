"""Regression for call counters beyond two/three-digit filename padding."""
from pathlib import Path
import unittest

import audit


class NumericRecordOrderTests(unittest.TestCase):
    def test_learner_crosses_99_without_reordering_history(self):
        paths = [Path(f"{n:02}.json") for n in (127, 10, 100, 9, 99)]
        self.assertEqual([int(p.stem) for p in audit.numeric_records(paths)], [9, 10, 99, 100, 127])
        self.assertNotEqual(sorted(paths), audit.numeric_records(paths))

    def test_actor_and_tool_counters_cross_999(self):
        for suffix in ("", ".json"):
            paths = [Path(f"{n:03}{suffix}") for n in (1000, 127, 9, 99, 999, 100, 10)]
            self.assertEqual([int(p.stem) for p in audit.numeric_records(iter(paths))],
                             [9, 10, 99, 100, 127, 999, 1000])


if __name__ == "__main__":
    unittest.main()
