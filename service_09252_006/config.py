"""运行配置：全部来自环境变量；运行数据默认放在用户数据目录，绝不写入源码目录。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "qev"


@dataclass(frozen=True)
class Settings:
    database_url: str


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """读取 QEV_DATABASE_URL；缺省为 $XDG_DATA_HOME/qev/qev.db。"""
    env = dict(os.environ if env is None else env)
    url = env.get("QEV_DATABASE_URL")
    if not url:
        data_dir = _default_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{data_dir / 'qev.db'}"
    return Settings(database_url=url)
