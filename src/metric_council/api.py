"""零依赖 HTTP JSON 适配层（标准库 WSGI）。

``create_app`` 把 :class:`~metric_council.service.CouncilService` 暴露为
JSON 接口，供各业务系统对接。鉴权采用请求头：

* ``X-Actor-Id``：操作人标识（须与已登记评议人一致）；
* ``X-Actor-Role``：角色代码，见 :class:`metric_council.access.Role`。

端点::

    POST   /api/proposals                      提交 metric_proposal.json
    POST   /api/proposals/{rid}/recusal        声明利益回避  {"relation": ...}
    POST   /api/proposals/{rid}/opinions       影响意见+表决 {"impact","vote"}
    POST   /api/proposals/{rid}/close          三委员会汇审
    POST   /api/series                         {"metric_code","canonical_unit"}
    POST   /api/series/{code}/ingest           {"batch": {...}, "rows": [...]}
    POST   /api/series/{code}/revise           另含 reason / reason_detail
    POST   /api/snapshots                      {"snapshot_id","report_name","selections"}
    GET    /api/snapshots/{id}/value?metric=&region=&period=
    GET    /api/snapshots/{id}/lineage?metric=&region=&period=
    GET    /api/snapshots/{id}/frozen-vs-latest?metric=&region=&period=
    GET    /api/snapshots/{id}/compare?metrics=a,b&region=&period=[&unit=]

错误以 ``{"error": "..."}`` 返回：400 校验/流程错误，403 访问拒绝，
404 对象不存在，409 利益冲突或状态冲突。
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs

from .access import Actor, Role
from .errors import (
    AccessDeniedError,
    ConflictOfInterestError,
    CouncilError,
    ValidationError,
    WorkflowError,
)
from .review import Committee, Vote
from .series import InputBatch, RevisionReason

_STATUS = {
    AccessDeniedError: "403 Forbidden",
    ConflictOfInterestError: "409 Conflict",
    ValidationError: "400 Bad Request",
    WorkflowError: "400 Bad Request",
    KeyError: "404 Not Found",
}


def register_standard_board(svc) -> None:
    """为每个委员会登记两名标准评议人（s1/s2、f1/f2、i1/i2）。"""

    for rid, committee in (
        ("s1", Committee.STATISTICS),
        ("s2", Committee.STATISTICS),
        ("f1", Committee.FISCAL),
        ("f2", Committee.FISCAL),
        ("i1", Committee.INDUSTRY),
        ("i2", Committee.INDUSTRY),
    ):
        if rid not in svc.board._reviewers:  # noqa: SLF001
            svc.board.register_reviewer(rid, committee)


def create_app(svc):
    """构造 WSGI 应用。可用 ``wsgiref`` 直接托管或测试。"""

    register_standard_board(svc)

    def application(environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ["PATH_INFO"].rstrip("/") or "/"
        try:
            actor = _actor_from_headers(environ)
            body = _read_json(environ)
            status, payload = _route(svc, actor, method, path, body, environ)
        except Exception as exc:  # noqa: BLE001 —— 统一错误映射
            status, payload = _error(exc)
        data = json.dumps(payload, ensure_ascii=False, default=_json_default,
                          indent=2).encode("utf-8")
        start_response(
            status,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(data))),
            ],
        )
        return [data]

    return application


def _actor_from_headers(environ) -> Actor:
    actor_id = environ.get("HTTP_X_ACTOR_ID", "")
    role_raw = environ.get("HTTP_X_ACTOR_ROLE", "")
    if not actor_id or not role_raw:
        raise AccessDeniedError("anonymous", "any", "缺少 X-Actor-Id/X-Actor-Role")
    return Actor(actor_id, actor_id, Role(role_raw))


def _read_json(environ):
    if environ["REQUEST_METHOD"] != "POST":
        return {}
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if not length:
        return {}
    raw = environ["wsgi.input"].read(length)
    return json.loads(raw.decode("utf-8"))


def _query(environ) -> dict[str, str]:
    parsed = parse_qs(environ.get("QUERY_STRING", ""))
    return {k: v[0] for k, v in parsed.items()}


def _route(svc, actor, method, path, body, environ):
    # --- 提案 ---
    if method == "POST" and path == "/api/proposals":
        proposal = svc.submit_proposal(actor, body)
        return "201 Created", proposal.to_payload()

    if method == "POST" and path.startswith("/api/proposals/"):
        rid = path.split("/")[3]
        action = path.split("/")[4] if path.count("/") >= 4 else ""
        proposal = svc.proposal(actor, rid)
        if action == "recusal":
            recusal = svc.declare_interest(actor, proposal, body["relation"])
            return "201 Created", {
                "reviewer": recusal.reviewer,
                "committee": recusal.committee.value,
                "relation": recusal.relation,
            }
        if action == "opinions":
            opinion = svc.file_opinion(
                actor, proposal, body["impact"], Vote(body["vote"])
            )
            return "201 Created", {
                "reviewer": opinion.reviewer,
                "committee": opinion.committee.value,
                "vote": opinion.vote.value,
            }
        if action == "close":
            updated, case = svc.close_review(actor, proposal)
            return "200 OK", {
                "status": updated.status.value,
                "opinions": len(case.opinions),
                "recusals": len(case.recusals),
            }
        raise KeyError(path)

    # --- 序列 ---
    if method == "POST" and path == "/api/series":
        series = svc.register_series(
            actor, body["metric_code"], body["canonical_unit"]
        )
        return "201 Created", {
            "metric_code": series.metric_code,
            "canonical_unit": series.canonical_unit,
        }

    if method == "POST" and path.startswith("/api/series/"):
        code = path.split("/")[3]
        action = path.split("/")[4]
        series = svc.publisher._series[code]  # noqa: SLF001
        batch = _batch_from(body["batch"])
        if action == "ingest":
            version = svc.ingest_rows(actor, series, batch, body["rows"])
        elif action == "revise":
            version = svc.revise_rows(
                actor, series,
                RevisionReason(body["reason"]),
                body["reason_detail"],
                batch,
                body["rows"],
            )
        else:
            raise KeyError(path)
        return "201 Created", {
            "version": version.version,
            "reason": version.reason.value,
            "batch_id": version.batch.batch_id,
        }

    # --- 快照 ---
    if method == "POST" and path == "/api/snapshots":
        selections = {
            code: tuple(regions)
            for code, regions in body["selections"].items()
        }
        snap = svc.publish_report(
            actor, body["snapshot_id"], body["report_name"], selections
        )
        return "201 Created", {
            "snapshot_id": snap.snapshot_id,
            "published_at": snap.published_at.isoformat(),
            "metric_versions": snap.metric_versions,
        }

    if method == "GET" and path.startswith("/api/snapshots/"):
        parts = path.split("/")
        snap_id = parts[3]
        action = parts[4]
        snapshot = svc.publisher.snapshot(snap_id)
        q = _query(environ)
        if action == "value":
            value = svc.chart_value(
                actor, snapshot, q["metric"], q["region"], q["period"]
            )
            return "200 OK", {"value": value}
        if action == "lineage":
            return "200 OK", svc.chart_lineage(
                actor, snapshot, q["metric"], q["region"], q["period"]
            )
        if action == "frozen-vs-latest":
            return "200 OK", svc.frozen_vs_latest(
                actor, snapshot, q["metric"], q["region"], q["period"]
            )
        if action == "compare":
            metrics = tuple(q["metrics"].split(","))
            return "200 OK", svc.compare_calibers(
                actor, snapshot, metrics, q["region"], q["period"],
                common_unit=q.get("unit"),
            )
        raise KeyError(path)

    raise KeyError(path)


def _batch_from(payload: dict) -> InputBatch:
    return InputBatch(
        batch_id=payload["batch_id"],
        source_id=payload["source_id"],
        received_at=datetime.fromisoformat(payload["received_at"]),
        submitted_by=payload["submitted_by"],
    )


def _error(exc: Exception) -> tuple[str, dict]:
    for error_type, status in _STATUS.items():
        if isinstance(exc, error_type):
            return status, {"error": str(exc), "type": type(exc).__name__}
    if isinstance(exc, CouncilError):
        return "400 Bad Request", {"error": str(exc), "type": type(exc).__name__}
    return "500 Internal Server Error", {
        "error": str(exc), "type": type(exc).__name__
    }


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, frozenset):
        return sorted(value)
    return str(value)
