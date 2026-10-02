"""计量单位登记与换算。

只有同一 *单位族* 之间允许换算。例如 ``万元`` 与 ``亿元`` 同属人民币族，
``%`` 与 ``小数比率`` 同属比率族；而 ``人次``（累计流量）与 ``人``
（去重存量）分属不同族，任何换算都被拒绝，以免把两种口径悄悄画等号。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ValidationError

# 单位族：族内可换算，跨族禁止
FAMILY_CURRENCY = "currency_cny"
FAMILY_PERSON_VISITS = "person_visits"  # 人次（不去重）
FAMILY_PERSONS = "persons"              # 人（去重）
FAMILY_TICKETS = "tickets"
FAMILY_RATIO = "ratio"


@dataclass(frozen=True)
class Unit:
    code: str
    """口径提案中书写的单位代码，如 ``CNY_10k_YUAN``。"""

    label: str
    """中文展示名。"""

    family: str
    """单位族；只有同族单位之间可以换算。"""

    factor: float
    """换算到本族基准单位时的乘数：基准值 = 原值 * factor。"""


# 基准单位：元 / 人次 / 人 / 张 / 小数比率(1.0=100%)
_REGISTRY: dict[str, Unit] = {}


def register(unit: Unit) -> None:
    _REGISTRY[unit.code] = unit


def get(code: str) -> Unit:
    try:
        return _REGISTRY[code]
    except KeyError as exc:
        raise ValidationError(f"未登记的计量单位：{code}") from exc


def is_registered(code: str) -> bool:
    return code in _REGISTRY


register(Unit("CNY_YUAN", "元", FAMILY_CURRENCY, 1.0))
register(Unit("CNY_10k_YUAN", "万元", FAMILY_CURRENCY, 1e4))
register(Unit("CNY_100M_YUAN", "亿元", FAMILY_CURRENCY, 1e8))
register(Unit("PERSON_VISIT", "人次", FAMILY_PERSON_VISITS, 1.0))
register(Unit("PERSON", "人", FAMILY_PERSONS, 1.0))
register(Unit("TICKET", "张", FAMILY_TICKETS, 1.0))
register(Unit("RATIO", "比率(小数)", FAMILY_RATIO, 1.0))
register(Unit("PERCENT", "%", FAMILY_RATIO, 0.01))
register(Unit("PERMILLE", "‰", FAMILY_RATIO, 0.001))


def convert(value: float, source: str, target: str) -> float:
    """把 *value* 从 *source* 单位换算到 *target* 单位。

    跨单位族（如 万元→人次、人次→人）抛出 :class:`ValidationError`。
    """

    src = get(source)
    dst = get(target)
    if src.family != dst.family:
        raise ValidationError(
            f"单位族不一致，禁止跨族换算：{src.label}（{src.family}）→ "
            f"{dst.label}（{dst.family}）"
        )
    return value * src.factor / dst.factor
