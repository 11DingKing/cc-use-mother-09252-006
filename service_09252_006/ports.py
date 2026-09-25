"""可替换端口：时间与标识生成。测试中可注入固定时钟与确定性 ID 以稳定复现状态变化。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """时间来源端口。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产时钟：始终返回 aware UTC。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class IdGenerator(Protocol):
    """标识生成端口。"""

    def new_id(self) -> str: ...


class UuidGenerator:
    def new_id(self) -> str:
        return uuid.uuid4().hex
