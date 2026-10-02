"""访问控制、谱系追溯与并排对比测试。"""

import json
import tempfile
import unittest
from datetime import date, datetime, timezone, timedelta

from support import (
    NOW,
    approve,
    base_proposal,
    chair,
    decision_maker,
    editor,
    engineer,
    new_service_with_experts,
    submitter,
)

from metric_council.access import (
    P_DETAIL_READ,
    User,
    ensure_no_enterprise_detail,
)
from metric_council.errors import PermissionDenied
from metric_council.ingest import InputBatch, Observation
from metric_council.storage import EventStore

TZ = timezone(timedelta(hours=8))


def _disjoint(payload, record_id, code, industry, source_id, name):
    out = dict(payload)
    out.update(record_id=record_id, code=code, name=name,
               industries=[industry],
               data_sources=[{
                   "source_id": source_id, "name": name,
                   "granularity": "aggregated",
                   "reporting_lag_days": 30,
                   "industries": [industry]}])
    return out


def build_two_calibers():
    svc = new_service_with_experts()
    p1 = approve(svc, _disjoint(base_proposal(), "rec-a", "M_A", "R90",
                                "src-a", "口径甲"))
    p2 = approve(svc, _disjoint(base_proposal(), "rec-b", "M_B", "R89",
                                "src-b", "口径乙"))
    svc.set_region_parent(engineer(), "330100", "330000")
    svc.ingest_batch(engineer(), InputBatch(
        "ba", "src-a", datetime(2026, 4, 10, tzinfo=TZ), (
        Observation("330106", "2026-Q1", date(2026, 3, 31), "x1", "c",
                    90.0, "CNY_WAN", "county", "R90"),)), "M_A")
    svc.ingest_batch(engineer(), InputBatch(
        "bb", "src-b", datetime(2026, 4, 10, tzinfo=TZ), (
        Observation("330106", "2026-Q1", date(2026, 3, 31), "y1", "c",
                    700000.0, "CNY_YUAN", "county", "R89"),)), "M_B")
    svc.build_initial_series(engineer(), "M_A", datetime(2026, 5, 1, tzinfo=TZ))
    svc.build_initial_series(engineer(), "M_B", datetime(2026, 5, 1, tzinfo=TZ))
    snapshot = svc.freeze_report(
        editor(), "R1", "报告", ["M_A", "M_B"],
        datetime(2026, 5, 2, tzinfo=TZ))
    return svc, snapshot


class AccessControlTest(unittest.TestCase):
    def test_decision_maker_cannot_ingest_or_freeze(self):
        svc = new_service_with_experts()
        dm = decision_maker()
        batch = InputBatch("b", "src", NOW, ())
        with self.assertRaises(PermissionDenied):
            svc.ingest_batch(dm, batch, "M_A")
        with self.assertRaises(PermissionDenied):
            svc.freeze_report(dm, "R", "t", ["M_A"], NOW)
        with self.assertRaises(PermissionDenied):
            svc.submit_proposal(dm, base_proposal())

    def test_decision_maker_has_no_detail_permission(self):
        self.assertFalse(decision_maker().permits(P_DETAIL_READ))

    def test_submitter_cannot_vote(self):
        svc = new_service_with_experts()
        proposal = svc.submit_proposal(submitter(),
                                       {**base_proposal(), "status": "draft"})
        svc.open_review(chair(), proposal.record_id, NOW)
        with self.assertRaises(PermissionDenied):
            svc.add_opinion(submitter(), proposal.record_id, "越权", NOW)

    def test_payload_guard_blocks_enterprise_detail_for_maker(self):
        payload = {"region": "330106", "value": 10,
                   "rows": [{"enterprise_id": "E-9"}]}
        with self.assertRaises(PermissionDenied):
            ensure_no_enterprise_detail(decision_maker(), payload)
        # detail_auditor 允许。
        auditor = User("a", "核查", frozenset({"detail_auditor"}))
        ensure_no_enterprise_detail(auditor, payload)

    def test_unknown_role_rejected(self):
        with self.assertRaises(PermissionDenied):
            User("x", "x", frozenset({"minister"}))


class LineageTest(unittest.TestCase):
    def test_trace_from_chart_value_to_definition_batches_review(self):
        svc, snapshot = build_two_calibers()
        trace = svc.trace(decision_maker(), snapshot.snapshot_id,
                          "M_A", "330106", "2026-Q1")
        self.assertEqual(trace["chart_value"]["value"], 90.0)
        self.assertEqual(trace["definition"]["code"], "M_A")
        self.assertEqual(trace["input_batches"], ["ba"])
        self.assertIn("snapshot", trace)
        # 三组表决意见齐备。
        panels = {t["panel"] for t in trace["review"]["tallies"]}
        self.assertEqual(panels, {"statistics", "fiscal", "industry"})
        self.assertTrue(all(t["passed"] for t in trace["review"]["tallies"]))
        # 决策者视图中绝不含企业明细字段。
        ensure_no_enterprise_detail(decision_maker(), trace)
        serialized = json.dumps(trace, ensure_ascii=False)
        self.assertNotIn("enterprise_id", serialized)

    def test_trace_requires_report_and_lineage_permissions(self):
        svc, snapshot = build_two_calibers()
        with self.assertRaises(PermissionDenied):
            svc.trace(submitter(), snapshot.snapshot_id,
                      "M_A", "330106", "2026-Q1")


class CompareTest(unittest.TestCase):
    def test_side_by_side_converts_units_and_shows_delta(self):
        svc, snapshot = build_two_calibers()
        result = svc.compare(decision_maker(), ["M_A", "M_B"],
                             "330106", "2026-Q1",
                             snapshot_id=snapshot.snapshot_id)
        rows = {r["metric_code"]: r for r in result["calibers"]}
        # 乙口径 700000 元换算为 70 万元，与甲口径同单位并排。
        self.assertEqual(rows["M_A"]["value"], 90.0)
        self.assertEqual(rows["M_B"]["value"], 70.0)
        self.assertEqual(rows["M_B"]["delta_vs_first"], -20.0)
        self.assertEqual(result["unit"], "CNY_WAN")

    def test_compare_based_on_frozen_snapshot_not_latest(self):
        svc, snapshot = build_two_calibers()
        # 快照后追加数据并修订当前序列；对比仍应基于冻结值。
        svc.ingest_batch(engineer(), InputBatch(
            "ba2", "src-a", datetime(2026, 6, 1, tzinfo=TZ), (
            Observation("330106", "2026-Q1", date(2026, 3, 31), "x9", "c",
                        5.0, "CNY_WAN", "county", "R90",
                        submitted_at=datetime(2026, 6, 1, tzinfo=TZ)),)),
            "M_A")
        svc.revise_series(engineer(), "M_A", "late_report", "迟报补录",
                          datetime(2026, 6, 2, tzinfo=TZ))
        frozen = svc.compare(decision_maker(), ["M_A", "M_B"],
                             "330106", "2026-Q1",
                             snapshot_id=snapshot.snapshot_id)
        latest = svc.compare(decision_maker(), ["M_A", "M_B"],
                             "330106", "2026-Q1")
        self.assertEqual(
            next(r for r in frozen["calibers"] if r["metric_code"] == "M_A")
            ["value"], 90.0)
        self.assertEqual(
            next(r for r in latest["calibers"] if r["metric_code"] == "M_A")
            ["value"], 95.0)

    def test_compare_surfaces_public_disagreement(self):
        svc = new_service_with_experts()
        approve(svc, base_proposal())
        wide = {**base_proposal(),
                "record_id": "rec-wide", "code": "CULT_CONSUME_WIDE",
                "name": "宽口径", "definition": "含B2B全部流水",
                "dedup": {"identity_keys": ["region_code", "activity_id",
                                            "period", "channel"],
                          "hierarchy_rule": "bottom_up", "exclusions": []}}
        approve(svc, wide)
        svc.register_conflict(chair(), "rec-wide", "CULT_CONSUME", NOW)
        # 两者尚无序列，故直接核对公开分歧查询面。
        disagreements = svc.public_disagreements(decision_maker())
        self.assertEqual(len(disagreements), 1)
        self.assertEqual(set(disagreements[0]["proposal_ids"]),
                         {"sample-007", "rec-wide"})
        self.assertIn("重复计数", disagreements[0]["reason"])

    def test_compare_requires_two_metrics(self):
        svc, snapshot = build_two_calibers()
        with self.assertRaises(ValueError):
            svc.compare(decision_maker(), ["M_A"], "330106", "2026-Q1")


class EventStoreTest(unittest.TestCase):
    def test_events_append_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(f"{tmp}/events.jsonl")
            seen = []
            store.append("ping", {"v": 1})
            store.append("pong", {"v": 2})
            count = store.replay({
                "ping": lambda p: seen.append(("ping", p["v"])),
                "pong": lambda p: seen.append(("pong", p["v"])),
            })
            self.assertEqual(count, 2)
            self.assertEqual(seen, [("ping", 1), ("pong", 2)])

    def test_service_writes_audit_trail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = new_service_with_experts_with_log(f"{tmp}/events.jsonl")
            approve(svc, base_proposal())
            types = [e["type"] for e in
                     EventStore(f"{tmp}/events.jsonl").read_all()]
            self.assertIn("proposal_submitted", types)
            self.assertIn("vote_closed", types)


def new_service_with_experts_with_log(path):
    from metric_council import CouncilService
    svc = CouncilService(event_log=path)
    from support import EXPERTS
    for expert in EXPERTS:
        svc.register_expert(chair(), expert)
    return svc


if __name__ == "__main__":
    unittest.main()
