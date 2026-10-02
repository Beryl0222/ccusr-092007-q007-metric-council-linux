"""指标序列：输入批次、带原因的版本修订与跨层级重复校验。

三类事后变化 *只* 生成新版本，绝不原地改写：

* :attr:`RevisionReason.SOURCE_CORRECTION` —— 来源更正；
* :attr:`RevisionReason.BOUNDARY_CHANGE` —— 行政区划变化；
* :attr:`RevisionReason.LATE_REPORT` —— 迟报数据补报。

每个版本都带原因、说明与来源批次；历史版本永久保留，正式报告通过
:mod:`metric_council.snapshots` 冻结的版本号引用当时的数值。

企业明细边界：上报行只能携带 *去重主体指纹*（不可逆哈希后的标识），
出现企业名称、统一社会信用代码等明细字段的批次直接拒收——服务内部
自始至终不持有企业明细。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from . import geography as geo
from . import units
from .errors import DoubleCountError, ValidationError

# 上报行中禁止出现的企业明细字段（只有脱敏指纹可以进入服务）
ENTERPRISE_DETAIL_FIELDS = frozenset(
    {
        "enterprise_name",
        "credit_code",
        "统一社会信用代码",
        "企业名称",
        "contact",
        "legal_person",
    }
)


class RevisionReason(str, Enum):
    INITIAL = "initial"
    SOURCE_CORRECTION = "source_correction"
    BOUNDARY_CHANGE = "boundary_change"
    LATE_REPORT = "late_report"


@dataclass(frozen=True)
class InputBatch:
    """一次数据上报批次。"""

    batch_id: str
    source_id: str
    received_at: datetime
    submitted_by: str
    contains_enterprise_detail: bool = False


@dataclass(frozen=True)
class Observation:
    """一个区域一个报告期的数值点。

    ``subject_fingerprints`` 是去重主体的不可逆指纹集合，用于跨层级
    重复校验；它不含任何可识别企业的信息。
    """

    region: str
    period: str
    as_of: date
    """取数时点，用于确定当日的行政区划隶属。"""

    value: float
    unit_code: str
    level: int
    """行政层级：0=省级，数字越大层级越低。"""

    subject_fingerprints: frozenset[str] = frozenset()
    scope_covers_children: bool = True
    """该点数值是否已包含下级区域（上级直接上报的总量通常为 True）。"""


@dataclass(frozen=True)
class SeriesVersion:
    """序列的一个不可变版本。"""

    version: int
    reason: RevisionReason
    reason_detail: str
    batch: InputBatch
    created_at: datetime
    observations: tuple[Observation, ...]
    parent_version: int | None = None

    def point(self, region: str, period: str) -> Observation | None:
        for obs in self.observations:
            if obs.region == region and obs.period == period:
                return obs
        return None

    def periods(self) -> tuple[str, ...]:
        return tuple(sorted({o.period for o in self.observations}))


def fingerprint_subject(raw_key: str) -> str:
    """把去重键（如企业信用代码）转为不可逆指纹后再进入服务。"""

    return "fp:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]


def _guard_row(row: dict) -> None:
    leaked = ENTERPRISE_DETAIL_FIELDS.intersection(row)
    if leaked:
        raise ValidationError(
            f"上报行包含企业明细字段 {sorted(leaked)}，服务边界拒收；"
            "请在来源侧完成去重并仅提交指纹"
        )


@dataclass
class MetricSeries:
    """一个指标代码的全部版本序列。"""

    metric_code: str
    canonical_unit: str
    """口径定义规定的标准单位，所有对外数值换算到该单位。"""

    _versions: list[SeriesVersion] = field(default_factory=list)

    # -- 写入 -------------------------------------------------------------

    def ingest_initial(self, batch: InputBatch, rows: list[dict]) -> SeriesVersion:
        """首次建序列。rows 为来源侧已汇总的数值点。"""

        # 先校验行内容（企业明细守卫等），使脏数据在任何状态判断前被拒
        observations = tuple(self._row_to_obs(row) for row in rows)
        if self._versions:
            raise ValidationError("序列已有初始版本，修订请用 revise()")
        version = SeriesVersion(
            version=1,
            reason=RevisionReason.INITIAL,
            reason_detail="初始建序列",
            batch=batch,
            created_at=batch.received_at,
            observations=observations,
        )
        self._versions.append(version)
        return version

    def revise(
        self,
        reason: RevisionReason,
        reason_detail: str,
        batch: InputBatch,
        rows: list[dict],
        *,
        now: datetime | None = None,
    ) -> SeriesVersion:
        """以带原因的新版本替换全量观测（旧版本保留）。

        来源更正、区划变化、迟报统一走此入口；``INITIAL`` 不是合法
        的修订原因。
        """

        if not self._versions:
            raise ValidationError("序列尚未建立，初始版本请用 ingest_initial()")
        if reason is RevisionReason.INITIAL:
            raise ValidationError("修订原因不能是 initial")
        if not reason_detail.strip():
            raise ValidationError("修订版本必须填写原因说明")
        observations = tuple(self._row_to_obs(row) for row in rows)
        version = SeriesVersion(
            version=self._versions[-1].version + 1,
            reason=reason,
            reason_detail=reason_detail,
            batch=batch,
            created_at=now or datetime.now().astimezone(),
            observations=observations,
            parent_version=self._versions[-1].version,
        )
        self._versions.append(version)
        return version

    def _row_to_obs(self, row: dict) -> Observation:
        _guard_row(row)
        required = ("region", "period", "as_of", "value", "unit", "level")
        missing = [k for k in required if k not in row]
        if missing:
            raise ValidationError(f"上报行缺少字段：{missing}")
        unit_code = row["unit"]
        # 入库前验证可换算到口径标准单位（跨族直接拒绝）
        if units.get(unit_code).family != units.get(self.canonical_unit).family:
            raise ValidationError(
                f"上报单位 {unit_code} 与口径标准单位 "
                f"{self.canonical_unit} 不属于同一单位族"
            )
        fps = row.get("subject_fingerprints", ())
        return Observation(
            region=row["region"],
            period=row["period"],
            as_of=(
                row["as_of"]
                if isinstance(row["as_of"], date)
                else date.fromisoformat(row["as_of"])
            ),
            value=float(row["value"]),
            unit_code=unit_code,
            level=int(row["level"]),
            subject_fingerprints=frozenset(fps),
            scope_covers_children=bool(row.get("scope_covers_children", True)),
        )

    # -- 读取 -------------------------------------------------------------

    @property
    def latest(self) -> SeriesVersion:
        if not self._versions:
            raise ValidationError(f"指标 {self.metric_code} 尚无序列版本")
        return self._versions[-1]

    def version(self, number: int) -> SeriesVersion:
        try:
            return self._versions[number - 1]
        except IndexError as exc:
            raise ValidationError(
                f"指标 {self.metric_code} 不存在版本 {number}"
            ) from exc

    def versions(self) -> tuple[SeriesVersion, ...]:
        return tuple(self._versions)

    def canonical_value(self, obs: Observation) -> float:
        return units.convert(obs.value, obs.unit_code, self.canonical_unit)


# ---------------------------------------------------------------------------
# 跨层级重复校验
# ---------------------------------------------------------------------------

def check_no_double_count(
    version: SeriesVersion,
    period: str,
    geography: geo.Geography,
) -> None:
    """校验同一报告期内跨层级汇总是否会重复计入。

    两种重复都会被拒绝：

    1. 上级点声明已包含下级（``scope_covers_children``），同时下级又
       单独上报——上级与下级相加会把同一主体算两次；
    2. 任意两个在当日构成隶属关系的点，其去重主体指纹出现交集。
    """

    points = [o for o in version.observations if o.period == period]
    for i, obs in enumerate(points):
        for other in points[i + 1 :]:
            higher, lower = _order_by_level(obs, other)
            if not geography.related_on(higher.region, lower.region, lower.as_of):
                continue
            overlap = higher.subject_fingerprints & lower.subject_fingerprints
            if overlap:
                raise DoubleCountError(
                    f"{period} {higher.region} 与 {lower.region} 存在 "
                    f"{len(overlap)} 个重复去重主体，跨层级汇总被拒绝"
                )
            if higher.scope_covers_children:
                raise DoubleCountError(
                    f"{period} 上级 {higher.region} 上报值已包含下级，"
                    f"下级 {lower.region} 不得再重复计入"
                )


def _order_by_level(a: Observation, b: Observation) -> tuple[Observation, Observation]:
    if a.level <= b.level:
        return a, b
    return b, a


def aggregate_children(
    version: SeriesVersion,
    parent_region: str,
    period: str,
    geography: geo.Geography,
    *,
    canonical_unit: str,
) -> float:
    """汇总直属下级点；若上级另有“已含下级”的总量点则拒绝，避免重复。"""

    points = [o for o in version.observations if o.period == period]
    parent_point = next((o for o in points if o.region == parent_region), None)
    children = [
        o
        for o in points
        if o.region != parent_region
        and geography.parent_on(o.region, o.as_of) == parent_region
    ]
    if parent_point is not None and parent_point.scope_covers_children and children:
        raise DoubleCountError(
            f"{period} {parent_region} 的上级总量与下级明细同时存在，"
            "只能二选一，禁止叠加"
        )
    if not children:
        if parent_point is not None:
            return units.convert(
                parent_point.value, parent_point.unit_code, canonical_unit
            )
        raise ValidationError(f"{period} {parent_region} 无可汇总的下级数据")
    total = 0.0
    seen: set[str] = set()
    for child in children:
        duplicated = seen & child.subject_fingerprints
        if duplicated:
            raise DoubleCountError(
                f"{period} 下级 {child.region} 与其他下级存在 "
                f"{len(duplicated)} 个重复主体"
            )
        seen |= child.subject_fingerprints
        total += units.convert(child.value, child.unit_code, canonical_unit)
    return total
