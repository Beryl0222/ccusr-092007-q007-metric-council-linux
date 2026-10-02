import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from metric_council.access import Action, Actor, Policy, Role
from metric_council.compare import compare_alternatives
from metric_council.errors import AccessDeniedError
from metric_council.proposals import (
    DedupBoundary,
    ProposalStatus,
)
from metric_council.review import Vote
from metric_council.series import InputBatch, RevisionReason
from _helpers import build_service_with_board, make_proposal, proposal_payload


class PolicyMatrixTest(unittest.TestCase):
    def test_enterprise_detail_denied_to_every_role(self):
        for role in Role:
            self.assertFalse(
                Policy.can(Actor("a", "a", role), Action.ENTERPRISE_DETAIL_READ),
                f"{role} 不应能接触企业明细",
            )

    def test_separation_of_duties(self):
        dm = Actor("dm", "决策者", Role.DECISION_MAKER)
        self.assertTrue(Policy.can(dm, Action.CHART_READ))
        self.assertTrue(Policy.can(dm, Action.LINEAGE_READ))
        self.assertFalse(Policy.can(dm, Action.PROPOSAL_SUBMIT))
        self.assertFalse(Policy.can(dm, Action.SNAPSHOT_PUBLISH))

        proposer = Actor("p", "业务处", Role.PROPOSER)
        self.assertTrue(Policy.can(proposer, Action.PROPOSAL_SUBMIT))
        self.assertFalse(Policy.can(proposer, Action.CHART_READ))
        self.assertFalse(Policy.can(proposer, Action.REVIEW_FILE))

        publisher = Actor("pub", "发布", Role.PUBLISHER)
        self.assertTrue(Policy.can(publisher, Action.SNAPSHOT_PUBLISH))
        self.assertFalse(Policy.can(publisher, Action.PROPOSAL_SUBMIT))

    def test_expert_limited_to_review(self):
        expert = Actor("s", "统计专家", Role.STATISTICS_REVIEWER)
        self.assertTrue(Policy.can(expert, Action.REVIEW_FILE))
        for denied in (
            Action.PROPOSAL_SUBMIT,
            Action.SNAPSHOT_PUBLISH,
            Action.LINEAGE_READ,
        ):
            self.assertFalse(Policy.can(expert, denied))

    def test_require_raises(self):
        with self.assertRaises(AccessDeniedError):
            Policy.require(
                Actor("dm", "d", Role.DECISION_MAKER), Action.PROPOSAL_SUBMIT
            )


def _approved_proposal(svc, actors, overrides):
    payload = proposal_payload(**overrides)
    p = svc.submit_proposal(actors["proposer"], payload)
    svc.file_opinion(actors["s1"], p, "统计意见", Vote.APPROVE)
    svc.file_opinion(actors["f1"], p, "财政意见", Vote.APPROVE)
    svc.file_opinion(actors["i1"], p, "行业意见", Vote.APPROVE)
    out, _ = svc.close_review(actors["s1"], p)
    return out


def _ingest(svc, actors, code, unit, rows, bid="B1"):
    s = svc.register_series(actors["proposer"], code, unit)
    svc.ingest_rows(
        actors["proposer"], s,
        InputBatch(bid, "ds1", datetime(2026, 2, 10), "p1"),
        rows,
    )
    return s


ROW = {
    "region": "330100", "period": "2026-01", "as_of": "2026-01-31",
    "value": 10000.0, "unit": "CNY_10k_YUAN", "level": 1,
}


class ServiceIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.actors = build_service_with_board()
        self.svc.geography.add_region("330000", None)
        self.svc.geography.add_region("330100", "330000")

    def _wide(self, status_overrides=None):
        return _approved_proposal(self.svc, self.actors, {})

    def test_submit_requires_proposer_role(self):
        with self.assertRaises(AccessDeniedError):
            self.svc.submit_proposal(
                self.actors["dm"], proposal_payload()
            )

    def test_full_lineage_chain_from_chart_value(self):
        p = self._wide()
        s = _ingest(self.svc, self.actors, "CUL_CONS_WIDE", "CNY_10k_YUAN", [dict(ROW)])

        # 迟报产生 v2
        self.svc.revise_rows(
            self.actors["proposer"], s, RevisionReason.LATE_REPORT,
            "平台 3 月补报 1 月创作者收入",
            InputBatch("B2", "ds1", datetime(2026, 3, 5), "p1"),
            [{**ROW, "value": 11500.0}],
        )
        snap = self.svc.publish_report(
            self.actors["publisher"], "R1", "正式监测报告",
            {"CUL_CONS_WIDE": ()},
        )

        # 决策者能取值、能追血缘
        value = self.svc.chart_value(
            self.actors["dm"], snap, "CUL_CONS_WIDE", "330100", "2026-01"
        )
        self.assertEqual(value, 11500.0)
        view = self.svc.chart_lineage(
            self.actors["dm"], snap, "CUL_CONS_WIDE", "330100", "2026-01"
        )
        self.assertEqual(view["value"]["frozen_value"], 11500.0)
        self.assertEqual(view["value"]["metric_version"], 2)
        self.assertEqual(view["value"]["batch_id"], "B2")
        self.assertEqual(view["definition"]["metric_code"], "CUL_CONS_WIDE")
        self.assertEqual(view["definition"]["dedup_boundary"]["window"], "month")
        self.assertEqual(len(view["definition"]["data_sources"]), 2)
        self.assertEqual(len(view["review"]["opinions"]), 3)
        self.assertEqual(
            [o["committee"] for o in view["review"]["opinions"]],
            ["statistics", "fiscal", "industry"],
        )
        self.assertEqual(
            [c["version"] for c in view["revision_chain"]], [1, 2]
        )

    def test_lineage_denied_to_proposer(self):
        self._wide()
        _ingest(self.svc, self.actors, "CUL_CONS_WIDE", "CNY_10k_YUAN", [dict(ROW)])
        snap = self.svc.publish_report(
            self.actors["publisher"], "R1", "r", {"CUL_CONS_WIDE": ()}
        )
        with self.assertRaises(AccessDeniedError):
            self.svc.chart_lineage(
                self.actors["proposer"], snap,
                "CUL_CONS_WIDE", "330100", "2026-01",
            )

    def test_recusal_visible_in_lineage(self):
        payload = proposal_payload()
        p = self.svc.submit_proposal(self.actors["proposer"], payload)
        self.svc.declare_interest(self.actors["s1"], p, "配偶在平台企业任职")
        self.svc.file_opinion(self.actors["s2"], p, "统计意见", Vote.APPROVE)
        self.svc.file_opinion(self.actors["f1"], p, "财政意见", Vote.APPROVE)
        self.svc.file_opinion(self.actors["i1"], p, "行业意见", Vote.APPROVE)
        out, _ = self.svc.close_review(self.actors["s2"], p)
        self.assertEqual(out.status, ProposalStatus.APPROVED)

        _ingest(self.svc, self.actors, "CUL_CONS_WIDE", "CNY_10k_YUAN", [dict(ROW)])
        snap = self.svc.publish_report(
            self.actors["publisher"], "R1", "r", {"CUL_CONS_WIDE": ()}
        )
        view = self.svc.chart_lineage(
            self.actors["dm"], snap, "CUL_CONS_WIDE", "330100", "2026-01"
        )
        recusals = view["review"]["recusals"]
        self.assertEqual(len(recusals), 1)
        self.assertEqual(recusals[0]["reviewer"], "s1")
        self.assertIn("配偶", recusals[0]["relation"])

    def test_compare_alternatives_side_by_side(self):
        # 宽口径（万元）
        self._wide()
        # 窄口径（人次，去重边界不同）——走完分裂审议，登记分歧
        narrow_payload = proposal_payload(
            record_id="sample-008",
            metric_code="CUL_CONS_NARROW",
            metric_name="文化消费（窄口径）",
            unit="PERSON_VISIT",
            dedup_boundary={"subject": "visit", "keys": ["ticket_id"],
                            "window": "day"},
        )
        pn = self.svc.submit_proposal(self.actors["proposer"], narrow_payload)
        self.svc.file_opinion(self.actors["s1"], pn, "反对", Vote.REJECT)
        self.svc.file_opinion(self.actors["f1"], pn, "反对", Vote.REJECT)
        self.svc.file_opinion(self.actors["i1"], pn, "赞成", Vote.APPROVE)
        pn, _ = self.svc.close_review(self.actors["s1"], pn)
        wide = self.svc.proposal(self.actors["dm"], "sample-007")
        self.svc.open_dispute(
            self.actors["s1"], "D1", "宽窄口径之争", wide, pn
        )

        _ingest(self.svc, self.actors, "CUL_CONS_WIDE", "CNY_10k_YUAN",
                [dict(ROW)], bid="B1")
        _ingest(self.svc, self.actors, "CUL_CONS_NARROW", "PERSON_VISIT",
                [{**ROW, "value": 500000.0, "unit": "PERSON_VISIT"}],
                bid="B2")
        snap = self.svc.publish_report(
            self.actors["publisher"], "R1", "对照",
            {"CUL_CONS_WIDE": (), "CUL_CONS_NARROW": ()},
        )

        view = self.svc.compare_calibers(
            self.actors["dm"], snap,
            ("CUL_CONS_WIDE", "CUL_CONS_NARROW"),
            "330100", "2026-01",
        )
        self.assertFalse(view["comparable_numerically"])
        self.assertEqual(view["columns"][0]["value"], 10000.0)
        self.assertEqual(view["columns"][1]["value"], 500000.0)
        self.assertTrue(view["columns"][1]["disputed"])
        # 分歧双方都被标注：宽口径虽已批准，但作为未决分歧当事方同样警示
        self.assertTrue(view["columns"][0]["disputed"])
        self.assertEqual(view["deltas_vs_first"], [])

    def test_compare_same_family_shows_delta_without_merging(self):
        self._wide()
        other = _approved_proposal(self.svc, self.actors, {
            "record_id": "sample-009",
            "metric_code": "DIGI_EXH",
            "metric_name": "数字展陈收入",
            "definition": "仅数字展陈部分",
        })
        _ingest(self.svc, self.actors, "CUL_CONS_WIDE", "CNY_10k_YUAN",
                [dict(ROW, value=10000.0)], bid="B1")
        _ingest(self.svc, self.actors, "DIGI_EXH", "CNY_10k_YUAN",
                [dict(ROW, value=3000.0)], bid="B3")
        snap = self.svc.publish_report(
            self.actors["publisher"], "R1", "对照",
            {"CUL_CONS_WIDE": (), "DIGI_EXH": ()},
        )
        view = self.svc.compare_calibers(
            self.actors["dm"], snap,
            ("CUL_CONS_WIDE", "DIGI_EXH"),
            "330100", "2026-01", common_unit="CNY_100M_YUAN",
        )
        self.assertTrue(view["comparable_numerically"])
        self.assertAlmostEqual(view["columns"][0]["value"], 1.0)
        self.assertAlmostEqual(view["columns"][1]["value"], 0.3)
        self.assertAlmostEqual(view["deltas_vs_first"][0]["absolute"], -0.7)
        self.assertIn("不作合并值", view["note"])


if __name__ == "__main__":
    unittest.main()
