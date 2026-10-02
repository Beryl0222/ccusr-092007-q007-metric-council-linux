"""采集：单位归一、去重、跨层级重复、序列版本与冻结快照测试。"""

import unittest
from datetime import date, datetime, timezone, timedelta

from support import (
    approve,
    base_proposal,
    editor,
    engineer,
    new_service_with_experts,
)

from metric_council.errors import (
    DuplicationViolation,
    SnapshotError,
    VersioningError,
)
from metric_council.ingest import (
    REASON_LATE_REPORT,
    REASON_REGION_CHANGE,
    REASON_SOURCE_CORRECTION,
    InputBatch,
    Observation,
)
from metric_council.regions import RegionMapping

TZ = timezone(timedelta(hours=8))


def approved_service():
    svc = new_service_with_experts()
    approve(svc, base_proposal())
    svc.set_region_parent(engineer(), "330100", "330000")
    svc.set_region_parent(engineer(), "330106", "330100")
    return svc


def obs(region="330106", activity="a1", channel="offline", value=120.0,
        unit="CNY_WAN", industry="R90", period_end=date(2026, 3, 31),
        **kw):
    return Observation(region, "2026-Q1", period_end, activity, channel,
                       value, unit, "county", industry, **kw)


class IngestTest(unittest.TestCase):
    def test_unit_normalization(self):
        svc = approved_service()
        batch = InputBatch("b1", "ds-digital-exhibit",
                           datetime(2026, 4, 10, tzinfo=TZ),
                           (obs(value=500000.0, unit="CNY_YUAN"),))
        points = svc.ingest_batch(engineer(), batch, "CULT_CONSUME")
        self.assertEqual(points[0].value, 50.0)
        self.assertEqual(points[0].unit, "CNY_WAN")

    def test_cross_dimension_value_rejects_batch(self):
        svc = approved_service()
        batch = InputBatch("b1", "ds-digital-exhibit",
                           datetime(2026, 4, 10, tzinfo=TZ),
                           (obs(value=10.0, unit="PERSON_VISIT"),))
        with self.assertRaises(DuplicationViolation) as ctx:
            svc.ingest_batch(engineer(), batch, "CULT_CONSUME")
        self.assertEqual(ctx.exception.violations[0]["type"], "unit_error")
        # 整批拒收：没有任何点入库。
        self.assertEqual(svc.ingest.all_points(), [])

    def test_within_batch_duplicate_rejected(self):
        svc = approved_service()
        batch = InputBatch("b1", "ds-digital-exhibit",
                           datetime(2026, 4, 10, tzinfo=TZ),
                           (obs(activity="a1"), obs(activity="a1")))
        with self.assertRaises(DuplicationViolation) as ctx:
            svc.ingest_batch(engineer(), batch, "CULT_CONSUME")
        self.assertEqual(ctx.exception.violations[0]["type"],
                         "within_batch_duplicate")

    def test_cross_batch_duplicate_rejected(self):
        svc = approved_service()
        b1 = InputBatch("b1", "ds-digital-exhibit",
                        datetime(2026, 4, 10, tzinfo=TZ), (obs(),))
        b2 = InputBatch("b2", "ds-digital-exhibit",
                        datetime(2026, 4, 11, tzinfo=TZ), (obs(),))
        svc.ingest_batch(engineer(), b1, "CULT_CONSUME")
        with self.assertRaises(DuplicationViolation) as ctx:
            svc.ingest_batch(engineer(), b2, "CULT_CONSUME")
        self.assertEqual(ctx.exception.violations[0]["type"],
                         "cross_batch_duplicate")

    def test_cross_hierarchy_duplicate_rejected(self):
        svc = approved_service()
        b1 = InputBatch("b1", "ds-digital-exhibit",
                        datetime(2026, 4, 10, tzinfo=TZ), (obs(),))
        svc.ingest_batch(engineer(), b1, "CULT_CONSUME")
        city = Observation("330100", "2026-Q1", date(2026, 3, 31), "a1",
                           "offline", 120.0, "CNY_WAN", "city", "R90")
        b2 = InputBatch("b2", "ds-digital-exhibit",
                        datetime(2026, 4, 11, tzinfo=TZ), (city,))
        with self.assertRaises(DuplicationViolation) as ctx:
            svc.ingest_batch(engineer(), b2, "CULT_CONSUME")
        self.assertEqual(ctx.exception.violations[0]["type"],
                         "cross_hierarchy_duplicate")

    def test_industry_out_of_scope_rejected(self):
        svc = approved_service()
        batch = InputBatch("b1", "ds-digital-exhibit",
                           datetime(2026, 4, 10, tzinfo=TZ),
                           (obs(industry="Z99"),))
        with self.assertRaises(DuplicationViolation) as ctx:
            svc.ingest_batch(engineer(), batch, "CULT_CONSUME")
        self.assertEqual(ctx.exception.violations[0]["type"],
                         "industry_out_of_scope")

    def test_late_report_flagged_not_silently_rewritten(self):
        svc = approved_service()
        # 来源允许迟报 30 天；Q1 结束 3/31 + 30 = 4/30，6/1 报送为迟报。
        late = obs(activity="a9",
                   submitted_at=datetime(2026, 6, 1, tzinfo=TZ))
        batch = InputBatch("b-late", "ds-digital-exhibit",
                           datetime(2026, 6, 1, tzinfo=TZ), (late,))
        points = svc.ingest_batch(engineer(), batch, "CULT_CONSUME")
        self.assertTrue(points[0].late)
        self.assertIn(REASON_LATE_REPORT, points[0].reasons)


class SeriesVersionTest(unittest.TestCase):
    def _initial(self, svc):
        batch = InputBatch("b1", "ds-digital-exhibit",
                           datetime(2026, 4, 10, tzinfo=TZ), (
            obs(activity="a1", value=120.0),
            obs(activity="a2", channel="online", value=50.0),
        ))
        svc.ingest_batch(engineer(), batch, "CULT_CONSUME")
        return svc.build_initial_series(
            engineer(), "CULT_CONSUME", datetime(2026, 5, 1, tzinfo=TZ))

    def test_initial_series_aggregates_and_tracks_batches(self):
        svc = approved_service()
        v1 = self._initial(svc)
        self.assertEqual(v1.value("330106", "2026-Q1"), 170.0)
        self.assertEqual(v1.cell_batches[("330106", "2026-Q1")], ("b1",))
        self.assertEqual(v1.reason, "initial")

    def test_late_data_creates_versioned_revision_with_reason(self):
        svc = approved_service()
        v1 = self._initial(svc)
        snapshot = svc.freeze_report(
            editor(), "R1", "报告", ["CULT_CONSUME"],
            datetime(2026, 5, 2, tzinfo=TZ))

        late_batch = InputBatch("b2", "ds-digital-exhibit",
                                datetime(2026, 6, 1, tzinfo=TZ),
                                (obs(activity="a9", value=30.0,
                                     submitted_at=datetime(2026, 6, 1,
                                                            tzinfo=TZ)),))
        svc.ingest_batch(engineer(), late_batch, "CULT_CONSUME")
        v2 = svc.revise_series(
            engineer(), "CULT_CONSUME", REASON_LATE_REPORT, "Q1迟报补录",
            datetime(2026, 6, 2, tzinfo=TZ))

        self.assertEqual(v2.seq, 2)
        self.assertEqual(v2.reason, REASON_LATE_REPORT)
        self.assertEqual(v2.based_on_version, v1.version_id)
        self.assertEqual(v2.value("330106", "2026-Q1"), 200.0)
        self.assertIn(("330106", "2026-Q1"), v2.changed_cells)
        # 旧版本与已冻结快照不受影响。
        self.assertEqual(v1.value("330106", "2026-Q1"), 170.0)
        self.assertEqual(snapshot.value("CULT_CONSUME", "330106", "2026-Q1"),
                         170.0)

    def test_revision_requires_note(self):
        svc = approved_service()
        self._initial(svc)
        batch = InputBatch("b2", "ds-digital-exhibit",
                           datetime(2026, 6, 1, tzinfo=TZ),
                           (obs(activity="a9",
                                submitted_at=datetime(2026, 6, 1, tzinfo=TZ)),))
        svc.ingest_batch(engineer(), batch, "CULT_CONSUME")
        with self.assertRaises(VersioningError):
            svc.series.revise(
                "CULT_CONSUME", svc._approved_proposal("CULT_CONSUME"),
                svc.ingest.points("CULT_CONSUME"), REASON_LATE_REPORT,
                "   ", datetime(2026, 6, 2, tzinfo=TZ), "CULT_CONSUME-v2")

    def test_no_change_no_new_version(self):
        svc = approved_service()
        self._initial(svc)
        with self.assertRaises(VersioningError):
            svc.revise_series(
                engineer(), "CULT_CONSUME", REASON_SOURCE_CORRECTION,
                "无变化的更正", datetime(2026, 5, 3, tzinfo=TZ))

    def test_source_correction_replaces_old_batch_and_versions(self):
        svc = approved_service()
        v1 = self._initial(svc)
        fix = InputBatch("b1-fix", "ds-digital-exhibit",
                         datetime(2026, 5, 10, tzinfo=TZ), (
            obs(activity="a1", value=90.0),
            obs(activity="a2", channel="online", value=50.0),
        ))
        v2 = svc.correct_source(
            engineer(), "b1", fix, "CULT_CONSUME", "场馆复核更正 a1",
            datetime(2026, 5, 11, tzinfo=TZ))
        self.assertEqual(v2.reason, REASON_SOURCE_CORRECTION)
        self.assertEqual(v2.value("330106", "2026-Q1"), 140.0)
        self.assertEqual(v1.value("330106", "2026-Q1"), 170.0)

    def test_correction_must_come_from_same_source(self):
        svc = approved_service()
        self._initial(svc)
        other = InputBatch("b-fix", "ds-combo-ticket",
                           datetime(2026, 5, 10, tzinfo=TZ), (
            obs(activity="a1", industry="R89", channel="park"),))
        with self.assertRaises(Exception):
            svc.correct_source(
                engineer(), "b1", other, "CULT_CONSUME", "换来源",
                datetime(2026, 5, 11, tzinfo=TZ))

    def test_region_change_rekeys_and_freezes_remain(self):
        svc = approved_service()
        v1 = self._initial(svc)
        snapshot = svc.freeze_report(
            editor(), "R1", "报告", ["CULT_CONSUME"],
            datetime(2026, 5, 2, tzinfo=TZ))
        svc.register_region_mapping(
            engineer(),
            RegionMapping("330106", "330199", date(2026, 1, 1), "撤区调整"))
        v2 = svc.apply_region_change(
            engineer(), "CULT_CONSUME", "撤区调整",
            datetime(2026, 5, 15, tzinfo=TZ))
        self.assertEqual(v2.reason, REASON_REGION_CHANGE)
        self.assertIn(("330199", "2026-Q1"), v2.values)
        self.assertNotIn(("330106", "2026-Q1"), v2.values)
        # 已发布报告继续引用发布时的旧区划与旧值。
        self.assertEqual(snapshot.value("CULT_CONSUME", "330106", "2026-Q1"),
                         170.0)

    def test_snapshot_is_immutable(self):
        svc = approved_service()
        self._initial(svc)
        snapshot = svc.freeze_report(
            editor(), "R1", "报告", ["CULT_CONSUME"],
            datetime(2026, 5, 2, tzinfo=TZ))
        frozen = snapshot.values["CULT_CONSUME"][("330106", "2026-Q1")]
        # 后续迟报产生新版本。
        svc.ingest_batch(
            engineer(),
            InputBatch("b2", "ds-digital-exhibit",
                       datetime(2026, 6, 1, tzinfo=TZ),
                       (obs(activity="a9",
                            submitted_at=datetime(2026, 6, 1, tzinfo=TZ)),)),
            "CULT_CONSUME")
        svc.revise_series(
            engineer(), "CULT_CONSUME", REASON_LATE_REPORT, "迟报",
            datetime(2026, 6, 2, tzinfo=TZ))
        self.assertEqual(
            snapshot.values["CULT_CONSUME"][("330106", "2026-Q1")], frozen)
        with self.assertRaises(SnapshotError):
            snapshot.value("CULT_CONSUME", "330106", "2099-Q1")


if __name__ == "__main__":
    unittest.main()
