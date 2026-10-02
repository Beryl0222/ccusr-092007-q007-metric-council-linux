"""基于角色的访问控制。

角色矩阵（最小权限原则）：

============================== ==========================================
操作                           允许角色
============================== ==========================================
``proposal.submit``            业务部门 PROPOSER
``proposal.read``              全部角色（口径定义本身公开）
``review.file_opinion``        三类专家（限本委员会）
``series.ingest`` / ``.revise`` 业务部门 PROPOSER
``snapshot.publish``           发布人 PUBLISHER
``chart.read``                 决策者、发布人、专家、审计
``lineage.read``               决策者、审计、发布人
``dispute.read``               全部角色（公开分歧）
``enterprise_detail.read``     *无任何角色*——企业明细不进入服务
============================== ==========================================

决策者从图表值一路追到定义、批次、意见，但任何路径都取不到企业
明细：该动作对所有角色一律拒绝，而不是“默认允许再逐个封堵”。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import AccessDeniedError


class Role(str, Enum):
    PROPOSER = "proposer"                # 业务部门
    STATISTICS_REVIEWER = "stat_reviewer"
    FISCAL_REVIEWER = "fiscal_reviewer"
    INDUSTRY_REVIEWER = "industry_reviewer"
    PUBLISHER = "publisher"              # 报告发布人
    DECISION_MAKER = "decision_maker"    # 决策者
    AUDITOR = "auditor"


class Action(str, Enum):
    PROPOSAL_SUBMIT = "proposal.submit"
    PROPOSAL_READ = "proposal.read"
    REVIEW_FILE = "review.file_opinion"
    SERIES_INGEST = "series.ingest"
    SERIES_REVISE = "series.revise"
    SNAPSHOT_PUBLISH = "snapshot.publish"
    CHART_READ = "chart.read"
    LINEAGE_READ = "lineage.read"
    DISPUTE_READ = "dispute.read"
    ENTERPRISE_DETAIL_READ = "enterprise_detail.read"


_ALL_REVIEWERS = frozenset(
    {
        Role.STATISTICS_REVIEWER,
        Role.FISCAL_REVIEWER,
        Role.INDUSTRY_REVIEWER,
    }
)

_READ_ROLES = frozenset(
    _ALL_REVIEWERS | {Role.PUBLISHER, Role.DECISION_MAKER, Role.AUDITOR, Role.PROPOSER}
)

_MATRIX: dict[Action, frozenset[Role]] = {
    Action.PROPOSAL_SUBMIT: frozenset({Role.PROPOSER}),
    Action.PROPOSAL_READ: _READ_ROLES,
    Action.REVIEW_FILE: _ALL_REVIEWERS,
    Action.SERIES_INGEST: frozenset({Role.PROPOSER}),
    Action.SERIES_REVISE: frozenset({Role.PROPOSER}),
    Action.SNAPSHOT_PUBLISH: frozenset({Role.PUBLISHER}),
    Action.CHART_READ: frozenset(
        {Role.DECISION_MAKER, Role.PUBLISHER, Role.AUDITOR} | _ALL_REVIEWERS
    ),
    Action.LINEAGE_READ: frozenset(
        {Role.DECISION_MAKER, Role.AUDITOR, Role.PUBLISHER}
    ),
    Action.DISPUTE_READ: _READ_ROLES,
    # 刻意留空：企业明细对任何角色都不可达
    Action.ENTERPRISE_DETAIL_READ: frozenset(),
}


@dataclass(frozen=True)
class Actor:
    actor_id: str
    name: str
    role: Role


class Policy:
    """集中授权点：所有服务方法在执行前调用 :meth:`require`。"""

    @staticmethod
    def can(actor: Actor, action: Action) -> bool:
        return actor.role in _MATRIX[action]

    @staticmethod
    def require(actor: Actor, action: Action) -> None:
        if not Policy.can(actor, action):
            raise AccessDeniedError(
                actor.role.value,
                action.value,
                "企业明细不进入本服务"
                if action is Action.ENTERPRISE_DETAIL_READ
                else "",
            )
