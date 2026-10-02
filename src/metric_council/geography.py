"""行政区划层级与边界变更。

区划变化（撤销、设立、隶属调整）不允许改写历史序列，只登记一张
*生效日映射表*：跨变化点做跨层级汇总时据此判断两个层级单元是否
在同一时点上构成包含关系，从而暴露重复计入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .errors import ValidationError


@dataclass(frozen=True)
class BoundaryChange:
    """一次行政区划调整。"""

    effective_on: date
    """调整生效日；该日（含）起适用新的父子关系。"""

    child: str
    """下级区划代码。"""

    old_parent: str | None
    """调整前的上级；新设区为 ``None``。"""

    new_parent: str | None
    """调整后的上级；撤销区为 ``None``。"""

    note: str = ""


@dataclass
class Geography:
    """行政区划层级表，支持带时间点的隶属查询。"""

    _parents: dict[str, str | None] = field(default_factory=dict)
    _changes: list[BoundaryChange] = field(default_factory=list)

    def add_region(self, code: str, parent: str | None) -> None:
        """登记一个区划单元及其当前上级。"""

        if not code:
            raise ValidationError("区划代码不能为空")
        self._parents[code] = parent

    def record_change(self, change: BoundaryChange) -> None:
        """登记一次边界调整，并把当前隶属推进到调整后状态。"""

        if change.child not in self._parents:
            raise ValidationError(
                f"区划调整引用了未登记的单元：{change.child}"
            )
        self._changes.append(change)
        self._parents[change.child] = change.new_parent

    def parent_on(self, code: str, on: date) -> str | None:
        """返回 *on* 当日区划 *code* 的上级。

        以当前隶属为基线，逆序回放该日之后的调整得到历史隶属。
        """

        if code not in self._parents:
            raise ValidationError(f"未登记的区划单元：{code}")
        parent = self._parents[code]
        for change in sorted(self._changes, key=lambda c: c.effective_on, reverse=True):
            if change.child == code and change.effective_on > on:
                parent = change.old_parent
        return parent

    def path_on(self, code: str, on: date) -> tuple[str, ...]:
        """返回 *on* 当日从根到 *code* 的区划链。"""

        path: list[str] = []
        current: str | None = code
        seen: set[str] = set()
        while current is not None:
            if current in seen:
                raise ValidationError(f"区划层级存在环：{current}")
            seen.add(current)
            path.append(current)
            current = self.parent_on(current, on)
        return tuple(reversed(path))

    def related_on(self, ancestor: str, descendant: str, on: date) -> bool:
        """*on* 当日 *ancestor* 是否是 *descendant* 的上级（含自身）。"""

        return ancestor in self.path_on(descendant, on)

    def changes(self) -> tuple[BoundaryChange, ...]:
        return tuple(sorted(self._changes, key=lambda c: c.effective_on))
