"""指标口径审议。

规则（对应规划研究组的要求）：

1. 统计、财政、行业三个专家组分别提出"影响意见"并分别计票；
2. 与提案存在利益关系（关联来源企业/行业/提交部门）的专家，
   系统在其投票时强制回避，可提交书面意见但不计入表决；
3. 某组因回避后无人可投票，构成法定人数不足，表决不能成立；
4. 口径**兼容**的定义可形成派生指标（如三类收入相加为总消费）；
   口径**冲突**的定义只能登记为"公开分歧"，任何流程都不得静默合并；
5. 表决未通过同样沉淀为公开分歧，保留各组理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .errors import (
    CompatibilityConflictError,
    QuorumError,
    RecusalViolation,
    WorkflowError,
)
from .proposals import (
    PANELS,
    ST_APPROVED,
    ST_DRAFT,
    ST_REJECTED,
    ST_REVIEW,
    EffectiveRange,
    MetricProposal,
)
from .units import get as get_unit

# 兼容关系判定结果标签。
REL_DISJOINT = "compatible_disjoint"   # 覆盖互斥，可相加
REL_NESTED = "compatible_nested"       # 一方显式剔除另一方口径，可派生
REL_CONFLICT = "conflict"              # 覆盖重叠且无剔除声明，禁止合并


@dataclass(frozen=True)
class Expert:
    expert_id: str
    name: str
    panel: str                       # statistics / fiscal / industry
    interests: frozenset[str] = frozenset()  # 利益关系标识：来源/行业/部门代码

    def __post_init__(self) -> None:
        if self.panel not in PANELS:
            raise ValueError(f"未知专家组：{self.panel}")


@dataclass(frozen=True)
class Opinion:
    expert_id: str
    panel: str
    impact: str                      # 影响意见：对可比性、财政、行业的影响判断
    recused: bool                    # 因利益回避时为 True
    created_at: datetime


@dataclass(frozen=True)
class Vote:
    expert_id: str
    panel: str
    approve: bool
    rationale: str
    created_at: datetime


@dataclass(frozen=True)
class PanelTally:
    panel: str
    eligible: int
    votes_for: int
    votes_against: int
    recused: int

    @property
    def passed(self) -> bool:
        # 有效票中多数赞成；平票视为未通过。
        cast = self.votes_for + self.votes_against
        return cast > 0 and self.votes_for > self.votes_against


@dataclass(frozen=True)
class BallotResult:
    proposal_id: str
    approved: bool
    tallies: tuple[PanelTally, ...]
    decided_at: datetime

    def panel(self, panel: str) -> PanelTally:
        return next(t for t in self.tallies if t.panel == panel)


@dataclass(frozen=True)
class PublicDisagreement:
    """公开分歧：永远保留、不可被合并消灭。"""

    disagreement_id: str
    title: str
    proposal_ids: tuple[str, ...]
    reason: str
    detail: dict
    created_at: datetime


@dataclass(frozen=True)
class DerivedMetric:
    """兼容定义形成的派生指标，记录父口径与合成方式。"""

    code: str
    name: str
    parent_ids: tuple[str, ...]
    formula: str                     # 目前支持 sum
    unit: str                        # 归一后的基准单位
    effective: EffectiveRange
    created_at: datetime

    def __post_init__(self) -> None:
        if self.formula != "sum":
            raise CompatibilityConflictError(f"暂不支持的派生方式：{self.formula}")


def has_conflict(expert: Expert, proposal: MetricProposal) -> bool:
    """判断专家与提案是否存在利益关系。

    利益标识与提案的来源 id、适用行业、提交部门任一重合即构成回避情形。
    """
    proposal_markers = set(proposal.industries)
    proposal_markers.add(proposal.submitting_dept)
    proposal_markers.update(d.source_id for d in proposal.data_sources)
    return bool(expert.interests & proposal_markers)


def conflict_reasons(expert: Expert, proposal: MetricProposal) -> list[str]:
    reasons: list[str] = []
    if expert.interests & set(proposal.industries):
        reasons.append(f"适用行业 {sorted(expert.interests & set(proposal.industries))}")
    src_markers = expert.interests & {d.source_id for d in proposal.data_sources}
    if src_markers:
        reasons.append(f"数据来源 {sorted(src_markers)}")
    if proposal.submitting_dept in expert.interests:
        reasons.append(f"提交部门 {proposal.submitting_dept}")
    return reasons


def compatibility_report(a: MetricProposal, b: MetricProposal) -> tuple[str, list[str]]:
    """判定两份定义的口径关系，返回 (关系标签, 原因列表)。"""
    reasons: list[str] = []
    dim_a = get_unit(a.unit).dimension
    dim_b = get_unit(b.unit).dimension
    if dim_a != dim_b:
        reasons.append(f"量纲不同：{a.code} 为 {dim_a}，{b.code} 为 {dim_b}")
        return REL_CONFLICT, reasons

    shared_industries = set(a.industries) & set(b.industries)
    shared_sources = {d.source_id for d in a.data_sources} & {
        d.source_id for d in b.data_sources
    }
    overlapping_period = a.effective.overlaps(b.effective)

    # 互斥覆盖：来源与行业均不重叠 → 同一笔钱不可能被两边各计一次。
    if not shared_sources and not shared_industries:
        return REL_DISJOINT, ["来源与适用行业均不重叠，覆盖互斥"]

    # 嵌套覆盖：同一时期、同一行业/来源上，一方在去重边界中显式剔除
    # 另一方指标代码代表的口径，则不会重复计数。
    if overlapping_period and (b.code in a.dedup.exclusions or a.code in b.dedup.exclusions):
        return REL_NESTED, ["一方已在去重边界显式剔除另一方口径"]

    if not overlapping_period:
        reasons.append("生效区间不重叠，属于不同时期的替代口径，不能合并为同一序列")
    if shared_industries:
        reasons.append(f"适用行业重叠：{sorted(shared_industries)}")
    if shared_sources:
        reasons.append(f"数据来源重叠：{sorted(shared_sources)}")
    reasons.append("重叠覆盖且无任一方声明剔除，存在重复计数风险")
    return REL_CONFLICT, reasons


def derived_effective_range(parents: list[MetricProposal]) -> EffectiveRange:
    start = max(p.effective.start for p in parents)
    ends = [p.effective.end for p in parents if p.effective.end is not None]
    return EffectiveRange(start=start, end=min(ends) if ends else None)


class ReviewBoard:
    """审议委员会：登记专家、归集意见、执行回避与分组表决。"""

    def __init__(self) -> None:
        self.proposals: dict[str, MetricProposal] = {}
        self.experts: dict[str, Expert] = {}
        self.opinions: dict[str, list[Opinion]] = {}
        self.votes: dict[str, list[Vote]] = {}
        self.ballots: dict[str, BallotResult] = {}
        self.disagreements: list[PublicDisagreement] = []
        self.derived: dict[str, DerivedMetric] = {}

    # —— 登记 ——

    def register_expert(self, expert: Expert) -> None:
        self.experts[expert.expert_id] = expert

    def submit(self, proposal: MetricProposal) -> None:
        if proposal.record_id in self.proposals:
            raise WorkflowError(f"提案已存在：{proposal.record_id}")
        self.proposals[proposal.record_id] = proposal
        self.opinions[proposal.record_id] = []
        self.votes[proposal.record_id] = []

    def open_review(self, proposal_id: str) -> MetricProposal:
        proposal = self._require(proposal_id)
        if proposal.status != ST_DRAFT:
            raise WorkflowError(f"提案 {proposal_id} 当前状态 {proposal.status}，不能开启审议")
        proposal = proposal.with_status(ST_REVIEW)
        self.proposals[proposal_id] = proposal
        return proposal

    # —— 意见与表决 ——

    def add_opinion(self, proposal_id: str, expert_id: str, impact: str,
                    now: datetime) -> Opinion:
        proposal = self._require(proposal_id)
        expert = self._expert(expert_id)
        recused = has_conflict(expert, proposal)
        opinion = Opinion(
            expert_id=expert_id, panel=expert.panel, impact=impact,
            recused=recused, created_at=now,
        )
        self.opinions[proposal_id].append(opinion)
        return opinion

    def cast_vote(self, proposal_id: str, expert_id: str, approve: bool,
                  rationale: str, now: datetime) -> Vote:
        proposal = self._require(proposal_id)
        if proposal.status != ST_REVIEW:
            raise WorkflowError(f"提案 {proposal_id} 不在审议中，不能投票")
        expert = self._expert(expert_id)
        if has_conflict(expert, proposal):
            raise RecusalViolation(
                f"专家 {expert_id} 与提案 {proposal_id} 存在利益关系"
                f"（{'; '.join(conflict_reasons(expert, proposal))}），必须回避表决"
            )
        votes = self.votes[proposal_id]
        if any(v.expert_id == expert_id for v in votes):
            raise WorkflowError(f"专家 {expert_id} 已投过票")
        vote = Vote(expert_id, expert.panel, approve, rationale, now)
        votes.append(vote)
        return vote

    def close_vote(self, proposal_id: str, disagreement_title: str,
                   now: datetime) -> BallotResult:
        proposal = self._require(proposal_id)
        if proposal.status != ST_REVIEW:
            raise WorkflowError(f"提案 {proposal_id} 不在审议中")

        tallies: list[PanelTally] = []
        for panel in PANELS:
            members = [e for e in self.experts.values() if e.panel == panel]
            eligible = [e for e in members if not has_conflict(e, proposal)]
            if not eligible:
                raise QuorumError(
                    f"{panel} 组全体成员与提案 {proposal_id} 存在利益关系，"
                    "无有效表决人，表决不能成立"
                )
            panel_votes = [v for v in self.votes[proposal_id] if v.panel == panel]
            voted_ids = {v.expert_id for v in panel_votes}
            missing = [e.expert_id for e in eligible if e.expert_id not in voted_ids]
            if missing:
                raise QuorumError(f"{panel} 组尚有应投票专家未投票：{missing}")
            recused = sum(1 for e in members if has_conflict(e, proposal))
            tallies.append(PanelTally(
                panel=panel,
                eligible=len(eligible),
                votes_for=sum(1 for v in panel_votes if v.approve),
                votes_against=sum(1 for v in panel_votes if not v.approve),
                recused=recused,
            ))

        approved = all(t.passed for t in tallies)
        result = BallotResult(proposal_id, approved, tuple(tallies), now)
        self.ballots[proposal_id] = result
        self.proposals[proposal_id] = proposal.with_status(
            ST_APPROVED if approved else ST_REJECTED
        )
        if not approved:
            failed = [t.panel for t in tallies if not t.passed]
            self.disagreements.append(PublicDisagreement(
                disagreement_id=f"dis-{proposal_id}",
                title=disagreement_title,
                proposal_ids=(proposal_id,),
                reason=f"分组表决未通过：{', '.join(failed)} 组未达多数",
                detail={"tallies": [t.__dict__ for t in tallies],
                        "opinions": [o.__dict__ for o in self.opinions[proposal_id]]},
                created_at=now,
            ))
        return result

    # —— 派生指标与公开分歧 ——

    def create_derived(self, code: str, name: str, parent_ids: list[str],
                       now: datetime) -> DerivedMetric:
        parents = [self._require(pid) for pid in parent_ids]
        for p in parents:
            if p.status != ST_APPROVED:
                raise WorkflowError(
                    f"只有通过审议的口径可形成派生指标，{p.record_id} 状态为 {p.status}"
                )
        for a, b in _pairs(parents):
            relation, reasons = compatibility_report(a, b)
            if relation == REL_CONFLICT:
                raise CompatibilityConflictError(
                    f"{a.code} 与 {b.code} 口径冲突，不能合并", reasons=reasons
                )
        dimension = get_unit(parents[0].unit).dimension
        parent_units = {p.unit for p in parents}
        if len(parent_units) == 1:
            # 父口径单位一致：直接沿用。
            derived_unit = parents[0].unit
        else:
            # 单位不同：派生序列在计算时须各自换算到量纲基准单位。
            from .units import base_unit
            derived_unit = base_unit(dimension).code
        derived = DerivedMetric(
            code=code, name=name, parent_ids=tuple(parent_ids), formula="sum",
            unit=derived_unit,
            effective=derived_effective_range(parents), created_at=now,
        )
        self.derived[code] = derived
        return derived

    def register_conflict(self, proposal: MetricProposal, existing_code: str,
                          now: datetime) -> PublicDisagreement:
        """替代口径与既有定义冲突时，登记公开分歧，绝不静默合并。"""
        existing = next(
            (p for p in self.proposals.values() if p.code == existing_code), None
        )
        if existing is None:
            raise WorkflowError(f"既有指标 {existing_code} 不存在")
        relation, reasons = compatibility_report(existing, proposal)
        if relation != REL_CONFLICT:
            raise WorkflowError(
                f"{existing.code} 与 {proposal.code} 关系为 {relation}，"
                "应通过派生指标而非公开分歧处理"
            )
        disagreement = PublicDisagreement(
            disagreement_id=f"dis-alt-{proposal.record_id}",
            title=f"{existing.code} 替代口径分歧",
            proposal_ids=(existing.record_id, proposal.record_id),
            reason="；".join(reasons),
            detail={"relation": relation, "reasons": reasons,
                    "existing": existing.record_id, "alternative": proposal.record_id},
            created_at=now,
        )
        self.disagreements.append(disagreement)
        return disagreement

    def disagreement_for(self, *proposal_ids: str) -> PublicDisagreement | None:
        wanted = set(proposal_ids)
        for d in self.disagreements:
            if wanted <= set(d.proposal_ids):
                return d
        return None

    # —— 查询 ——

    def _require(self, proposal_id: str) -> MetricProposal:
        try:
            return self.proposals[proposal_id]
        except KeyError:
            raise WorkflowError(f"提案不存在：{proposal_id}") from None

    def _expert(self, expert_id: str) -> Expert:
        try:
            return self.experts[expert_id]
        except KeyError:
            raise WorkflowError(f"专家未登记：{expert_id}") from None

    def opinions_for(self, proposal_id: str) -> list[Opinion]:
        return list(self.opinions.get(proposal_id, ()))

    def votes_for(self, proposal_id: str) -> list[Vote]:
        return list(self.votes.get(proposal_id, ()))


def _pairs(items: list[MetricProposal]):
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            yield items[i], items[j]
