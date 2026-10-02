"""行政区划版本。

区划调整（撤市设区、托管变更）改变同一地理标识的统计范围，
是触发"新序列版本"的三类原因之一。这里登记区划代码在某个生效日的归属，
用于判断两批数据的 region_code 是否仍指代同一口径范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RegionMapping:
    """一次区划变更：old_code 在 effective 当日起并入 new_code。"""

    old_code: str
    new_code: str
    effective: date
    reason: str


class RegionRegistry:
    def __init__(self) -> None:
        self._mappings: list[RegionMapping] = []

    def record(self, mapping: RegionMapping) -> None:
        self._mappings.append(mapping)

    def code_on(self, code: str, day: date) -> str:
        """返回 code 在 day 这一天有效的区划代码（跟随最新的并入链）。"""
        changed = True
        current = code
        while changed:
            changed = False
            for m in self._mappings:
                if m.old_code == current and day >= m.effective:
                    current = m.new_code
                    changed = True
        return current

    def affected_codes(self) -> set[str]:
        return {m.old_code for m in self._mappings}

    def mapping_for(self, old_code: str) -> RegionMapping | None:
        for m in self._mappings:
            if m.old_code == old_code:
                return m
        return None
