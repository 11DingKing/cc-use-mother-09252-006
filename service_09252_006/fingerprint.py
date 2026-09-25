"""内容指纹与规范化哈希工具：所有链式结构共用同一套 canonical 形式。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def sha256_hex(data: bytes) -> str:
    """返回字节内容的 SHA-256 十六进制指纹。"""
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """键排序、无空白、UTF-8 的规范化 JSON，保证同构对象哈希一致。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def hash_json(obj: Any) -> str:
    """对任意可 JSON 序列化对象给出稳定指纹。"""
    return sha256_hex(canonical_json(obj))


def iso_utc(value: datetime) -> str:
    """把 aware 时间格式化为稳定的 UTC 字符串，供链式哈希离线复算。"""
    if value.tzinfo is None:
        raise ValueError("时间戳必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
