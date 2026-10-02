"""派生指标：只允许建立在 *兼容* 定义之上。

兼容（同单位族、同去重边界）的已批准定义可以组合成派生指标，例如
``数字展陈收入 + 文旅联票分摊收入`` 在同一去重主体与窗口下求和。
任何口径冲突（单位族不同、去重边界不同）都会抛出
:class:`IncompatibleDefinitionsError`，由调用方转入公开分歧登记，
而不是在本模块里被静默合并。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from .errors import (
    IncompatibleDefinitionsError,
    ValidationError,
    WorkflowError,
)
from .proposals import (
    MetricProposal,
    ProposalStatus,
    are_compatible,
    compatibility_report,
)
from . import units


class DeriveOp(str, Enum):
    SUM = "sum"
    RATIO = "ratio"        # 分子/分母，跨单位族允许，但结果单位为比率族
    WEIGHTED = "weighted"  # 加权组合，要求同族


@dataclass(frozen=True)
class DerivedMetric:
    """一条派生指标定义。"""

    derived_code: str
    name: str
    op: DeriveOp
    components: tuple[str, ...]
    """组件的 ``metric_code``（必须全部已批准且两两兼容，比率除外）。"""

    unit_code: str
    weights: tuple[float, ...] = ()
    effective_from: date | None = None

    def validate_against(
        self, approved: dict[str, MetricProposal]
    ) -> None:
        if not self.components:
            raise ValidationError("派生指标至少需要一个组件")
        missing = [c for c in self.components if c not in approved]
        if missing:
            raise ValidationError(f"组件指标未批准或不存在：{missing}")

        defs = [approved[c] for c in self.components]
        for d in defs:
            if d.status is not ProposalStatus.APPROVED:
                raise WorkflowError(
                    f"组件 {d.metric_code} 未处于批准状态"
                )

        if self.op is DeriveOp.RATIO:
            # 比率派生允许跨族（如 收入/人次），但结果必须落在比率族
            if units.get(self.unit_code).family != units.FAMILY_RATIO:
                raise ValidationError("比率派生的结果单位必须属于比率族")
        else:
            # sum / weighted：两两兼容，禁止把冲突口径加成一个数
            for i in range(len(defs)):
                for j in range(i + 1, len(defs)):
                    ok, reasons = compatibility_report(defs[i], defs[j])
                    if not ok:
                        raise IncompatibleDefinitionsError(
                            "；".join(reasons),
                            left=defs[i].metric_code,
                            right=defs[j].metric_code,
                        )
            if self.op is DeriveOp.WEIGHTED:
                if len(self.weights) != len(self.components):
                    raise ValidationError("权重数量必须与组件一致")
                if abs(sum(self.weights) - 1.0) > 1e-9:
                    raise ValidationError("加权派生的权重之和必须为 1")

    def compute(self, values: dict[str, float]) -> float:
        """按定义对组件值（已换算到共同单位）做运算。"""

        missing = [c for c in self.components if c not in values]
        if missing:
            raise ValidationError(f"计算缺少组件值：{missing}")
        seq = [values[c] for c in self.components]
        if self.op is DeriveOp.SUM:
            return sum(seq)
        if self.op is DeriveOp.WEIGHTED:
            return sum(v * w for v, w in zip(seq, self.weights))
        # RATIO：分子/分母
        denominator = seq[1]
        if denominator == 0:
            raise ValidationError("比率派生的分母为 0")
        return seq[0] / denominator
