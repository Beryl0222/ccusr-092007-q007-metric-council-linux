"""审议流程：三委员会影响意见、利益回避与表决。

统计、财政、行业三个委员会分别就提案提出影响意见并表决。评议人与
提案存在利益关系时必须回避：回避被记录在案，若仍尝试表决则抛出
:class:`ConflictOfInterestError`，该票不计入任何结果。

裁决规则（结果写入提案状态机）：

* 三个委员会均以多数投赞成 → ``APPROVED``；
* 任一委员会多数反对且无委员会赞成 → ``REJECTED``；
* 委员会之间立场分裂（既有多数赞成又有多数反对）→ ``DISPUTED``，
  进入公开分歧，提案不得被静默合并或用于派生；
* 不足法定人数（委员会有表决权者全部回避）→ 审议不能关闭，抛错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .errors import (
    ConflictOfInterestError,
    ValidationError,
    WorkflowError,
)
from .proposals import MetricProposal, ProposalStatus


class Committee(str, Enum):
    STATISTICS = "statistics"   # 统计
    FISCAL = "fiscal"           # 财政
    INDUSTRY = "industry"       # 行业专家

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Committee.STATISTICS: "统计",
    Committee.FISCAL: "财政",
    Committee.INDUSTRY: "行业",
}


class Vote(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class Recusal:
    """一次回避记录。"""

    reviewer: str
    committee: Committee
    proposal_id: str
    relation: str
    declared_at: datetime


@dataclass(frozen=True)
class Opinion:
    """一名评议人提出的影响意见与表决。"""

    reviewer: str
    committee: Committee
    proposal_id: str
    impact: str
    """影响意见正文：对可比性、序列、财政或行业面的评估。"""

    vote: Vote
    filed_at: datetime


@dataclass
class ReviewCase:
    """一次提案审议的完整卷宗（进入血缘，冻结后不可改）。"""

    proposal_id: str
    opinions: list[Opinion] = field(default_factory=list)
    recusals: list[Recusal] = field(default_factory=list)
    closed: bool = False

    def opinion_index(self) -> dict[str, Opinion]:
        return {o.reviewer: o for o in self.opinions}

    def recused_reviewers(self) -> frozenset[str]:
        return frozenset(r.reviewer for r in self.recusals)

    def committee_votes(self, committee: Committee) -> list[Vote]:
        return [
            o.vote
            for o in self.opinions
            if o.committee is committee and o.vote is not Vote.ABSTAIN
        ]


class ReviewBoard:
    """登记评议人、利益关系，并管理逐提案的审议卷宗。"""

    def __init__(self) -> None:
        self._reviewers: dict[str, Committee] = {}
        # proposal_id -> {reviewer: relation}
        self._interests: dict[str, dict[str, str]] = {}
        self._cases: dict[str, ReviewCase] = {}

    # -- 人员与利益关系 ---------------------------------------------------

    def register_reviewer(self, reviewer: str, committee: Committee) -> None:
        if reviewer in self._reviewers:
            raise ValidationError(f"评议人已登记：{reviewer}")
        self._reviewers[reviewer] = committee

    def declare_interest(
        self, reviewer: str, proposal_id: str, relation: str
    ) -> Recusal:
        """登记利益关系；该评议人对该提案的表决资格被取消。"""

        committee = self._committee_of(reviewer)
        case = self._cases.setdefault(proposal_id, ReviewCase(proposal_id))
        if reviewer in case.opinion_index():
            raise WorkflowError(
                f"{reviewer} 已就提案 {proposal_id} 表决，不能事后声明回避"
            )
        recusal = Recusal(
            reviewer=reviewer,
            committee=committee,
            proposal_id=proposal_id,
            relation=relation,
            declared_at=datetime.now().astimezone(),
        )
        case.recusals.append(recusal)
        self._interests.setdefault(proposal_id, {})[reviewer] = relation
        return recusal

    def has_interest(self, reviewer: str, proposal_id: str) -> bool:
        return reviewer in self._interests.get(proposal_id, {})

    def case_of(self, proposal_id: str) -> ReviewCase:
        return self._cases.setdefault(proposal_id, ReviewCase(proposal_id))

    def _committee_of(self, reviewer: str) -> Committee:
        try:
            return self._reviewers[reviewer]
        except KeyError as exc:
            raise ValidationError(f"未登记的评议人：{reviewer}") from exc

    # -- 意见与表决 -------------------------------------------------------

    def file_opinion(
        self,
        proposal: MetricProposal,
        reviewer: str,
        impact: str,
        vote: Vote,
        *,
        now: datetime | None = None,
    ) -> Opinion:
        """提交影响意见并表决；应回避而未回避时拒绝该票。"""

        if proposal.status is not ProposalStatus.SUBMITTED:
            raise WorkflowError(
                f"提案 {proposal.record_id} 状态为 {proposal.status.value}，"
                "不处于审议中"
            )
        committee = self._committee_of(reviewer)
        case = self.case_of(proposal.record_id)

        relation = self._interests.get(proposal.record_id, {}).get(reviewer)
        if relation is not None or reviewer in case.recused_reviewers():
            relation = relation or "已声明利益关系"
            raise ConflictOfInterestError(reviewer, proposal.record_id, relation)

        if reviewer in case.opinion_index():
            raise WorkflowError(f"{reviewer} 已就该提案表决，不得重复投票")
        if not impact.strip():
            raise ValidationError("影响意见不能为空")

        opinion = Opinion(
            reviewer=reviewer,
            committee=committee,
            proposal_id=proposal.record_id,
            impact=impact,
            vote=vote,
            filed_at=now or datetime.now().astimezone(),
        )
        case.opinions.append(opinion)
        return opinion

    # -- 裁决 -------------------------------------------------------------

    def close_review(self, proposal: MetricProposal) -> tuple[MetricProposal, ReviewCase]:
        """汇审三个委员会的立场并推进提案状态。"""

        case = self.case_of(proposal.record_id)
        if case.closed:
            raise WorkflowError(f"提案 {proposal.record_id} 的审议已关闭")

        positions: dict[Committee, Vote] = {}
        for committee in Committee:
            voters = {
                o.reviewer
                for o in case.opinions
                if o.committee is committee
                and o.vote is not Vote.ABSTAIN
            }
            if not voters:
                raise WorkflowError(
                    f"{committee.label}委员会对提案 {proposal.record_id} "
                    "无有效表决（全员回避或未表决），不足法定人数"
                )
            votes = case.committee_votes(committee)
            approves = sum(1 for v in votes if v is Vote.APPROVE)
            rejects = sum(1 for v in votes if v is Vote.REJECT)
            if approves > rejects:
                positions[committee] = Vote.APPROVE
            elif rejects > approves:
                positions[committee] = Vote.REJECT
            else:
                positions[committee] = Vote.ABSTAIN

        n_approve = sum(1 for v in positions.values() if v is Vote.APPROVE)
        n_reject = sum(1 for v in positions.values() if v is Vote.REJECT)

        if n_approve == len(Committee):
            outcome = ProposalStatus.APPROVED
        elif n_approve == 0 and n_reject > 0:
            outcome = ProposalStatus.REJECTED
        else:
            # 立场分裂或夹杂弃权：进入公开分歧，不静默合并
            outcome = ProposalStatus.DISPUTED

        case.closed = True
        return proposal.with_status(outcome), case

    def committee_positions(
        self, proposal_id: str
    ) -> dict[Committee, Vote]:
        """只读查看各委员会立场（审议关闭后用于报告与血缘）。"""

        case = self.case_of(proposal_id)
        if not case.closed:
            raise WorkflowError("审议尚未关闭")
        result: dict[Committee, Vote] = {}
        for committee in Committee:
            votes = case.committee_votes(committee)
            approves = sum(1 for v in votes if v is Vote.APPROVE)
            rejects = sum(1 for v in votes if v is Vote.REJECT)
            result[committee] = (
                Vote.APPROVE
                if approves > rejects
                else Vote.REJECT
                if rejects > approves
                else Vote.ABSTAIN
            )
        return result
