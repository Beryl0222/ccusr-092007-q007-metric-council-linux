"""JSON 追加存储。

所有状态变更以"事件"形式追加到 JSONL 文件，不做就地更新——与
"历史不可原地改写"的领域铁律一致。这里提供的是最小持久化：
服务每次重放事件重建内存状态；生产环境可替换为事件库而不改动领域层。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


def _default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, frozenset):
        return sorted(obj)
    if isinstance(obj, set):
        return sorted(obj)
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(f"不可序列化的对象：{type(obj)!r}")


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, payload: Any) -> dict:
        event = {"type": event_type, "payload": payload}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=_default))
            fh.write("\n")
        return event

    def replay(self, handlers: dict[str, Callable[[dict], None]]) -> int:
        if not self.path.exists():
            return 0
        count = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            handler = handlers.get(event["type"])
            if handler is not None:
                handler(event["payload"])
            count += 1
        return count

    def read_all(self) -> Iterable[dict]:
        if not self.path.exists():
            return ()
        return (json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip())
