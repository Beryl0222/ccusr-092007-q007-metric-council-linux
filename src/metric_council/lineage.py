"""血缘追溯：从一个图表值追到当时的定义、输入批次与审议意见。

决策者在图表上看到的每个值都来自某个已发布快照。血缘视图按
*快照冻结时* 的版本号回溯，返回四部分证据：

1. 数值：快照值、单位、指标版本；
2. 定义：该版本生效区间内适用的口径（定义、单位、行业、去重边界）；
3. 批次：产生该版本的输入批次与修订原因链；
4. 审议：该口径提案的三委员会意见与回避记录。

全程只出现汇总值与指纹数量，不出现任何企业明细。
"""

from __future__ import annotations

from dataclasses import dataclass

from .proposals import MetricProposal
from .review import ReviewBoard
from .series import MetricSeries
from .snapshots import ReportSnapshot


@dataclass(frozen=True)
class Lineage:
    """一个图表值的完整证据链。"""

    metric_code: str
    region: str
    period: str
    frozen_value: float
    unit_code: str
    snapshot_id: str
    metric_version: int
    revision_reason: str
    batch_id: str
    definition: dict
    review: dict
    revision_chain: tuple[dict, ...]

    def to_view(self) -> dict:
        return {
            "value": {
                "metric_code": self.metric_code,
                "region": self.region,
                "period": self.period,
                "frozen_value": self.frozen_value,
                "unit": self.unit_code,
                "snapshot_id": self.snapshot_id,
                "metric_version": self.metric_version,
                "revision_reason": self.revision_reason,
                "batch_id": self.batch_id,
            },
            "definition": self.definition,
            "review": self.review,
            "revision_chain": list(self.revision_chain),
        }


def trace(
    snapshot: ReportSnapshot,
    metric_code: str,
    region: str,
    period: str,
    *,
    series: MetricSeries,
    proposal: MetricProposal,
    board: ReviewBoard,
) -> Lineage:
    """组装快照值 → 定义/批次/意见 的证据链。"""

    point = snapshot._point(metric_code, region, period)  # noqa: SLF001
    frozen_version = series.version(point.metric_version)
    case = board.case_of(proposal.record_id)

    definition_view = {
        "proposal_id": proposal.record_id,
        "metric_code": proposal.metric_code,
        "revision": proposal.revision,
        "metric_name": proposal.metric_name,
        "definition": proposal.definition,
        "unit": proposal.unit_code,
        "industries": sorted(proposal.industries),
        "dedup_boundary": {
            "subject": proposal.dedup.subject,
            "keys": list(proposal.dedup.keys),
            "window": proposal.dedup.window,
        },
        "effective_range": {
            "start": proposal.effective.start.isoformat(),
            "end": proposal.effective.end.isoformat() if proposal.effective.end else None,
        },
        "status": proposal.status.value,
        "data_sources": [
            {
                "source_id": s.source_id,
                "name": s.name,
                "granularity": s.granularity,
            }
            for s in proposal.sources
        ],
    }

    review_view = {
        "opinions": [
            {
                "reviewer": o.reviewer,
                "committee": o.committee.value,
                "impact": o.impact,
                "vote": o.vote.value,
                "filed_at": o.filed_at.isoformat(),
            }
            for o in case.opinions
        ],
        "recusals": [
            {
                "reviewer": r.reviewer,
                "committee": r.committee.value,
                "relation": r.relation,
                "declared_at": r.declared_at.isoformat(),
            }
            for r in case.recusals
        ],
    }

    chain = tuple(
        {
            "version": v.version,
            "reason": v.reason.value,
            "reason_detail": v.reason_detail,
            "batch_id": v.batch.batch_id,
            "source_id": v.batch.source_id,
            "created_at": v.created_at.isoformat(),
        }
        for v in series.versions()
        if v.version <= point.metric_version
    )

    return Lineage(
        metric_code=metric_code,
        region=region,
        period=period,
        frozen_value=point.value,
        unit_code=point.unit_code,
        snapshot_id=snapshot.snapshot_id,
        metric_version=point.metric_version,
        revision_reason=point.revision_reason,
        batch_id=point.batch_id,
        definition=definition_view,
        review=review_view,
        revision_chain=chain,
    )
