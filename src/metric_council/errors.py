"""领域错误体系。

所有可预期的业务拒绝都使用 :class:`MetricCouncilError` 的子类，
便于服务层把"输入不合法"与"程序缺陷"区分开。
"""

from __future__ import annotations


class MetricCouncilError(Exception):
    """指标口径审议领域的基础异常。"""


class ContractError(MetricCouncilError):
    """记录信封/数据合同不符合约定（含版本无法识别）。"""


class UnitConversionError(MetricCouncilError):
    """量纲不同或单位未登记，无法换算。"""


class ProposalValidationError(MetricCouncilError):
    """指标提案缺少必填要素或要素取值非法。"""


class CompatibilityConflictError(MetricCouncilError):
    """两份定义口径冲突，不能合并为派生指标。"""

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message)
        self.reasons = reasons or []


class RecusalViolation(MetricCouncilError):
    """存在利益关系的专家试图参与表决，必须回避。"""


class QuorumError(MetricCouncilError):
    """某一专家组因回避等原因失去有效表决人数。"""


class DuplicationViolation(MetricCouncilError):
    """采集数据命中去重边界或跨层级重复。"""

    def __init__(self, message: str, *, violations: list[dict] | None = None) -> None:
        super().__init__(message)
        self.violations = violations or []


class VersioningError(MetricCouncilError):
    """试图原地改写历史序列；更正/区划/迟报只能另立版本。"""


class SnapshotError(MetricCouncilError):
    """快照不可变，或引用了不存在的发布版本。"""


class PermissionDenied(MetricCouncilError):
    """当前角色无权执行该操作或查看该粒度的数据。"""


class WorkflowError(MetricCouncilError):
    """提案/审议状态机不允许该动作。"""
