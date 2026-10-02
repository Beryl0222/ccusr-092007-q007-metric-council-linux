"""替代口径并排对比：展示口径选择 *造成* 的数值变化，但不合并。

对同一主题的不同口径（如“文化消费-宽口径”与“文化消费-窄口径”），
分别取已发布快照值并列展示：

* 同单位族：换算到共同单位并给出差额与相对差异，但保留两个独立值；
* 跨单位族（如 万元 与 人次）：只并列，不计算差额——差额没有量纲意义。

若某一口径处于公开分歧，对比结果显式标注 ``disputed: true``，
提醒决策者这是尚未裁决的分歧，而非可加总的两个部分。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import units
from .disputes import DisputeRegistry
from .errors import ValidationError
from .proposals import MetricProposal
from .snapshots import ReportSnapshot


@dataclass(frozen=True)
class CaliberColumn:
    metric_code: str
    metric_name: str
    revision: int
    value: float
    displayed_unit: str
    native_unit: str
    proposal_id: str
    disputed: bool
    definition: str


def compare_alternatives(
    snapshot: ReportSnapshot,
    proposals: tuple[MetricProposal, ...],
    region: str,
    period: str,
    *,
    common_unit: str | None = None,
    disputes: DisputeRegistry | None = None,
) -> dict:
    """在同一快照下并排对比多个替代口径。

    :param common_unit: 希望统一展示的单位；所有口径必须与它同族。
        省略时使用第一个口径的单位族；跨族则各自保留原单位并列。
    """

    if len(proposals) < 2:
        raise ValidationError("并排对比至少需要两个替代口径")

    disputed_ids: set[str] = set()
    if disputes is not None:
        for record in disputes.all():
            if record.open:
                disputed_ids.update(side.proposal_id for side in record.sides)

    columns: list[CaliberColumn] = []
    families: set[str] = set()
    for proposal in proposals:
        point = snapshot._point(  # noqa: SLF001
            proposal.metric_code, region, period
        )
        family = units.get(proposal.unit_code).family
        families.add(family)

        target_unit = common_unit or proposal.unit_code
        if common_unit is not None:
            displayed = units.convert(point.value, point.unit_code, common_unit)
        else:
            displayed = units.convert(
                point.value, point.unit_code, proposal.unit_code
            )

        columns.append(
            CaliberColumn(
                metric_code=proposal.metric_code,
                metric_name=proposal.metric_name,
                revision=proposal.revision,
                value=displayed,
                displayed_unit=target_unit,
                native_unit=point.unit_code,
                proposal_id=proposal.record_id,
                disputed=proposal.record_id in disputed_ids,
                definition=proposal.definition,
            )
        )

    same_family = len(families) == 1 and (
        common_unit is None
        or all(
            units.get(c.native_unit).family == units.get(common_unit).family
            for c in columns
        )
    )

    result = {
        "snapshot_id": snapshot.snapshot_id,
        "region": region,
        "period": period,
        "comparable_numerically": same_family,
        "columns": [
            {
                "metric_code": c.metric_code,
                "metric_name": c.metric_name,
                "revision": c.revision,
                "value": c.value,
                "unit": c.displayed_unit,
                "proposal_id": c.proposal_id,
                "disputed": c.disputed,
                "definition": c.definition,
            }
            for c in columns
        ],
    }

    if same_family:
        baseline = columns[0]
        result["deltas_vs_first"] = [
            {
                "metric_code": c.metric_code,
                "absolute": c.value - baseline.value,
                "relative": (
                    (c.value - baseline.value) / baseline.value
                    if baseline.value
                    else None
                ),
            }
            for c in columns[1:]
        ]
        result["note"] = "差额仅说明口径差异，两列独立存在，不作合并值"
    else:
        result["deltas_vs_first"] = []
        result["note"] = (
            "替代口径跨单位族，只并列展示原值，不计算差额；"
            "若二者应视为同一指标，请提交修订进入公开分歧程序"
        )
    return result
