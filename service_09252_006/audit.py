"""追加式审计日志：每个事件携带前序哈希，构成可离线复算的哈希链。"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .fingerprint import hash_json, iso_utc
from .models import AuditEvent, AuditHead
from .ports import Clock

GENESIS_HASH = "0" * 64


def event_body(seq: int, at, actor_id: str, action: str, entity_type: str,
               entity_id: str, payload: dict[str, Any], prev_hash: str) -> dict[str, Any]:
    """事件的规范化载荷，写入与核验共用同一构造函数。"""
    return {
        "seq": seq,
        "at": iso_utc(at),
        "actor_id": actor_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "payload": payload,
        "prev_hash": prev_hash,
    }


def record_event(session: Session, clock: Clock, *, actor_id: str, action: str,
                 entity_type: str, entity_id: str, payload: dict[str, Any]) -> AuditEvent:
    """在同一事务内追加一个审计事件，并推进链头锚点（随业务变更一起提交或回滚）。"""
    head = session.get(AuditHead, 1)
    if head is None:  # 首事件（或兼容无链头的既有库）
        last = session.scalars(
            select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).first()
        seq = (last.seq + 1) if last else 1
        prev_hash = last.event_hash if last else GENESIS_HASH
        head = AuditHead(id=1, last_seq=seq, last_hash="")
        session.add(head)
    else:
        seq = head.last_seq + 1
        prev_hash = head.last_hash
    at = clock.now()
    body = event_body(seq, at, actor_id, action, entity_type, entity_id, payload, prev_hash)
    event_hash = hash_json(body)
    event = AuditEvent(
        at=at,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload_json=json.dumps(payload, sort_keys=True, ensure_ascii=False),
        prev_hash=prev_hash,
        event_hash=event_hash,
    )
    session.add(event)
    head.last_seq = seq
    head.last_hash = event_hash
    return event
