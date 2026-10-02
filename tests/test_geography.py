import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council.geography import BoundaryChange, Geography
from metric_council.errors import ValidationError


class GeographyTest(unittest.TestCase):
    def test_static_hierarchy(self):
        g = Geography()
        g.add_region("330000", None)
        g.add_region("330100", "330000")
        g.add_region("330106", "330100")
        self.assertEqual(g.parent_on("330106", date(2026, 1, 1)), "330100")
        self.assertEqual(
            g.path_on("330106", date(2026, 1, 1)),
            ("330000", "330100", "330106"),
        )
        self.assertTrue(g.related_on("330000", "330106", date(2026, 1, 1)))
        self.assertFalse(g.related_on("330106", "330000", date(2026, 1, 1)))

    def test_boundary_change_is_point_in_time(self):
        # 2026-06-01 起某功能区从 A 市划转 B 市；历史隶属可回放
        g = Geography()
        g.add_region("P", None)
        g.add_region("A", "P")
        g.add_region("B", "P")
        g.add_region("X", "A")
        g.record_change(
            BoundaryChange(
                effective_on=date(2026, 6, 1),
                child="X",
                old_parent="A",
                new_parent="B",
                note="功能区划转",
            )
        )
        self.assertEqual(g.parent_on("X", date(2026, 5, 31)), "A")
        self.assertEqual(g.parent_on("X", date(2026, 6, 1)), "B")
        self.assertTrue(g.related_on("A", "X", date(2026, 5, 31)))
        self.assertFalse(g.related_on("A", "X", date(2026, 6, 1)))
        self.assertEqual(len(g.changes()), 1)

    def test_unknown_region_rejected(self):
        g = Geography()
        with self.assertRaises(ValidationError):
            g.parent_on("999999", date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
