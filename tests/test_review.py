import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from metric_council.errors import (
    ConflictOfInterestError,
    WorkflowError,
)
from metric_council.proposals import ProposalStatus
from metric_council.review import Vote
from _helpers import build_service_with_board, make_proposal


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.actors = build_service_with_board()

    def _submit(self):
        from _helpers import proposal_payload

        return self.svc.submit_proposal(
            self.actors["proposer"], proposal_payload()
        )

    def test_three_committees_approve(self):
        p = self._submit()
        self.svc.file_opinion(self.actors["s1"], p, "统计可行", Vote.APPROVE)
        self.svc.file_opinion(self.actors["f1"], p, "财政可行", Vote.APPROVE)
        self.svc.file_opinion(self.actors["i1"], p, "行业可行", Vote.APPROVE)
        out, case = self.svc.close_review(self.actors["s1"], p)
        self.assertEqual(out.status, ProposalStatus.APPROVED)
        self.assertTrue(case.closed)
        self.assertEqual(len(case.opinions), 3)

    def test_interested_reviewer_must_recuse(self):
        p = self._submit()
        self.svc.declare_interest(
            self.actors["s1"], p, "本人持有相关平台股份"
        )
        with self.assertRaises(ConflictOfInterestError):
            self.svc.file_opinion(self.actors["s1"], p, "想投赞成", Vote.APPROVE)

        # 回避后同委员会另一名专家仍可表决，审议正常关闭
        self.svc.file_opinion(self.actors["s2"], p, "统计可行", Vote.APPROVE)
        self.svc.file_opinion(self.actors["f1"], p, "财政可行", Vote.APPROVE)
        self.svc.file_opinion(self.actors["i1"], p, "行业可行", Vote.APPROVE)
        out, case = self.svc.close_review(self.actors["s2"], p)
        self.assertEqual(out.status, ProposalStatus.APPROVED)
        self.assertEqual(len(case.recusals), 1)
        self.assertEqual(case.recusals[0].relation, "本人持有相关平台股份")

    def test_cannot_recuse_after_voting(self):
        p = self._submit()
        self.svc.file_opinion(self.actors["s1"], p, "统计可行", Vote.APPROVE)
        with self.assertRaises(WorkflowError):
            self.svc.declare_interest(self.actors["s1"], p, "事后声明")

    def test_duplicate_vote_rejected(self):
        p = self._submit()
        self.svc.file_opinion(self.actors["s1"], p, "意见", Vote.APPROVE)
        with self.assertRaises(WorkflowError):
            self.svc.file_opinion(self.actors["s1"], p, "再投", Vote.APPROVE)

    def test_committee_split_becomes_dispute(self):
        # 统计、财政反对，行业赞成 → 委员会立场分裂 → DISPUTED
        p = self._submit()
        self.svc.file_opinion(self.actors["s1"], p, "口径不可比", Vote.REJECT)
        self.svc.file_opinion(self.actors["f1"], p, "财政不认可", Vote.REJECT)
        self.svc.file_opinion(self.actors["i1"], p, "行业确有需要", Vote.APPROVE)
        out, _ = self.svc.close_review(self.actors["s1"], p)
        self.assertEqual(out.status, ProposalStatus.DISPUTED)

    def test_all_three_reject(self):
        p = self._submit()
        self.svc.file_opinion(self.actors["s1"], p, "否", Vote.REJECT)
        self.svc.file_opinion(self.actors["f1"], p, "否", Vote.REJECT)
        self.svc.file_opinion(self.actors["i1"], p, "否", Vote.REJECT)
        out, _ = self.svc.close_review(self.actors["s1"], p)
        self.assertEqual(out.status, ProposalStatus.REJECTED)

    def test_committee_majority_rule(self):
        # 统计委员会两人：一赞成一反对，再由 s? —— 单独验证多数：
        # 这里 s1 赞成、s2 反对，统计委员会平局 = 弃权立场；
        # 财政、行业赞成 → 既非全赞成也非纯反对 → DISPUTED
        p = self._submit()
        self.svc.file_opinion(self.actors["s1"], p, "赞成", Vote.APPROVE)
        self.svc.file_opinion(self.actors["s2"], p, "反对", Vote.REJECT)
        self.svc.file_opinion(self.actors["f1"], p, "赞成", Vote.APPROVE)
        self.svc.file_opinion(self.actors["i1"], p, "赞成", Vote.APPROVE)
        out, case = self.svc.close_review(self.actors["s1"], p)
        self.assertEqual(out.status, ProposalStatus.DISPUTED)
        positions = self.svc.board.committee_positions(p.record_id)
        self.assertEqual(positions[self.svc.board.case_of(p.record_id).opinions[0].committee],
                         Vote.ABSTAIN)

    def test_quorum_failure_when_committee_silent(self):
        p = self._submit()
        # 财政委员会无人表决
        self.svc.file_opinion(self.actors["s1"], p, "赞成", Vote.APPROVE)
        self.svc.file_opinion(self.actors["i1"], p, "赞成", Vote.APPROVE)
        with self.assertRaises(WorkflowError):
            self.svc.close_review(self.actors["s1"], p)

    def test_double_close_rejected(self):
        p = self._submit()
        self.svc.file_opinion(self.actors["s1"], p, "ok", Vote.APPROVE)
        self.svc.file_opinion(self.actors["f1"], p, "ok", Vote.APPROVE)
        self.svc.file_opinion(self.actors["i1"], p, "ok", Vote.APPROVE)
        self.svc.close_review(self.actors["s1"], p)
        with self.assertRaises(WorkflowError):
            self.svc.close_review(self.actors["s1"], p)


if __name__ == "__main__":
    unittest.main()
