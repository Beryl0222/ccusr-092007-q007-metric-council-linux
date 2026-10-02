"""访问控制。

角色与数据粒度分离：能否"操作流程"由权限位决定，能否"看到企业明细"
由独立的 ``detail.read`` 决定。决策者（decision_maker）可以从图表值
一路追溯到定义、批次和审议意见，但这些视图只包含区域汇总值与
批次/意见元数据，**不包含** enterprise_id 等企业明细——明细仅对
经授权的数据核查角色开放。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import PermissionDenied

# 权限位
P_PROPOSAL_SUBMIT = "proposal.submit"
P_REVIEW_OPEN = "review.open"
P_OPINION = "review.opinion"
P_VOTE = "review.vote"
P_INGEST = "ingest.submit"
P_SERIES_REVISE = "series.revise"
P_REPORT_FREEZE = "report.freeze"
P_REPORT_READ = "report.read"
P_LINEAGE_READ = "lineage.read"
P_DISAGREEMENT_READ = "disagreement.read"
P_DERIVED_CREATE = "derived.create"
P_DETAIL_READ = "detail.read"        # 企业明细；独立于所有汇总视图
P_REGION_ADMIN = "region.admin"

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    # 业务部门：提交/修订本部门提案
    "dept_submitter": frozenset({P_PROPOSAL_SUBMIT}),
    # 三类专家：提影响意见、表决（回避由 review 层强制执行）
    "expert_statistics": frozenset({P_OPINION, P_VOTE, P_DISAGREEMENT_READ}),
    "expert_fiscal": frozenset({P_OPINION, P_VOTE, P_DISAGREEMENT_READ}),
    "expert_industry": frozenset({P_OPINION, P_VOTE, P_DISAGREEMENT_READ}),
    # 审议召集人：开启/结束表决、建派生指标、登记公开分歧
    "review_chair": frozenset({
        P_REVIEW_OPEN, P_DERIVED_CREATE, P_DISAGREEMENT_READ, P_LINEAGE_READ,
    }),
    # 统计机构：采集、区划维护、序列修订
    "data_engineer": frozenset({
        P_INGEST, P_SERIES_REVISE, P_REGION_ADMIN, P_LINEAGE_READ,
        P_DISAGREEMENT_READ,
    }),
    # 授权核查角色：唯一可接触企业明细的角色
    "detail_auditor": frozenset({P_DETAIL_READ, P_LINEAGE_READ}),
    # 报告编辑：冻结发布
    "report_editor": frozenset({P_REPORT_FREEZE, P_REPORT_READ}),
    # 决策者：看报告、看谱系与分歧，但永无 detail.read
    "decision_maker": frozenset({
        P_REPORT_READ, P_LINEAGE_READ, P_DISAGREEMENT_READ,
    }),
}


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    roles: frozenset[str]
    dept: str | None = None
    expert_id: str | None = None

    def __post_init__(self) -> None:
        unknown = self.roles - ROLE_PERMISSIONS.keys()
        if unknown:
            raise PermissionDenied(f"未定义的角色：{sorted(unknown)}")

    def permits(self, permission: str) -> bool:
        return any(permission in ROLE_PERMISSIONS[r] for r in self.roles)


def require(user: User, permission: str, *, what: str = "") -> None:
    if not user.permits(permission):
        raise PermissionDenied(
            f"用户 {user.user_id}（角色 {sorted(user.roles)}）无权执行 "
            f"{permission}{('：' + what) if what else ''}"
        )


def ensure_no_enterprise_detail(user: User, payload: dict | list,
                                _path: str = "") -> None:
    """递归检查对外载荷中绝不携带企业明细字段。

    这是权限之外的第二道防线：即使装配视图时误放入了 enterprise_id，
    也在这里被拦下，而不是依赖调用方"记得不放"。
    """
    forbidden = {"enterprise_id", "enterprise_name", "tax_id"}
    if isinstance(payload, dict):
        leaked = forbidden & payload.keys()
        if leaked and not user.permits(P_DETAIL_READ):
            raise PermissionDenied(
                f"视图 {_path or '(根)'} 含企业明细字段 {sorted(leaked)}，"
                "决策者视图不得包含企业明细"
            )
        for key, value in payload.items():
            ensure_no_enterprise_detail(user, value, f"{_path}.{key}")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            ensure_no_enterprise_detail(user, item, f"{_path}[{i}]")
