"""项目已确认的最小数据合同（信封层）。

``DomainRecord`` 是所有入库记录共用的信封，跨版本保持既有标识与
时间含义不变：``record_id`` 标识一条记录、``revision`` 为正整数版本、
``occurred_at`` 为业务发生时刻。``schema_version=2`` 在信封之外增加
指标业务要素（见 :mod:`metric_council.proposals`），信封读取器忽略其不
认识的业务字段，因此旧调用方继续可用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import ContractError

ENVELOPE_FIELDS = (
    "schema_version",
    "record_id",
    "domain",
    "occurred_at",
    "revision",
    "source",
)


@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def load_record(path: Path) -> DomainRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    missing = [f for f in ENVELOPE_FIELDS if f not in payload]
    if missing:
        raise ContractError(f"数据记录缺少信封字段：{', '.join(missing)}")
    if not isinstance(payload["revision"], int) or payload["revision"] < 1:
        raise ContractError("revision 必须为正整数")
    if payload.get("domain") != "metric_council":
        raise ContractError(f"未知 domain：{payload.get('domain')}")
    known = {f.name for f in fields(DomainRecord)}
    return DomainRecord(**{k: v for k, v in payload.items() if k in known})
