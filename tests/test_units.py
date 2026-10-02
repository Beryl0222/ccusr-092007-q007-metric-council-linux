import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council import units
from metric_council.errors import ValidationError


class UnitTest(unittest.TestCase):
    def test_currency_scaling(self):
        self.assertAlmostEqual(
            units.convert(3.0, "CNY_100M_YUAN", "CNY_10k_YUAN"), 30000.0
        )
        self.assertAlmostEqual(
            units.convert(50000.0, "CNY_10k_YUAN", "CNY_100M_YUAN"), 5.0
        )
        self.assertAlmostEqual(units.convert(1.0, "CNY_YUAN", "CNY_10k_YUAN"), 0.0001)

    def test_percent_to_ratio(self):
        self.assertAlmostEqual(units.convert(50.0, "PERCENT", "RATIO"), 0.5)
        self.assertAlmostEqual(units.convert(2.0, "RATIO", "PERCENT"), 200.0)

    def test_cross_family_rejected(self):
        # 万元不能换算成人次（累计流量 vs 存量），防止口径被画等号
        with self.assertRaises(ValidationError):
            units.convert(1.0, "CNY_10k_YUAN", "PERSON_VISIT")
        with self.assertRaises(ValidationError):
            units.convert(1.0, "PERSON_VISIT", "PERSON")
        with self.assertRaises(ValidationError):
            units.convert(1.0, "CNY_10k_YUAN", "RATIO")

    def test_unknown_unit_rejected(self):
        with self.assertRaises(ValidationError):
            units.get("USD_DOLLAR")


if __name__ == "__main__":
    unittest.main()
