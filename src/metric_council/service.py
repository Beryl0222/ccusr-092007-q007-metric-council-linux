"""审议后端服务门面：把全部能力收敛到一个带访问控制的入口。

典型链路::

    svc = CouncilService()
    proposal = svc.submit_proposal(actor, payload)      # 业务部门提交
    svc.file_opinion(actor, proposal, "...", vote)      # 专家三委员会审议
    approved, case = svc.close_review(actor, proposal)
    svc.ingest_rows(actor, batch, rows)                 # 数据上报
    snapshot = svc.publish_report(actor, ...)           # 发布冻结
    svc.chart_lineage(actor, snapshot, ...)             # 决策者追证据链
    svc.compare_calibers(actor, snapshot, ...)          # 并排看替代口径

所有方法首先过 :class:`~metric_council.access.Policy`；企业明细在
*上报边界*（行字段守卫）和 *访问矩阵*（无角色可达）两处同时被拒。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import geography as geo
from .access import Action, Actor, Policy
from .compare import compare_alternatives
from .disputes import DisputeRegistry
from .lineage import trace
from .proposals import (
    MetricProposal,
    ProposalStatus,
    proposal_from_payload,
)
from .review import ReviewBoard, Vote
from .series import (
    InputBatch,
    MetricSeries,
    RevisionReason,
    aggregate_children,
    check_no_double_count,
)
from .snapshots import Publisher, ReportSnapshot
from .derivations import DerivedMetric


class CouncilService:
    def __init__(self) -> None:
        self.board = ReviewBoard()
        self.disputes = DisputeRegistry()
        self.publisher = Publisher()
        self.geography = geo.Geography()
        self._proposals: dict[str, MetricProposal] = {}
        """record_id -> 提案（状态推进后的最新对象）。"""

        self._by_code: dict[str, dict[int, str]] = {}
        """metric_code -> {revision: record_id}。"""

    # -- 提案 -------------------------------------------------------------

    def submit_proposal(
        self, actor: Actor, payload: dict
    ) -> MetricProposal:
        Policy.require(actor, Action.PROPOSAL_SUBMIT)
        proposal = proposal_from_payload(payload)
        if proposal.status is not ProposalStatus.DRAFT:
            from .errors import ValidationError

            raise ValidationError("新提交的提案必须处于 draft 状态")
        proposal = proposal.with_status(ProposalStatus.SUBMITTED)
        self._proposals[proposal.record_id] = proposal
        self._by_code.setdefault(proposal.metric_code, {})[
            proposal.revision
        ] = proposal.record_id
        return proposal

    def submit_proposal_file(self, actor: Actor, path: Path) -> MetricProposal:
        return self.submit_proposal(
            actor, json.loads(path.read_text(encoding="utf-8"))
        )

    def proposal(self, actor: Actor, record_id: str) -> MetricProposal:
        Policy.require(actor, Action.PROPOSAL_READ)
        return self._proposals[record_id]

    def approved_definition(
        self, metric_code: str, revision: int | None = None
    ) -> MetricProposal:
        """取已批准定义（派生指标内部用）；默认取最新批准修订。"""

        revisions = self._by_code[metric_code]
        rev = revision or max(revisions)
        proposal = self._proposals[revisions[rev]]
        if proposal.status is not ProposalStatus.APPROVED:
            from .errors import WorkflowError

            raise WorkflowError(
                f"{metric_code} r{rev} 状态为 {proposal.status.value}，"
                "不能作为已批准定义引用"
            )
        return proposal

    # -- 审议 -------------------------------------------------------------

    def file_opinion(
        self,
        actor: Actor,
        proposal: MetricProposal,
        impact: str,
        vote: Vote,
    ):
        Policy.require(actor, Action.REVIEW_FILE)
        opinion = self.board.file_opinion(proposal, actor.actor_id, impact, vote)
        return opinion

    def declare_interest(
        self, actor: Actor, proposal: MetricProposal, relation: str
    ):
        # 回避声明本身是义务而非特权：任何已登记专家都可声明
        return self.board.declare_interest(
            actor.actor_id, proposal.record_id, relation
        )

    def close_review(self, actor: Actor, proposal: MetricProposal):
        Policy.require(actor, Action.REVIEW_FILE)
        updated, case = self.board.close_review(proposal)
        self._proposals[updated.record_id] = updated
        return updated, case

    # -- 派生 -------------------------------------------------------------

    def add_derived(self, actor: Actor, derived: DerivedMetric) -> DerivedMetric:
        Policy.require(actor, Action.PROPOSAL_SUBMIT)
        approved = {
            code: self.approved_definition(code)
            for code in derived.components
        }
        derived.validate_against(approved)
        return derived

    # -- 序列与版本 -------------------------------------------------------

    def register_series(
        self, actor: Actor, metric_code: str, canonical_unit: str
    ) -> MetricSeries:
        Policy.require(actor, Action.SERIES_INGEST)
        series = MetricSeries(metric_code, canonical_unit)
        self.publisher.register_series(series)
        return series

    def ingest_rows(
        self, actor: Actor, series: MetricSeries, batch: InputBatch, rows: list[dict]
    ):
        Policy.require(actor, Action.SERIES_INGEST)
        return series.ingest_initial(batch, rows)

    def revise_rows(
        self,
        actor: Actor,
        series: MetricSeries,
        reason: RevisionReason,
        reason_detail: str,
        batch: InputBatch,
        rows: list[dict],
    ):
        Policy.require(actor, Action.SERIES_REVISE)
        return series.revise(reason, reason_detail, batch, rows)

    def check_double_count(
        self, actor: Actor, series: MetricSeries, period: str
    ) -> None:
        Policy.require(actor, Action.SERIES_INGEST)
        check_no_double_count(series.latest, period, self.geography)

    def aggregate(
        self,
        actor: Actor,
        series: MetricSeries,
        parent_region: str,
        period: str,
    ) -> float:
        Policy.require(actor, Action.CHART_READ)
        return aggregate_children(
            series.latest,
            parent_region,
            period,
            self.geography,
            canonical_unit=series.canonical_unit,
        )

    # -- 发布 -------------------------------------------------------------

    def publish_report(
        self,
        actor: Actor,
        snapshot_id: str,
        report_name: str,
        selections: dict[str, tuple[str, ...]],
    ) -> ReportSnapshot:
        Policy.require(actor, Action.SNAPSHOT_PUBLISH)
        return self.publisher.publish(snapshot_id, report_name, selections)

    # -- 决策者视图 -------------------------------------------------------

    def chart_value(
        self,
        actor: Actor,
        snapshot: ReportSnapshot,
        metric_code: str,
        region: str,
        period: str,
    ) -> float:
        Policy.require(actor, Action.CHART_READ)
        return snapshot.value(metric_code, region, period)

    def chart_lineage(
        self,
        actor: Actor,
        snapshot: ReportSnapshot,
        metric_code: str,
        region: str,
        period: str,
    ) -> dict:
        Policy.require(actor, Action.LINEAGE_READ)
        series = self.publisher.series_of(metric_code)
        # 找到冻结版本生效的口径修订：取快照发布时的最新已批准定义
        proposal = self._definition_for(metric_code, snapshot.published_at)
        lineage = trace(
            snapshot,
            metric_code,
            region,
            period,
            series=series,
            proposal=proposal,
            board=self.board,
        )
        return lineage.to_view()

    def _definition_for(
        self, metric_code: str, on: datetime
    ) -> MetricProposal:
        """取 *on* 时点适用的口径修订（含 disputed——分歧口径同样要
        能被追溯与并排展示）；同日取修订号最大者。"""

        revisions = self._by_code.get(metric_code, {})
        if not revisions:
            from .errors import WorkflowError

            raise WorkflowError(f"{metric_code} 无已提交定义可供追溯")
        rid = revisions[max(revisions)]
        return self._proposals[rid]

    def frozen_vs_latest(
        self,
        actor: Actor,
        snapshot: ReportSnapshot,
        metric_code: str,
        region: str,
        period: str,
    ) -> dict:
        Policy.require(actor, Action.LINEAGE_READ)
        return self.publisher.frozen_vs_latest(
            snapshot, metric_code, region, period
        )

    def compare_calibers(
        self,
        actor: Actor,
        snapshot: ReportSnapshot,
        metric_codes: tuple[str, ...],
        region: str,
        period: str,
        *,
        common_unit: str | None = None,
    ) -> dict:
        Policy.require(actor, Action.CHART_READ)
        proposals = tuple(
            self._definition_for(code, snapshot.published_at)
            for code in metric_codes
        )
        return compare_alternatives(
            snapshot,
            proposals,
            region,
            period,
            common_unit=common_unit,
            disputes=self.disputes,
        )

    # -- 公开分歧 ---------------------------------------------------------

    def open_dispute(
        self,
        actor: Actor,
        dispute_id: str,
        topic: str,
        left: MetricProposal,
        right: MetricProposal,
    ):
        Policy.require(actor, Action.REVIEW_FILE)
        return self.disputes.open_from_conflict(
            dispute_id, topic, left, right
        )
