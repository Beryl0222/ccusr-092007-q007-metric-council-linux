"""发布冻结：正式报告引用发布时的快照，事后修订不影响已发布数字。

一次发布把每个指标 *当时的版本号* 连同数值点一起冻结。之后来源
更正、区划变化或迟报产生新版本时：

* 旧快照仍返回发布时的值，任何改写尝试抛出
  :class:`SnapshotFrozenError`；
* 新报告重新发布时生成新快照；
* 血缘视图同时给出快照值与最新值，并排显示修订带来的变化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from . import units
from .errors import SnapshotFrozenError, ValidationError
from .series import MetricSeries, Observation


@dataclass(frozen=True)
class FrozenPoint:
    region: str
    period: str
    value: float
    """已换算到口径标准单位的值。"""

    unit_code: str
    metric_version: int
    revision_reason: str
    batch_id: str


@dataclass(frozen=True)
class ReportSnapshot:
    """一次正式报告发布的冻结快照。"""

    snapshot_id: str
    report_name: str
    published_at: datetime
    points: dict[tuple[str, str, str], FrozenPoint]
    """键：(metric_code, region, period)。"""

    metric_versions: dict[str, int]
    """冻结时每个指标的序列版本号，供血缘追溯。"""

    def value(self, metric_code: str, region: str, period: str) -> float:
        point = self._point(metric_code, region, period)
        return point.value

    def _point(self, metric_code: str, region: str, period: str) -> FrozenPoint:
        try:
            return self.points[(metric_code, region, period)]
        except KeyError as exc:
            raise ValidationError(
                f"快照中无此数值：{metric_code}/{region}/{period}"
            ) from exc

    def provenance_key(
        self, metric_code: str, region: str, period: str
    ) -> tuple[int, str]:
        """返回 (冻结版本号, 输入批次号)。"""

        point = self._point(metric_code, region, period)
        return point.metric_version, point.batch_id

    def items(self) -> tuple[FrozenPoint, ...]:
        return tuple(self.points.values())


class Publisher:
    """管理指标序列与报告快照的发布台。"""

    def __init__(self) -> None:
        self._series: dict[str, MetricSeries] = {}
        self._snapshots: dict[str, ReportSnapshot] = {}

    def register_series(self, series: MetricSeries) -> None:
        if series.metric_code in self._series:
            raise ValidationError(f"序列已登记：{series.metric_code}")
        self._series[series.metric_code] = series

    def series_of(self, metric_code: str) -> MetricSeries:
        return self._series[metric_code]

    def publish(
        self,
        snapshot_id: str,
        report_name: str,
        selections: dict[str, tuple[str, ...]],
        *,
        now: datetime | None = None,
    ) -> ReportSnapshot:
        """发布快照。

        :param selections: ``{metric_code: (region, ...)}``，对给定
            区域取最新版本中 *全部报告期* 的点；不指定区域时取全部点。
        """

        if snapshot_id in self._snapshots:
            raise ValidationError(f"快照编号已存在：{snapshot_id}")

        points: dict[tuple[str, str, str], FrozenPoint] = {}
        versions: dict[str, int] = {}
        for metric_code, regions in selections.items():
            series = self._series[metric_code]
            version = series.latest
            versions[metric_code] = version.version
            wanted = frozenset(regions)
            for obs in version.observations:
                if wanted and obs.region not in wanted:
                    continue
                points[(metric_code, obs.region, obs.period)] = FrozenPoint(
                    region=obs.region,
                    period=obs.period,
                    value=series.canonical_value(obs),
                    unit_code=series.canonical_unit,
                    metric_version=version.version,
                    revision_reason=version.reason.value,
                    batch_id=version.batch.batch_id,
                )

        snapshot = ReportSnapshot(
            snapshot_id=snapshot_id,
            report_name=report_name,
            published_at=now or datetime.now().astimezone(),
            points=points,
            metric_versions=versions,
        )
        self._snapshots[snapshot_id] = snapshot
        return snapshot

    def snapshot(self, snapshot_id: str) -> ReportSnapshot:
        return self._snapshots[snapshot_id]

    def snapshots(self) -> tuple[ReportSnapshot, ...]:
        return tuple(self._snapshots.values())

    def frozen_vs_latest(
        self, snapshot: ReportSnapshot, metric_code: str, region: str, period: str
    ) -> dict:
        """并排返回快照值与最新版本值，展示修订带来的变化及原因链。"""

        point = snapshot._point(metric_code, region, period)  # noqa: SLF001
        series = self._series[metric_code]
        latest = series.latest
        latest_obs = latest.point(region, period)
        if latest_obs is None:
            raise ValidationError(
                f"最新版本中无 {metric_code}/{region}/{period}"
            )

        chain = []
        for number in range(point.metric_version, latest.version + 1):
            v = series.version(number)
            chain.append(
                {
                    "version": v.version,
                    "reason": v.reason.value,
                    "reason_detail": v.reason_detail,
                    "batch_id": v.batch.batch_id,
                    "created_at": v.created_at.isoformat(),
                }
            )
        latest_value = series.canonical_value(latest_obs)
        frozen_value = point.value
        delta = latest_value - frozen_value
        return {
            "metric_code": metric_code,
            "region": region,
            "period": period,
            "unit": series.canonical_unit,
            "frozen": {
                "value": frozen_value,
                "snapshot_id": snapshot.snapshot_id,
                "version": point.metric_version,
                "batch_id": point.batch_id,
            },
            "latest": {
                "value": latest_value,
                "version": latest.version,
                "batch_id": latest.batch.batch_id,
                "reason": latest.reason.value,
            },
            "delta": delta,
            "changed": latest.version != point.metric_version,
            "revision_chain": chain,
        }

    def replace_frozen_value(self, *args, **kwargs) -> None:
        """显式拒绝：冻结值不可改写。"""

        raise SnapshotFrozenError(
            "正式报告快照不可改写；来源更正/区划变化/迟报只能生成新版本，"
            "请重新发布新快照"
        )
