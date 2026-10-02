"""审议：回避、分组表决、公开分歧、派生指标测试。"""

import unittest

from support import (
    NOW,
    approve,
    base_proposal,
    chair,
    expert_user,
    new_service_with_experts,
    submitter,
)

from metric_council.errors import (
    CompatibilityConflictError,
    QuorumError,
    RecusalViolation,
    WorkflowError,
)
from metric_council.review import Expert
from metric_council.access import User


def open_one(svc, payload_overrides=None, record_id="sample-x1"):
    payload = base_proposal()
    payload["record_id"] = record_id
    if payload_overrides:
        payload.update(payload_overrides)
    payload["status"] = "draft"
    proposal = svc.submit_proposal(submitter(), payload)
    svc.open_review(chair(), proposal.record_id, NOW)
    return proposal


class RecusalTest(unittest.TestCase):
    def test_interested_expert_may_opine_but_cannot_vote(self):
        svc = new_service_with_experts()
        svc.register_expert(chair(), Expert(
            "e-i9", "利益行业专家", "industry",
            frozenset({"ds-platform-creator"})))
        proposal = open_one(svc)
        opinion = svc.add_opinion(
            expert_user("e-i9") if False else
            User("e-i9", "利益行业专家", frozenset({"expert_industry"}),
                 expert_id="e-i9"),
            proposal.record_id, "代表平台方的影响意见", NOW)
        self.assertTrue(opinion.recused)  # 意见被标记为回避，仍留痕
        with self.assertRaises(RecusalViolation):
            svc.cast_vote(
                User("e-i9", "利益行业专家", frozenset({"expert_industry"}),
                     expert_id="e-i9"),
                proposal.record_id, True, "赞成", NOW)

    def test_whole_panel_recused_blocks_ballot(self):
        svc = new_service_with_experts_with_industry_conflict()
        proposal = open_one(svc)
        # 统计、财政正常；行业组全员利益相关。
        for eid in ["e-s1", "e-s2", "e-f1", "e-f2", "e-i1", "e-i2"]:
            svc.add_opinion(expert_user(eid), proposal.record_id, "意见", NOW)
        for eid in ["e-s1", "e-s2", "e-f1", "e-f2"]:
            svc.cast_vote(expert_user(eid), proposal.record_id, True, "r", NOW)
        with self.assertRaises(QuorumError):
            svc.close_vote(chair(), proposal.record_id, "标题", NOW)

    def test_tie_vote_is_rejection_and_public_disagreement(self):
        svc = new_service_with_experts()
        proposal = open_one(svc, record_id="sample-tie")
        for eid in ["e-s1", "e-s2", "e-f1", "e-f2", "e-i1", "e-i2"]:
            svc.add_opinion(expert_user(eid), proposal.record_id, "意见", NOW)
        # 统计通过、财政平票、行业通过
        votes = {"e-s1": True, "e-s2": True, "e-f1": True, "e-f2": False,
                 "e-i1": True, "e-i2": True}
        for eid, ok in votes.items():
            svc.cast_vote(expert_user(eid), proposal.record_id, ok, "r", NOW)
        result = svc.close_vote(chair(), proposal.record_id, "财政口径分歧", NOW)
        self.assertFalse(result.approved)
        self.assertFalse(result.panel("fiscal").passed)
        self.assertEqual(svc.board.proposals[proposal.record_id].status,
                         "rejected")
        disagreement = svc.board.disagreement_for(proposal.record_id)
        self.assertIsNotNone(disagreement)
        self.assertIn("fiscal", disagreement.reason)

    def test_missing_vote_blocks_close(self):
        svc = new_service_with_experts()
        proposal = open_one(svc, record_id="sample-miss")
        for eid in ["e-s1", "e-s2", "e-f1", "e-f2", "e-i1", "e-i2"]:
            svc.add_opinion(expert_user(eid), proposal.record_id, "意见", NOW)
        for eid in ["e-s1", "e-s2", "e-f1", "e-f2", "e-i1"]:  # e-i2 未投
            svc.cast_vote(expert_user(eid), proposal.record_id, True, "r", NOW)
        with self.assertRaises(QuorumError):
            svc.close_vote(chair(), proposal.record_id, "t", NOW)


def new_service_with_experts_with_industry_conflict():
    svc = new_service_with_experts()
    # 让行业组两位专家都与平台来源存在利益关系。
    svc.board.experts["e-i1"] = Expert(
        "e-i1", "孙行业", "industry", frozenset({"ds-platform-creator"}))
    svc.board.experts["e-i2"] = Expert(
        "e-i2", "周行业", "industry", frozenset({"ds-platform-creator"}))
    return svc


class CompatibilityTest(unittest.TestCase):
    def test_disjoint_sources_form_derived_metric(self):
        svc = new_service_with_experts()
        p_digital = approve(svc, _override(base_proposal(), {
            "record_id": "rec-dig", "code": "CULT_DIGITAL",
            "industries": ["R90"],
            "data_sources": [{"source_id": "ds-digital-exhibit",
                              "name": "展陈", "granularity": "aggregated",
                              "reporting_lag_days": 30,
                              "industries": ["R90"]}]}))
        p_ticket = approve(svc, _override(base_proposal(), {
            "record_id": "rec-tk", "code": "CULT_TICKET",
            "industries": ["R89"],
            "data_sources": [{"source_id": "ds-combo-ticket",
                              "name": "联票", "granularity": "aggregated",
                              "reporting_lag_days": 45,
                              "industries": ["R89"]}]}))
        derived = svc.create_derived(
            chair(), "CULT_SUB", "展陈+联票",
            [p_digital.record_id, p_ticket.record_id], NOW)
        self.assertEqual(derived.parent_ids, ("rec-dig", "rec-tk"))
        self.assertEqual(derived.formula, "sum")
        # 两者单位同为 CNY_WAN，沿用父口径单位。
        self.assertEqual(derived.unit, "CNY_WAN")

    def test_conflicting_overlap_cannot_merge(self):
        svc = new_service_with_experts()
        p_base = approve(svc, base_proposal())
        alt = _override(base_proposal(), {
            "record_id": "rec-wide", "code": "CULT_CONSUME_WIDE",
            "name": "宽口径", "definition": "含B2B全部流水",
            "dedup": {"identity_keys": ["region_code", "activity_id",
                                        "period", "channel"],
                      "hierarchy_rule": "bottom_up", "exclusions": []}})
        p_alt = approve(svc, alt)
        with self.assertRaises(CompatibilityConflictError) as ctx:
            svc.create_derived(
                chair(), "BAD", "x",
                [p_base.record_id, p_alt.record_id], NOW)
        self.assertTrue(ctx.exception.reasons)

    def test_conflict_enters_public_disagreement_not_silent_merge(self):
        svc = new_service_with_experts()
        approve(svc, base_proposal())  # 既有口径 CULT_CONSUME 已生效
        alt = _override(base_proposal(), {
            "record_id": "rec-wide2", "code": "CULT_CONSUME_WIDE2",
            "name": "宽口径2", "definition": "含B2B全部流水",
            "dedup": {"identity_keys": ["region_code", "activity_id",
                                        "period", "channel"],
                      "hierarchy_rule": "bottom_up", "exclusions": []}})
        p_alt = approve(svc, alt)
        disagreement = svc.register_conflict(
            chair(), "rec-wide2", "CULT_CONSUME", NOW)
        self.assertEqual(set(disagreement.proposal_ids),
                         {"sample-007", "rec-wide2"})
        self.assertTrue(disagreement.reason)
        # 派生指标仍因冲突被拒绝。
        with self.assertRaises(CompatibilityConflictError):
            svc.create_derived(
                chair(), "BAD2", "x",
                ["sample-007", "rec-wide2"], NOW)

    def test_non_conflicting_pair_rejects_disagreement_registration(self):
        svc = new_service_with_experts()
        p_digital = approve(svc, _override(base_proposal(), {
            "record_id": "rec-dig2", "code": "CULT_TV2",
            "industries": ["R87"],
            "data_sources": [{"source_id": "ds-tv-license",
                              "name": "广电收视", "granularity": "aggregated",
                              "reporting_lag_days": 30,
                              "industries": ["R87"]}]}))
        # 兼容关系不应登记成分歧。
        with self.assertRaises(WorkflowError):
            svc.register_conflict(
                chair(), "rec-dig2", "CULT_CONSUME", NOW)


def _override(payload: dict, changes: dict) -> dict:
    out = dict(payload)
    out.update(changes)
    return out


if __name__ == "__main__":
    unittest.main()
