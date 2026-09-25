"""离线完整性核验：直接读取数据库，重算全部指纹与链式哈希，不依赖服务进程。

核验内容：
1. 每个证据版本的内容指纹与字节数；
2. 每条版本链的序号连续性与前驱关系；
3. 每个已封存评审包的清单指纹；
4. 每个结论对清单的固定关系与结论自身哈希；
5. 审计日志的哈希链。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import GENESIS_HASH, event_body
from .fingerprint import hash_json, iso_utc, sha256_hex
from .models import (AuditEvent, AuditHead, Decision, EvidenceVersion, Package,
                     PackageItem)
from .services.packages import build_manifest


@dataclass
class VerifyReport:
    checked: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "failures": self.failures}


def verify_session(session: Session) -> VerifyReport:
    report = VerifyReport(checked={"evidence_versions": 0, "version_chains": 0,
                                   "manifests": 0, "decisions": 0, "audit_events": 0})

    # 1. 内容指纹（含已撤回版本：历史必须保持可核验）
    versions = session.scalars(select(EvidenceVersion)).all()
    for v in versions:
        report.checked["evidence_versions"] += 1
        if sha256_hex(bytes(v.content)) != v.sha256:
            report.failures.append(f"证据版本 {v.id} 的内容指纹不匹配")
        if v.byte_size != len(v.content):
            report.failures.append(f"证据版本 {v.id} 的字节数被改动")

    # 2. 版本链：序号从 1 连续递增，supersedes 指向前一版本
    by_item: dict[str, list[EvidenceVersion]] = {}
    for v in versions:
        by_item.setdefault(v.item_id, []).append(v)
    for item_id, chain in by_item.items():
        report.checked["version_chains"] += 1
        chain.sort(key=lambda x: x.seq)
        for idx, v in enumerate(chain):
            if v.seq != idx + 1:
                report.failures.append(f"材料 {item_id} 的版本序号断裂于 {v.seq}")
            expected_prev = chain[idx - 1].id if idx else None
            if v.supersedes_id != expected_prev:
                report.failures.append(
                    f"材料 {item_id} 第 {v.seq} 版的前驱关系被篡改")

    # 3. 评审包清单指纹
    packages = session.scalars(
        select(Package).where(Package.manifest_hash.isnot(None))).all()
    for p in packages:
        report.checked["manifests"] += 1
        items = session.scalars(
            select(PackageItem).where(PackageItem.package_id == p.id)).all()
        if hash_json(build_manifest(p, items)) != p.manifest_hash:
            report.failures.append(f"评审包 {p.id} 的清单指纹不匹配")

    # 4. 结论固定关系与结论哈希
    decisions = session.scalars(select(Decision)).all()
    for d in decisions:
        report.checked["decisions"] += 1
        package = session.get(Package, d.package_id)
        if package is None or package.manifest_hash != d.manifest_hash:
            report.failures.append(f"结论 {d.id} 未固定到评审包 {d.package_id} 的清单")
        expected = hash_json({
            "package_id": d.package_id, "manifest_hash": d.manifest_hash,
            "outcome": d.outcome, "rationale": d.rationale, "issued_by": d.issued_by,
            "issued_at": iso_utc(d.issued_at)})
        if expected != d.decision_hash:
            report.failures.append(f"结论 {d.id} 的内容被篡改")

    # 5. 审计链：逐事件复算，并校验链头锚点（尾部删除也会暴露）
    events = session.scalars(select(AuditEvent).order_by(AuditEvent.seq)).all()
    prev = GENESIS_HASH
    for e in events:
        report.checked["audit_events"] += 1
        body = event_body(e.seq, e.at, e.actor_id, e.action, e.entity_type,
                          e.entity_id, json.loads(e.payload_json), e.prev_hash)
        if e.prev_hash != prev:
            report.failures.append(f"审计事件 {e.seq} 的前链断裂")
        if hash_json(body) != e.event_hash:
            report.failures.append(f"审计事件 {e.seq} 的内容被篡改")
        prev = e.event_hash
    head = session.get(AuditHead, 1)
    if events:
        if head is None:
            report.failures.append("审计链头锚点缺失")
        elif head.last_seq != events[-1].seq or head.last_hash != events[-1].event_hash:
            report.failures.append("审计链头与最新事件不一致（可能存在尾部删除）")
    elif head is not None:
        report.failures.append("审计事件为空但链头锚点存在")

    return report
