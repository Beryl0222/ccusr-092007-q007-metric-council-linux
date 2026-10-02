"""测试共享：构造提案与服务环境的工厂。"""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from metric_council.access import Actor, Role
from metric_council.proposals import (
    DataSourceRef,
    DedupBoundary,
    EffectiveRange,
    MetricProposal,
    ProposalStatus,
)
from metric_council.review import Committee
from metric_council.service import CouncilService

FIXTURE = Path(__file__).parents[1] / "fixtures" / "metric_proposal.json"


def make_proposal(
    record_id: str = "r1",
    *,
    metric_code: str = "CUL_CONS_WIDE",
    metric_name: str = "文化消费（宽口径）",
    definition: str = "数字展陈+联票+创作者收入",
    unit_code: str = "CNY_10k_YUAN",
    industries=("digital_exhibition", "cultural_tourism"),
    dedup: DedupBoundary | None = None,
    effective: EffectiveRange | None = None,
    revision: int = 1,
    status: ProposalStatus = ProposalStatus.DRAFT,
    sources=(DataSourceRef("ds1", "平台汇总", "aggregate"),),
    occurred_at: datetime | None = None,
) -> MetricProposal:
    return MetricProposal(
        record_id=record_id,
        occurred_at=occurred_at or datetime(2026, 9, 20, 9, 0),
        revision=revision,
        source="测试构造",
        metric_code=metric_code,
        metric_name=metric_name,
        definition=definition,
        unit_code=unit_code,
        industries=frozenset(industries),
        sources=tuple(sources),
        dedup=dedup
        or DedupBoundary("enterprise", ("enterprise_credit_code",), "month"),
        effective=effective or EffectiveRange(date(2025, 1, 1)),
        proposer="t-proposer",
        status=status,
    )


def proposal_payload(**overrides) -> dict:
    from metric_council.proposals import proposal_from_payload
    import json

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.update(overrides)
    return payload


def build_service_with_board() -> tuple[CouncilService, dict[str, Actor]]:
    """带 3 委员会（每委员会 2 名专家）的服务。"""

    svc = CouncilService()
    actors = {
        "proposer": Actor("p1", "业务处", Role.PROPOSER),
        "s1": Actor("s1", "统计甲", Role.STATISTICS_REVIEWER),
        "s2": Actor("s2", "统计乙", Role.STATISTICS_REVIEWER),
        "f1": Actor("f1", "财政甲", Role.FISCAL_REVIEWER),
        "f2": Actor("f2", "财政乙", Role.FISCAL_REVIEWER),
        "i1": Actor("i1", "行业甲", Role.INDUSTRY_REVIEWER),
        "i2": Actor("i2", "行业乙", Role.INDUSTRY_REVIEWER),
        "publisher": Actor("pub", "发布岗", Role.PUBLISHER),
        "dm": Actor("dm", "决策者", Role.DECISION_MAKER),
        "auditor": Actor("aud", "审计", Role.AUDITOR),
    }
    mapping = {
        "s1": Committee.STATISTICS,
        "s2": Committee.STATISTICS,
        "f1": Committee.FISCAL,
        "f2": Committee.FISCAL,
        "i1": Committee.INDUSTRY,
        "i2": Committee.INDUSTRY,
    }
    for key, committee in mapping.items():
        svc.board.register_reviewer(actors[key].actor_id, committee)
    return svc, actors


def approve(svc: CouncilService, actors: dict[str, Actor], proposal, *,
            votes=None) -> object:
    """每委员会派 1 人表决后关闭审议。"""

    votes = votes or {"s1": None, "f1": None, "i1": None}
    from metric_council.review import Vote

    for key, vote in votes.items():
        svc.file_opinion(actors[key], proposal, f"{key} 的影响意见", vote or Vote.APPROVE)
    out, _ = svc.close_review(actors["s1"], proposal)
    return out
