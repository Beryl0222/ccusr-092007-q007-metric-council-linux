"""测试辅助：构造最小合法提案与一条走通的审议链。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council import CouncilService
from metric_council.access import User
from metric_council.review import Expert

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 20, 9, 0, tzinfo=TZ)

EXPERTS = [
    Expert("e-s1", "张统计", "statistics"),
    Expert("e-s2", "李统计", "statistics"),
    Expert("e-f1", "王财政", "fiscal"),
    Expert("e-f2", "赵财政", "fiscal"),
    Expert("e-i1", "孙行业", "industry"),
    Expert("e-i2", "周行业", "industry"),
]

ROLE = {
    "e-s1": "expert_statistics", "e-s2": "expert_statistics",
    "e-f1": "expert_fiscal", "e-f2": "expert_fiscal",
    "e-i1": "expert_industry", "e-i2": "expert_industry",
}


def chair() -> User:
    return User("u-chair", "召集人", frozenset({"review_chair"}))


def submitter() -> User:
    return User("u-dept", "业务处", frozenset({"dept_submitter"}))


def engineer() -> User:
    return User("u-eng", "统计师", frozenset({"data_engineer"}))


def editor() -> User:
    return User("u-ed", "编辑", frozenset({"report_editor"}))


def decision_maker() -> User:
    return User("u-dm", "决策者", frozenset({"decision_maker"}))


def expert_user(expert_id: str) -> User:
    return User(expert_id, expert_id, frozenset({ROLE[expert_id]}),
                expert_id=expert_id)


def base_proposal() -> dict:
    import json
    return json.loads(
        (Path(__file__).parents[1] / "fixtures" / "metric_proposal.json")
        .read_text(encoding="utf-8"))


def new_service_with_experts() -> CouncilService:
    svc = CouncilService()
    for expert in EXPERTS:
        svc.register_expert(chair(), expert)
    return svc


def approve(svc: CouncilService, payload: dict, *, now: datetime = NOW,
            disagreement_title: str = "测试分歧"):
    """提交并让三组一致通过，返回提案。payload 会被复制并置为 draft。"""
    payload = dict(payload)
    payload["status"] = "draft"
    proposal = svc.submit_proposal(submitter(), payload)
    svc.open_review(chair(), proposal.record_id, now)
    for expert_id in ROLE:
        svc.add_opinion(expert_user(expert_id), proposal.record_id, "影响意见", now)
    for expert_id in ROLE:
        svc.cast_vote(expert_user(expert_id), proposal.record_id, True, "赞成", now)
    svc.close_vote(chair(), proposal.record_id, disagreement_title, now)
    return proposal
