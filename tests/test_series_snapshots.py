import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council import units
from metric_council.errors import (
    DoubleCountError,
    SnapshotFrozenError,
    ValidationError,
)
from metric_council.geography import Geography
from metric_council.series import (
    ENTERPRISE_DETAIL_FIELDS,
    InputBatch,
    MetricSeries,
    RevisionReason,
    aggregate_children,
    check_no_double_count,
    fingerprint_subject,
)
from metric_council.snapshots import Publisher


def batch(bid="B1", when=datetime(2026, 2, 10)):
    return InputBatch(bid, "ds1", when, "p1")


def row(region, period, value, *, unit="CNY_10k_YUAN", level=1,
        as_of="2026-01-31", fps=(), covers=True):
    return {
        "region": region, "period": period, "as_of": as_of,
        "value": value, "unit": unit, "level": level,
        "subject_fingerprints": tuple(fps),
        "scope_covers_children": covers,
    }


class VersioningTest(unittest.TestCase):
    def setUp(self):
        self.series = MetricSeries("M", "CNY_10k_YUAN")

    def test_initial_then_revision_keeps_history(self):
        v1 = self.series.ingest_initial(
            batch("B1"), [row("330100", "2026-01", 10000.0)]
        )
        self.assertEqual(v1.version, 1)
        self.assertEqual(v1.reason, RevisionReason.INITIAL)

        v2 = self.series.revise(
            RevisionReason.SOURCE_CORRECTION,
            "来源方发现联票分润重复清分，更正",
            batch("B2", datetime(2026, 3, 1)),
            [row("330100", "2026-01", 9500.0)],
        )
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.parent_version, 1)
        # 历史版本原样保留
        self.assertEqual(
            self.series.version(1).point("330100", "2026-01").value, 10000.0
        )
        self.assertEqual(self.series.latest.version, 2)

    def test_revision_requires_reason_detail(self):
        self.series.ingest_initial(batch(), [row("r", "2026-01", 1.0)])
        with self.assertRaises(ValidationError):
            self.series.revise(
                RevisionReason.LATE_REPORT, "  ",
                batch("B2"), [row("r", "2026-01", 2.0)],
            )

    def test_three_revision_reasons(self):
        self.series.ingest_initial(batch(), [row("r", "p", 1.0)])
        for reason in (
            RevisionReason.SOURCE_CORRECTION,
            RevisionReason.BOUNDARY_CHANGE,
            RevisionReason.LATE_REPORT,
        ):
            n = self.series.latest.version
            v = self.series.revise(reason, "原因说明", batch(f"B{n+1}"),
                                   [row("r", "p", float(n + 1))])
            self.assertEqual(v.reason, reason)

    def test_initial_reason_not_allowed_for_revision(self):
        self.series.ingest_initial(batch(), [row("r", "p", 1.0)])
        with self.assertRaises(ValidationError):
            self.series.revise(
                RevisionReason.INITIAL, "x", batch("B2"), [row("r", "p", 2.0)]
            )

    def test_unit_conversion_on_ingest_and_cross_family_reject(self):
        # 亿元上报，标准单位万元，读取时换算
        self.series.ingest_initial(
            batch(), [row("r", "p", 1.5, unit="CNY_100M_YUAN")]
        )
        obs = self.series.latest.point("r", "p")
        self.assertEqual(obs.value, 1.5)
        self.assertAlmostEqual(
            self.series.canonical_value(obs), 15000.0
        )
        s2 = MetricSeries("M2", "CNY_10k_YUAN")
        with self.assertRaises(ValidationError):
            s2.ingest_initial(batch(), [row("r", "p", 1.0, unit="PERSON_VISIT")])

    def test_enterprise_detail_rows_rejected(self):
        leaked = row("r", "p", 1.0)
        leaked["enterprise_name"] = "某文化科技公司"
        with self.assertRaises(ValidationError) as ctx:
            self.series.ingest_initial(batch(), [leaked])
        self.assertIn("企业明细", str(ctx.exception))
        # 指纹可以进入，且不可逆
        fp = fingerprint_subject("91330000-credit-code")
        self.assertTrue(fp.startswith("fp:"))
        self.assertNotIn("91330000", fp)
        self.series.ingest_initial(batch("B9"), [row("r", "p", 1.0, fps=(fp,))])
        self.assertIn(fp, self.series.latest.observations[0].subject_fingerprints)

    def test_missing_field_rejected(self):
        bad = {"region": "r"}
        with self.assertRaises(ValidationError):
            self.series.ingest_initial(batch(), [bad])


class DoubleCountTest(unittest.TestCase):
    def setUp(self):
        self.g = Geography()
        self.g.add_region("330000", None)
        self.g.add_region("330100", "330000")
        self.g.add_region("330200", "330000")

    def test_parent_scope_plus_child_rejected(self):
        s = MetricSeries("M", "CNY_10k_YUAN")
        s.ingest_initial(batch(), [
            row("330000", "p", 18000.0, level=0, covers=True),
            row("330100", "p", 10000.0, level=1, covers=True),
        ])
        with self.assertRaises(DoubleCountError):
            check_no_double_count(s.latest, "p", self.g)

    def test_fingerprint_overlap_rejected(self):
        s = MetricSeries("M", "CNY_10k_YUAN")
        s.ingest_initial(batch(), [
            row("330000", "p", 18000.0, level=0, covers=False,
                fps=("fp:A", "fp:C")),
            row("330100", "p", 10000.0, level=1, fps=("fp:A",)),
        ])
        with self.assertRaises(DoubleCountError):
            check_no_double_count(s.latest, "p", self.g)

    def test_clean_children_pass_and_aggregate(self):
        s = MetricSeries("M", "CNY_10k_YUAN")
        s.ingest_initial(batch(), [
            row("330100", "p", 10000.0, level=1, covers=False, fps=("fp:A",)),
            row("330200", "p", 8000.0, level=1, covers=False, fps=("fp:B",)),
        ])
        check_no_double_count(s.latest, "p", self.g)
        total = aggregate_children(
            s.latest, "330000", "p", self.g,
            canonical_unit="CNY_10k_YUAN",
        )
        self.assertEqual(total, 18000.0)

    def test_aggregate_blocked_when_parent_total_and_children_coexist(self):
        s = MetricSeries("M", "CNY_10k_YUAN")
        s.ingest_initial(batch(), [
            row("330000", "p", 18000.0, level=0, covers=True),
            row("330100", "p", 10000.0, level=1, covers=False),
        ])
        with self.assertRaises(DoubleCountError):
            aggregate_children(s.latest, "330000", "p", self.g,
                               canonical_unit="CNY_10k_YUAN")

    def test_sibling_overlap_rejected(self):
        s = MetricSeries("M", "CNY_10k_YUAN")
        s.ingest_initial(batch(), [
            row("330100", "p", 10000.0, level=1, covers=False, fps=("fp:A",)),
            row("330200", "p", 8000.0, level=1, covers=False, fps=("fp:A",)),
        ])
        with self.assertRaises(DoubleCountError):
            aggregate_children(s.latest, "330000", "p", self.g,
                               canonical_unit="CNY_10k_YUAN")


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.g = Geography()
        self.pub = Publisher()
        self.s = MetricSeries("M", "CNY_10k_YUAN")
        self.pub.register_series(self.s)
        self.s.ingest_initial(batch("B1"), [
            row("330100", "2026-01", 10000.0),
            row("330200", "2026-01", 8000.0),
        ])

    def test_snapshot_freezes_value_then_revision_leaves_it_untouched(self):
        snap = self.pub.publish("R1", "一季度报告", {"M": ()})
        self.assertEqual(snap.value("M", "330100", "2026-01"), 10000.0)
        self.assertEqual(snap.provenance_key("M", "330100", "2026-01"), (1, "B1"))

        self.s.revise(
            RevisionReason.LATE_REPORT, "迟报补报",
            batch("B2", datetime(2026, 3, 5)),
            [row("330100", "2026-01", 12000.0),
             row("330200", "2026-01", 8000.0)],
        )
        # 已发布快照不变
        self.assertEqual(snap.value("M", "330100", "2026-01"), 10000.0)
        # 冻结值显式拒改
        with self.assertRaises(SnapshotFrozenError):
            self.pub.replace_frozen_value()

        view = self.pub.frozen_vs_latest(snap, "M", "330100", "2026-01")
        self.assertEqual(view["frozen"]["value"], 10000.0)
        self.assertEqual(view["latest"]["value"], 12000.0)
        self.assertEqual(view["delta"], 2000.0)
        self.assertTrue(view["changed"])
        self.assertEqual(
            [c["reason"] for c in view["revision_chain"]],
            ["initial", "late_report"],
        )

    def test_duplicate_snapshot_id_rejected(self):
        self.pub.publish("R1", "a", {"M": ()})
        with self.assertRaises(ValidationError):
            self.pub.publish("R1", "b", {"M": ()})

    def test_selection_filters_regions(self):
        snap = self.pub.publish("R1", "a", {"M": ("330100",)})
        snap.value("M", "330100", "2026-01")
        with self.assertRaises(ValidationError):
            snap.value("M", "330200", "2026-01")


if __name__ == "__main__":
    unittest.main()
