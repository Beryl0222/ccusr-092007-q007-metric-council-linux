"""文化指标口径审议后端。

模块分层：
- contracts/envelope：跨版本数据信封
- proposals：业务部门提交的指标口径定义（v2 合同）
- review：统计/财政/行业三组审议、回避、表决与公开分歧
- ingest：单位换算、去重与跨层级重复校验、输入批次
- series：序列版本（更正/区划/迟报）与发布快照
- access：访问控制
- council：服务门面，串联提案→审议→采集→发布→追溯
"""

from .contracts import DomainRecord, load_record
from .proposals import MetricProposal, load_proposal
from .council import CouncilService

__all__ = [
    "DomainRecord",
    "load_record",
    "MetricProposal",
    "load_proposal",
    "CouncilService",
]
