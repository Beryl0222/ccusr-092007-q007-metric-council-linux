"""口径审议后端的领域错误类型。"""

from __future__ import annotations


class CouncilError(Exception):
    """所有领域错误的基类。"""


class ValidationError(CouncilError):
    """提案、批次或时间区间不满足合同。"""


class IncompatibleDefinitionsError(CouncilError):
    """两个定义口径冲突，不能静默合并或派生。

    :param reason: 具体不兼容点（单位族、去重边界、适用行业等）。
    """

    def __init__(self, reason: str, *, left: str = "", right: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.left = left
        self.right = right


class ConflictOfInterestError(CouncilError):
    """评议人对存在利益关系的提案尝试表决。"""

    def __init__(self, reviewer: str, proposal_id: str, relation: str):
        super().__init__(
            f"评议人 {reviewer} 与提案 {proposal_id} 存在利益关系（{relation}），必须回避"
        )
        self.reviewer = reviewer
        self.proposal_id = proposal_id
        self.relation = relation


class WorkflowError(CouncilError):
    """状态机非法迁移（重复提交、已冻结后修改等）。"""


class AccessDeniedError(CouncilError):
    """当前角色无权执行该操作或接触该粒度的数据。"""

    def __init__(self, actor: str, action: str, detail: str = ""):
        msg = f"角色 {actor} 无权执行：{action}"
        if detail:
            msg += f"（{detail}）"
        super().__init__(msg)
        self.actor = actor
        self.action = action
        self.detail = detail


class DoubleCountError(CouncilError):
    """跨层级汇总检测到同一主体/同一业务被重复计入。"""


class SnapshotFrozenError(CouncilError):
    """正式报告引用的是发布时冻结快照，不能被后续版本改写。"""
