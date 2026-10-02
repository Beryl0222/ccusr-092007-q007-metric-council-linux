"""单位登记与换算。

每个指标提案声明计量单位；不同来源可能报送不同单位（元/万元/亿元），
入库前必须归一到该量纲的基准单位。换算只允许在同一量纲内进行，
跨量纲（金额与人次）直接拒绝，避免静默地把口径加在一起。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import UnitConversionError


@dataclass(frozen=True)
class Unit:
    code: str
    dimension: str  # amount / count / ratio / area ...
    to_base: float  # 乘以该系数得到基准单位；比例类为 1.0
    name: str

    def convert(self, value: float, target: "Unit") -> float:
        if target.dimension != self.dimension:
            raise UnitConversionError(
                f"量纲冲突：{self.code}({self.dimension}) 不能换算为 "
                f"{target.code}({target.dimension})"
            )
        return value * self.to_base / target.to_base


# 文化消费监测常用单位登记。金额基准单位为"元"，计数基准为"人次"。
_REGISTRY: dict[str, Unit] = {}


def register(unit: Unit) -> None:
    _REGISTRY[unit.code] = unit


def get(code: str) -> Unit:
    try:
        return _REGISTRY[code]
    except KeyError:
        raise UnitConversionError(f"未登记的计量单位：{code}") from None


def convert(value: float, source: str, target: str) -> float:
    return get(source).convert(value, get(target))


def base_unit(dimension: str) -> Unit:
    for unit in _REGISTRY.values():
        if unit.dimension == dimension and unit.to_base == 1.0:
            return unit
    raise UnitConversionError(f"量纲 {dimension} 没有登记基准单位")


def same_dimension(a: str, b: str) -> bool:
    return get(a).dimension == get(b).dimension


register(Unit("CNY_YUAN", "amount", 1.0, "元"))
register(Unit("CNY_WAN", "amount", 1e4, "万元"))
register(Unit("CNY_YI", "amount", 1e8, "亿元"))
register(Unit("PERSON_VISIT", "count", 1.0, "人次"))
register(Unit("VISIT_WAN", "count", 1e4, "万人次"))
register(Unit("PERCENT", "ratio", 1.0, "%"))
register(Unit("RATIO_POINT", "ratio", 1.0, "百分点"))
register(Unit("SQM", "area", 1.0, "平方米"))
