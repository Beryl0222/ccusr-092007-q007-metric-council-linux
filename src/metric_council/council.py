"""服务门面：把提案、审议、采集、序列、快照与权限串成一条链。

对外只暴露"决策者视角"的汇总对象，企业明细永不进入这些返回值。
核心查询：

- :meth:`CouncilService.trace` —— 从一个图表值追到发布快照、当时冻结的
  定义（含 revision）、输入批次和三组审议意见/表决；
- :meth:`CouncilService.compare` —— 并排查看替代口径在同一区域/期的值、
  差异以及它们之间的公开分歧；冲突口径无法合并，但可以对照。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import access
from .access import User, require
from .ingest import (
    REASON_LATE_REPORT,
    REASON_REGION_CHANGE,
    REASON_SOURCE_CORRECTION,
    AcceptedPoint,
    IngestService,
    InputBatch,
)
from .proposals import MetricProposal, proposal_from_dict
from .regions import RegionMapping, RegionRegistry
from .review import (
    BallotResult,
    Expert,
    PublicDisagreement,
    ReviewBoard,
)
from .series import Cell, ReportSnapshot, SeriesStore, SeriesVersion, VersioningError
from .storage import EventStore
from .units import convert as convert_value


class CouncilService:
    def __init__(self, *, event_log: str | Path | None = None) -> None:
        self.board = ReviewBoard()
        self.regions = RegionRegistry()
        self.ingest = IngestService(self.regions)
        self.series = SeriesStore()
        self.store = EventStore(event_log) if event_log else None
        self._proposal_seq = 0
        self._version_seq: dict[str, int] = {}
        self._snapshot_seq = 0

    # —— 组织与区划 ——

    def register_expert(self, user: User, expert: Expert) -> None:
        require(user, access.P_REVIEW_OPEN, what="登记专家")
        self.board.register_expert(expert)
        self._emit("expert_registered", {
            "expert_id": expert.expert_id, "panel": expert.panel})

    def register_region_mapping(self, user: User, mapping: RegionMapping) -> None:
        require(user, access.P_REGION_ADMIN, what="登记区划变更")
        self.regions.record(mapping)
        self._emit("region_mapping", {
            "old_code": mapping.old_code, "new_code": mapping.new_code,
            "effective": mapping.effective.isoformat(), "reason": mapping.reason})

    def set_region_parent(self, user: User, child: str, parent: str) -> None:
        require(user, access.P_REGION_ADMIN, what="维护区划层级")
        self.ingest.set_parent(child, parent)

    # —— 提案与审议 ——

    def submit_proposal(self, user: User, payload: dict) -> MetricProposal:
        require(user, access.P_PROPOSAL_SUBMIT, what="提交指标提案")
        proposal = proposal_from_dict(payload)
        self.board.submit(proposal)
        self._emit("proposal_submitted", {"record_id": proposal.record_id,
                                          "code": proposal.code})
        return proposal

    def open_review(self, user: User, proposal_id: str, now: datetime) -> MetricProposal:
        require(user, access.P_REVIEW_OPEN, what="开启审议")
        proposal = self.board.open_review(proposal_id)
        self._emit("review_opened", {"record_id": proposal_id})
        return proposal

    def add_opinion(self, user: User, proposal_id: str, impact: str,
                    now: datetime):
        require(user, access.P_OPINION, what="提出影响意见")
        expert_id = self._require_expert_identity(user)
        opinion = self.board.add_opinion(proposal_id, expert_id, impact, now)
        self._emit("opinion_added", {
            "record_id": proposal_id, "expert_id": expert_id,
            "panel": opinion.panel, "recused": opinion.recused})
        return opinion

    def cast_vote(self, user: User, proposal_id: str, approve: bool,
                  rationale: str, now: datetime):
        require(user, access.P_VOTE, what="表决")
        expert_id = self._require_expert_identity(user)
        vote = self.board.cast_vote(proposal_id, expert_id, approve, rationale, now)
        self._emit("vote_cast", {
            "record_id": proposal_id, "expert_id": expert_id,
            "panel": vote.panel, "approve": approve})
        return vote

    def close_vote(self, user: User, proposal_id: str, disagreement_title: str,
                   now: datetime) -> BallotResult:
        require(user, access.P_REVIEW_OPEN, what="结束表决")
        result = self.board.close_vote(proposal_id, disagreement_title, now)
        proposal = self.board.proposals[proposal_id]
        self._emit("vote_closed", {
            "record_id": proposal_id, "approved": result.approved,
            "status": proposal.status})
        if result.approved:
            # 通过后定义对采集层生效。
            self.ingest.register_proposal(proposal)
        return result

    def create_derived(self, user: User, code: str, name: str,
                       parent_ids: list[str], now: datetime):
        require(user, access.P_DERIVED_CREATE, what="形成派生指标")
        derived = self.board.create_derived(code, name, parent_ids, now)
        self._emit("derived_created", {
            "code": code, "parent_ids": list(parent_ids), "formula": "sum"})
        return derived

    def register_conflict(self, user: User, proposal_id: str, existing_code: str,
                          now: datetime) -> PublicDisagreement:
        require(user, access.P_REVIEW_OPEN, what="登记公开分歧")
        proposal = self.board.proposals[proposal_id]
        disagreement = self.board.register_conflict(proposal, existing_code, now)
        self._emit("disagreement_registered", {
            "disagreement_id": disagreement.disagreement_id,
            "proposal_ids": list(disagreement.proposal_ids)})
        return disagreement

    # —— 采集与序列 ——

    def ingest_batch(self, user: User, batch: InputBatch,
                     metric_code: str) -> list[AcceptedPoint]:
        require(user, access.P_INGEST, what="提交输入批次")
        points = self.ingest.ingest(batch, metric_code)
        self._emit("batch_ingested", {
            "batch_id": batch.batch_id, "metric": metric_code,
            "source_id": batch.source_id, "points": len(points),
            "late_points": sum(1 for p in points if p.late)})
        return points

    def build_initial_series(self, user: User, metric_code: str,
                             now: datetime) -> SeriesVersion:
        require(user, access.P_SERIES_REVISE, what="建立初始序列")
        proposal = self._approved_proposal(metric_code)
        self._version_seq.setdefault(metric_code, 0)
        self._version_seq[metric_code] += 1
        version = self.series.build_initial(
            metric_code, proposal, self.ingest.points(metric_code), now,
            version_id=f"{metric_code}-v{self._version_seq[metric_code]}")
        self._emit("series_version", {
            "metric": metric_code, "version_id": version.version_id,
            "reason": version.reason, "seq": version.seq})
        return version

    def revise_series(self, user: User, metric_code: str, reason: str,
                      note: str, now: datetime) -> SeriesVersion:
        require(user, access.P_SERIES_REVISE, what="修订序列")
        if reason not in (REASON_SOURCE_CORRECTION, REASON_REGION_CHANGE,
                          REASON_LATE_REPORT):
            raise ValueError(f"非法修订原因：{reason}")
        version = self._make_revision(user, metric_code, reason, note, now)
        self._emit("series_version", {
            "metric": metric_code, "version_id": version.version_id,
            "reason": reason, "note": note, "seq": version.seq,
            "changed_cells": [list(c) for c in version.changed_cells]})
        return version

    def correct_source(self, user: User, old_batch_id: str,
                       new_batch: InputBatch, metric_code: str, note: str,
                       now: datetime) -> SeriesVersion:
        """来源更正：撤回旧批次、重走校验，并另立带原因的新版本。"""
        require(user, access.P_SERIES_REVISE, what="来源更正")
        points = self.ingest.correct_batch(old_batch_id, new_batch, metric_code)
        self._emit("batch_corrected", {
            "old_batch_id": old_batch_id,
            "new_batch_id": new_batch.batch_id, "metric": metric_code,
            "points": len(points)})
        return self._make_revision(user, metric_code, REASON_SOURCE_CORRECTION,
                                   note, now)

    def apply_region_change(self, user: User, metric_code: str, note: str,
                            now: datetime) -> SeriesVersion:
        """登记区划变更后重算归属，并以 region_change 原因另立版本。"""
        require(user, access.P_SERIES_REVISE, what="区划变更重算")
        moved = self.ingest.reapply_region_mappings(metric_code)
        self._emit("region_reapplied", {"metric": metric_code, "moved": moved})
        return self._make_revision(user, metric_code, REASON_REGION_CHANGE,
                                   f"{note}；归属变化 {len(moved)} 个活动", now)

    def _make_revision(self, user: User, metric_code: str, reason: str,
                       note: str, now: datetime) -> SeriesVersion:
        proposal = self._approved_proposal(metric_code)
        self._version_seq[metric_code] = self._version_seq.get(metric_code, 1) + 1
        try:
            return self.series.revise(
                metric_code, proposal, self.ingest.points(metric_code),
                reason, note, now,
                version_id=f"{metric_code}-v{self._version_seq[metric_code]}")
        except VersioningError:
            # 重算未改变任何单元：不制造无意义版本，回退序号。
            self._version_seq[metric_code] -= 1
            raise

    def freeze_report(self, user: User, report_id: str, title: str,
                      metric_codes: list[str], now: datetime) -> ReportSnapshot:
        require(user, access.P_REPORT_FREEZE, what="冻结发布快照")
        self._snapshot_seq += 1
        snapshot = self.series.freeze(
            report_id, title, metric_codes, now,
            snapshot_id=f"{report_id}-snap{self._snapshot_seq}")
        self._emit("report_frozen", {
            "snapshot_id": snapshot.snapshot_id, "report_id": report_id,
            "metric_versions": snapshot.metric_versions})
        return snapshot

    # —— 决策者查询 ——

    def trace(self, user: User, snapshot_id: str, metric_code: str,
              region_code: str, period: str) -> dict:
        """图表值 → 快照 → 定义 → 输入批次 → 审议意见的完整谱系。"""
        require(user, access.P_REPORT_READ, what="读取报告")
        require(user, access.P_LINEAGE_READ, what="追溯谱系")
        snapshot = self.series.snapshot(snapshot_id)
        cell: Cell = (region_code, period)
        value = snapshot.value(metric_code, region_code, period)
        version_id = snapshot.metric_versions[metric_code]
        version = self.series.version(version_id)

        proposal = self._proposal_for_revision(metric_code,
                                               version.proposal_revision)
        ballot = self.board.ballots.get(proposal.record_id)
        return {
            "metric_code": metric_code,
            "region_code": region_code,
            "period": period,
            "chart_value": {"value": value, "unit": proposal.unit},
            "snapshot": {
                "snapshot_id": snapshot.snapshot_id,
                "report_id": snapshot.report_id,
                "title": snapshot.title,
                "frozen_at": snapshot.frozen_at.isoformat(),
            },
            "series_version": {
                "version_id": version.version_id, "seq": version.seq,
                "reason": version.reason, "note": version.note,
                "based_on_version": version.based_on_version,
                "created_at": version.created_at.isoformat(),
            },
            "definition": {
                "record_id": proposal.record_id,
                "revision": proposal.revision,
                "code": proposal.code,
                "name": proposal.name,
                "definition": proposal.definition,
                "unit": proposal.unit,
                "industries": list(proposal.industries),
                "dedup": {
                    "identity_keys": list(proposal.dedup.identity_keys),
                    "hierarchy_rule": proposal.dedup.hierarchy_rule,
                    "exclusions": list(proposal.dedup.exclusions),
                },
                "data_sources": [
                    {"source_id": d.source_id, "name": d.name,
                     "granularity": d.granularity}
                    for d in proposal.data_sources
                ],
                "effective": {
                    "start": proposal.effective.start.isoformat(),
                    "end": proposal.effective.end.isoformat()
                    if proposal.effective.end else None,
                },
                "submitting_dept": proposal.submitting_dept,
            },
            "input_batches": list(snapshot.provenance(metric_code,
                                                      region_code, period)),
            "review": self._review_view(proposal.record_id, ballot),
        }

    def compare(self, user: User, metric_codes: list[str], region_code: str,
                period: str, *, snapshot_id: str | None = None) -> dict:
        """并排查看多种口径在同一单元上的值与差异。

        可基于已发布快照（默认）或各指标当前最新版本；不同口径间若存在
        公开分歧，一并返回分歧原因。所有值换算到第一种口径的单位展示。
        """
        require(user, access.P_REPORT_READ, what="读取报告")
        if len(metric_codes) < 2:
            raise ValueError("并排对比至少需要两个指标代码")

        snapshot = self.series.snapshot(snapshot_id) if snapshot_id else None
        rows = []
        display_unit = None
        for code in metric_codes:
            version = (self.series.version(snapshot.metric_versions[code])
                       if snapshot else self.series.current(code))
            if version is None:
                raise ValueError(f"指标 {code} 尚无序列版本")
            proposal = self._proposal_for_revision(code, version.proposal_revision)
            raw_value = version.values.get((region_code, period))
            if display_unit is None:
                display_unit = proposal.unit
            shown = (convert_value(raw_value, proposal.unit, display_unit)
                     if raw_value is not None else None)
            rows.append({
                "metric_code": code,
                "record_id": proposal.record_id,
                "revision": proposal.revision,
                "name": proposal.name,
                "definition": proposal.definition,
                "version_id": version.version_id,
                "version_reason": version.reason,
                "value": shown,
                "unit": display_unit,
            })

        baseline = rows[0]["value"]
        for row in rows:
            row["delta_vs_first"] = (
                round(row["value"] - baseline, 6)
                if row["value"] is not None and baseline is not None else None
            )

        disagreements = []
        for i, a in enumerate(metric_codes):
            for b in metric_codes[i + 1:]:
                d = self._disagreement_between(a, b)
                if d is not None:
                    disagreements.append({
                        "disagreement_id": d.disagreement_id,
                        "title": d.title, "reason": d.reason,
                        "between": [a, b],
                    })
        return {
            "region_code": region_code, "period": period,
            "unit": display_unit,
            "snapshot_id": snapshot.snapshot_id if snapshot else None,
            "calibers": rows,
            "disagreements": disagreements,
        }

    def public_disagreements(self, user: User) -> list[dict]:
        require(user, access.P_DISAGREEMENT_READ, what="查看公开分歧")
        return [{
            "disagreement_id": d.disagreement_id,
            "title": d.title,
            "proposal_ids": list(d.proposal_ids),
            "reason": d.reason,
            "created_at": d.created_at.isoformat(),
        } for d in self.board.disagreements]

    # —— 内部 ——

    def _approved_proposal(self, metric_code: str) -> MetricProposal:
        candidates = [p for p in self.board.proposals.values()
                      if p.code == metric_code and p.status == "approved"]
        if not candidates:
            raise ValueError(f"指标 {metric_code} 没有已通过审议的定义")
        return max(candidates, key=lambda p: p.revision)

    def _proposal_for_revision(self, metric_code: str,
                               revision: int) -> MetricProposal:
        matches = [p for p in self.board.proposals.values()
                   if p.code == metric_code and p.revision == revision]
        if matches:
            return matches[0]
        # 修订链断裂不应发生；回退到该指标当前通过的定义。
        return self._approved_proposal(metric_code)

    def _review_view(self, proposal_id: str,
                     ballot: BallotResult | None) -> dict:
        opinions = [
            {"expert_id": o.expert_id, "panel": o.panel, "impact": o.impact,
             "recused": o.recused}
            for o in self.board.opinions_for(proposal_id)
        ]
        votes = [
            {"expert_id": v.expert_id, "panel": v.panel, "approve": v.approve,
             "rationale": v.rationale}
            for v in self.board.votes_for(proposal_id)
        ]
        return {
            "opinions": opinions,
            "votes": votes,
            "tallies": ([
                {"panel": t.panel, "eligible": t.eligible,
                 "votes_for": t.votes_for, "votes_against": t.votes_against,
                 "recused": t.recused, "passed": t.passed}
                for t in ballot.tallies] if ballot else []),
        }

    def _disagreement_between(self, code_a: str,
                              code_b: str) -> PublicDisagreement | None:
        ids_a = {p.record_id for p in self.board.proposals.values()
                 if p.code == code_a}
        ids_b = {p.record_id for p in self.board.proposals.values()
                 if p.code == code_b}
        for d in self.board.disagreements:
            ids = set(d.proposal_ids)
            if (ids & ids_a) and (ids & ids_b):
                return d
        return None

    def _require_expert_identity(self, user: User) -> str:
        if user.expert_id is None:
            from .errors import PermissionDenied
            raise PermissionDenied(
                f"用户 {user.user_id} 未绑定专家身份，不能参与审议")
        if user.expert_id not in self.board.experts:
            from .errors import WorkflowError
            raise WorkflowError(f"专家 {user.expert_id} 尚未在委员会登记")
        return user.expert_id

    def _emit(self, event_type: str, payload: dict) -> None:
        if self.store is not None:
            self.store.append(event_type, payload)
