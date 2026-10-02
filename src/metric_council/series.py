"""时间序列版本与发布快照。

铁律：**历史不可原地改写**。来源更正、行政区划变化、迟报数据都只生成
一个带原因的新 :class:`SeriesVersion`，旧版本原样保留；正式报告引用
发布当时冻结的 :class:`ReportSnapshot`，之后的任何修订都不改变报告数字。

每个序列单元（指标 × 区域 × 期）同时记录其输入批次，支撑
"从图表值追到当时的定义、输入批次与审议意见"的谱系链；
快照只物化区域值与批次 id，不含任何企业明细。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .errors import SnapshotError, VersioningError
from .ingest import (
    REASON_LATE_REPORT,
    REASON_REGION_CHANGE,
    REASON_SOURCE_CORRECTION,
    AcceptedPoint,
)
from .proposals import MetricProposal

VERSION_REASONS = (
    "initial",
    REASON_SOURCE_CORRECTION,
    REASON_REGION_CHANGE,
    REASON_LATE_REPORT,
)

Cell = tuple[str, str]  # (region_code, period)


@dataclass(frozen=True)
class SeriesVersion:
    version_id: str
    metric_code: str
    seq: int                       # 同一指标内单调递增
    reason: str
    based_on_version: str | None
    note: str
    created_at: datetime
    values: dict[Cell, float] = field(default_factory=dict)
    cell_batches: dict[Cell, tuple[str, ...]] = field(default_factory=dict)
    changed_cells: tuple[Cell, ...] = ()
    proposal_revision: int = 0     # 生成该版本时定义的 revision

    def value(self, region_code: str, period: str) -> float:
        try:
            return self.values[(region_code, period)]
        except KeyError:
            raise VersioningError(
                f"版本 {self.version_id} 无 {region_code}/{period} 的值"
            ) from None


@dataclass(frozen=True)
class ReportSnapshot:
    """发布时冻结的快照：值与谱系引用被复制，之后不可变。"""

    snapshot_id: str
    report_id: str
    title: str
    frozen_at: datetime
    metric_versions: dict[str, str]                   # metric_code -> version_id
    values: dict[str, dict[Cell, float]] = field(default_factory=dict)
    cell_batches: dict[str, dict[Cell, tuple[str, ...]]] = field(default_factory=dict)

    def value(self, metric_code: str, region_code: str, period: str) -> float:
        try:
            return self.values[metric_code][(region_code, period)]
        except KeyError:
            raise SnapshotError(
                f"快照 {self.snapshot_id} 未冻结 {metric_code} "
                f"{region_code}/{period}"
            ) from None

    def provenance(self, metric_code: str, region_code: str,
                   period: str) -> tuple[str, ...]:
        return self.cell_batches.get(metric_code, {}).get((region_code, period), ())


class SeriesStore:
    def __init__(self) -> None:
        self._versions: dict[str, list[SeriesVersion]] = {}
        self._snapshots: dict[str, ReportSnapshot] = {}

    # —— 版本 ——

    def current(self, metric_code: str) -> SeriesVersion | None:
        versions = self._versions.get(metric_code, [])
        return versions[-1] if versions else None

    def version(self, version_id: str) -> SeriesVersion:
        for versions in self._versions.values():
            for v in versions:
                if v.version_id == version_id:
                    return v
        raise VersioningError(f"序列版本不存在：{version_id}")

    def versions(self, metric_code: str) -> list[SeriesVersion]:
        return list(self._versions.get(metric_code, ()))

    def build_initial(self, metric_code: str, proposal: MetricProposal,
                      points: list[AcceptedPoint], now: datetime,
                      version_id: str) -> SeriesVersion:
        if metric_code in self._versions:
            raise VersioningError(
                f"指标 {metric_code} 已有序列版本，后续数据只能按原因另立版本"
            )
        values, cell_batches = self._aggregate(points)
        version = SeriesVersion(
            version_id=version_id, metric_code=metric_code, seq=1,
            reason="initial", based_on_version=None,
            note="按首次通过审议的口径建立序列", created_at=now,
            values=values, cell_batches=cell_batches,
            changed_cells=tuple(sorted(values)),
            proposal_revision=proposal.revision,
        )
        self._versions.setdefault(metric_code, []).append(version)
        return version

    def revise(self, metric_code: str, proposal: MetricProposal,
               points: list[AcceptedPoint], reason: str, note: str,
               now: datetime, version_id: str) -> SeriesVersion:
        """以新一批已采集点生成新版本。

        reason 必须是三类修订原因之一；新值在上一版本基础上重算，
        仅记录发生变化的单元。绝不修改旧版本。
        """
        if reason not in VERSION_REASONS or reason == "initial":
            raise VersioningError(
                f"修订原因必须是 {VERSION_REASONS[1:]} 之一，收到 {reason!r}"
            )
        prior = self.current(metric_code)
        if prior is None:
            raise VersioningError(f"指标 {metric_code} 尚无初始版本")
        if not note.strip():
            raise VersioningError("生成新版本必须附文字说明（原因详情）")

        # 以新版本时刻的全部点重算：points 为采集层当前接受的全集。
        values, cell_batches = self._aggregate(points)
        changed = tuple(sorted(
            cell for cell in set(values) | set(prior.values)
            if values.get(cell) != prior.values.get(cell)
        ))
        if not changed:
            raise VersioningError("新批次未改变任何序列单元，不应另立版本")
        seq = prior.seq + 1
        version = SeriesVersion(
            version_id=version_id, metric_code=metric_code, seq=seq,
            reason=reason, based_on_version=prior.version_id, note=note,
            created_at=now, values=values, cell_batches=cell_batches,
            changed_cells=changed, proposal_revision=proposal.revision,
        )
        self._versions[metric_code].append(version)
        return version

    @staticmethod
    def _aggregate(points: list[AcceptedPoint]):
        values: dict[Cell, float] = {}
        cell_batches: dict[Cell, list[str]] = {}
        for p in points:
            cell = (p.region_code, p.period)
            values[cell] = round(values.get(cell, 0.0) + p.value, 6)
            cell_batches.setdefault(cell, [])
            if p.batch_id not in cell_batches[cell]:
                cell_batches[cell].append(p.batch_id)
        return values, {k: tuple(v) for k, v in cell_batches.items()}

    # —— 快照 ——

    def freeze(self, report_id: str, title: str, metric_codes: list[str],
               now: datetime, snapshot_id: str) -> ReportSnapshot:
        if snapshot_id in self._snapshots:
            raise SnapshotError(f"快照 id 重复：{snapshot_id}")
        metric_versions: dict[str, str] = {}
        values: dict[str, dict[Cell, float]] = {}
        cell_batches: dict[str, dict[Cell, tuple[str, ...]]] = {}
        for code in metric_codes:
            current = self.current(code)
            if current is None:
                raise SnapshotError(f"指标 {code} 尚无序列，无法冻结")
            metric_versions[code] = current.version_id
            # 复制 dict，确保后续修订无法影响已冻结内容。
            values[code] = dict(current.values)
            cell_batches[code] = {k: v for k, v in current.cell_batches.items()}
        snapshot = ReportSnapshot(
            snapshot_id=snapshot_id, report_id=report_id, title=title,
            frozen_at=now, metric_versions=metric_versions,
            values=values, cell_batches=cell_batches,
        )
        self._snapshots[snapshot_id] = snapshot
        return snapshot

    def snapshot(self, snapshot_id: str) -> ReportSnapshot:
        try:
            return self._snapshots[snapshot_id]
        except KeyError:
            raise SnapshotError(f"快照不存在：{snapshot_id}") from None

    def snapshots_for_report(self, report_id: str) -> list[ReportSnapshot]:
        return [s for s in self._snapshots.values() if s.report_id == report_id]
