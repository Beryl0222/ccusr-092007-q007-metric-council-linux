"""读取项目已确认的最小数据合同，不包含业务流程实现。

完整提案结构见 :mod:`metric_council.proposals`；本模块保留信封合同
（``schema_version`` / ``record_id`` / ``domain`` / ``occurred_at`` /
``revision`` / ``source``）及其读取入口，使既有调用方不受影响——
``load_record`` 只取信封字段，忽略提案文件中的前向新增字段。
"""

from __future__ import annotations

from .proposals import DomainRecord, load_record

__all__ = ["DomainRecord", "load_record"]
