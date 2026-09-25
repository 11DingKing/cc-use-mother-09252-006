"""评审包：组装、封存（清单指纹）、失效恢复与复审新轮次。

封存是原子状态迁移（CAS）：并发封存只有一个胜者；复审开启新 cycle 依赖
（机构, 学期, cycle）唯一约束，并发复审只会产生一个新包。
"""
from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import record_event
from ..errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from ..fingerprint import hash_json, iso_utc
from ..models import (EvidenceItem, EvidenceVersion, Objection, ObjectionStatus,
                      Package, PackageItem, PackageStatus, ReReviewRequest,
                      ReReviewStatus, Role, User, VersionStatus)
from ..ports import Clock, IdGenerator
from .directory import require_officer


def get_package(session: Session, package_id: str) -> Package:
    package = session.get(Package, package_id)
    if package is None:
        raise NotFoundError("评审包不存在")
    return package


def build_manifest(package: Package, items: list[PackageItem]) -> dict:
    """评审包清单的规范化形式：封存与离线核验共用，任何字段变化都会改变指纹。"""
    entries = sorted(
        ({"item_id": it.item_id, "version_id": it.evidence_version_id,
          "sha256": it.sha256, "kind": it.kind, "sensitivity": it.sensitivity}
         for it in items),
        key=lambda e: (e["item_id"], e["version_id"]),
    )
    return {
        "package_id": package.id,
        "institution_id": package.institution_id,
        "term": package.term,
        "cycle": package.cycle,
        "deadline_at": iso_utc(package.deadline_at),
        "items": entries,
    }


def create_package(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                   institution_id: str, term: str, deadline_at) -> tuple[Package, bool]:
    """开启新一轮评审包（通常 cycle=1）；并发创建以唯一约束裁决。"""
    require_officer(actor)
    term = term.strip()
    if not term:
        raise ValidationError("term 不能为空")
    cycle = (session.scalar(
        select(func.max(Package.cycle)).where(Package.institution_id == institution_id,
                                              Package.term == term)) or 0) + 1
    package = Package(id=ids.new_id(), institution_id=institution_id, term=term,
                      cycle=cycle, status=PackageStatus.DRAFT.value,
                      deadline_at=deadline_at, created_by=actor.id,
                      created_at=clock.now())
    session.add(package)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发创建：返回先提交者
        existing = session.scalars(
            select(Package).where(Package.institution_id == institution_id,
                                  Package.term == term, Package.cycle == cycle)).one()
        return existing, False
    record_event(session, clock, actor_id=actor.id, action="package.created",
                 entity_type="package", entity_id=package.id,
                 payload={"institution_id": institution_id, "term": term, "cycle": cycle,
                          "deadline_at": iso_utc(deadline_at)})
    return package, True


def _require_package_editor(actor: User, package: Package) -> None:
    if actor.role == Role.QUALITY_OFFICER.value:
        return
    if (actor.institution_id == package.institution_id
            and actor.role in (Role.INSTITUTION_ADMIN.value, Role.COORDINATOR.value)):
        return
    raise PermissionDeniedError("无权编辑该评审包")


def add_item(session: Session, clock: Clock, actor: User, *, package_id: str,
             evidence_version_id: str) -> PackageItem:
    """把证据版本钉入草稿包；封存后一律拒绝（后补文件只能走复审）。"""
    package = get_package(session, package_id)
    _require_package_editor(actor, package)
    if package.status != PackageStatus.DRAFT.value:
        raise ConflictError("评审包已封存，材料集合不可更改；后补文件请走复审流程")
    version = session.get(EvidenceVersion, evidence_version_id)
    if version is None:
        raise NotFoundError("证据版本不存在")
    item = session.get(EvidenceItem, version.item_id)
    if item.institution_id != package.institution_id or item.term != package.term:
        raise ValidationError("材料不属于该评审包的机构/学期")
    if version.status != VersionStatus.ACTIVE.value:
        raise ValidationError("材料已撤回，不能进入评审包")
    if version.submitted_at > package.deadline_at:
        raise ValidationError("材料迟于截止时间提交，不能进入本轮评审包")

    existing = session.get(PackageItem, (package.id, version.id))
    if existing is not None:
        return existing  # 幂等：重复钉入无效果
    pin = PackageItem(package_id=package.id, evidence_version_id=version.id,
                      item_id=item.id, sha256=version.sha256, kind=item.kind,
                      sensitivity=item.sensitivity, pinned_at=clock.now())
    session.add(pin)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        return session.get(PackageItem, (package.id, version.id))
    record_event(session, clock, actor_id=actor.id, action="package.item_pinned",
                 entity_type="package", entity_id=package.id,
                 payload={"evidence_version_id": version.id, "item_id": item.id,
                          "sha256": version.sha256})
    return pin


def seal_package(session: Session, clock: Clock, actor: User, *, package_id: str) -> Package:
    """封存草稿包：校验材料可用后计算清单指纹并原子迁移状态（幂等）。"""
    require_officer(actor)
    package = get_package(session, package_id)
    if package.status in (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value,
                          PackageStatus.DECIDED.value):
        return package  # 幂等：已封存直接返回
    if package.status == PackageStatus.INVALIDATED.value:
        raise ConflictError("评审包已失效，请通过复审开启新一轮")

    items = session.scalars(
        select(PackageItem).where(PackageItem.package_id == package.id)).all()
    if not items:
        raise ValidationError("评审包为空，不能封存")
    problems = []
    for pin in items:
        version = session.get(EvidenceVersion, pin.evidence_version_id)
        if version.status != VersionStatus.ACTIVE.value:
            problems.append(f"材料 {pin.item_id} 已撤回")
        elif version.submitted_at > package.deadline_at:
            problems.append(f"材料 {pin.item_id} 迟于截止时间")
    if problems:
        raise ConflictError("评审包含不可用材料，无法封存", details={"problems": problems})

    manifest_hash = hash_json(build_manifest(package, items))
    now = clock.now()
    result = session.execute(
        update(Package)
        .where(Package.id == package.id, Package.status == PackageStatus.DRAFT.value)
        .values(status=PackageStatus.SEALED.value, sealed_at=now,
                manifest_hash=manifest_hash)
        .execution_options(synchronize_session=False))
    if result.rowcount == 0:  # 并发封存：他人已完成
        session.expire(package)
        refreshed = session.get(Package, package.id)
        if refreshed.status in (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value,
                                PackageStatus.DECIDED.value):
            return refreshed
        raise ConflictError("评审包状态已变化，无法封存")
    package.status = PackageStatus.SEALED.value
    package.sealed_at = now
    package.manifest_hash = manifest_hash
    record_event(session, clock, actor_id=actor.id, action="package.sealed",
                 entity_type="package", entity_id=package.id,
                 payload={"manifest_hash": manifest_hash, "item_count": len(items),
                          "cycle": package.cycle})
    return package


def open_rereview(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                  package_id: str, new_deadline_at) -> tuple[Package, bool]:
    """基于待处理复审请求（或已失效包）开启新一轮评审包。

    并发安全：cycle 取当前最大值+1，(机构, 学期, cycle) 唯一约束保证
    并发开启只有一个胜者，其余调用拿到同一个新包（幂等）。
    """
    require_officer(actor)
    old = get_package(session, package_id)
    if old.status not in (PackageStatus.DECIDED.value, PackageStatus.INVALIDATED.value):
        raise ConflictError("仅已签发结论或已失效的评审包可发起复审")
    # 幂等：该包已开启过后续轮次 → 直接返回既有后继（并发复审收敛于此）
    successor = session.scalars(
        select(Package).where(Package.supersedes_package_id == old.id)
        .order_by(Package.cycle.desc()).limit(1)).first()
    if successor is not None:
        return successor, False
    open_requests = session.scalars(
        select(ReReviewRequest).where(ReReviewRequest.package_id == old.id,
                                      ReReviewRequest.status == ReReviewStatus.OPEN.value)).all()
    upheld = session.scalars(
        select(Objection).where(Objection.package_id == old.id,
                                Objection.status == ObjectionStatus.UPHELD.value)).all()
    if old.status == PackageStatus.DECIDED.value and not open_requests and not upheld:
        raise ConflictError("没有触发复审的待处理事项")

    cycle = (session.scalar(
        select(func.max(Package.cycle)).where(Package.institution_id == old.institution_id,
                                              Package.term == old.term)) or 0) + 1
    now = clock.now()
    new_package = Package(id=ids.new_id(), institution_id=old.institution_id,
                          term=old.term, cycle=cycle, status=PackageStatus.DRAFT.value,
                          deadline_at=new_deadline_at, created_by=actor.id,
                          created_at=now, supersedes_package_id=old.id)
    session.add(new_package)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发复审：返回已创建的新一轮包
        existing = session.scalars(
            select(Package).where(Package.institution_id == old.institution_id,
                                  Package.term == old.term, Package.cycle == cycle)).one()
        return existing, False

    # 预置材料：旧包涉及的每个材料取其当前有效头版本（已撤回的自动排除）
    old_pins = session.scalars(
        select(PackageItem).where(PackageItem.package_id == old.id)).all()
    item_ids = {pin.item_id for pin in old_pins}
    for req in open_requests:
        version = session.get(EvidenceVersion, req.evidence_version_id)
        item_ids.add(version.item_id)
    pinned = []
    for item_id in sorted(item_ids):
        head = session.scalars(
            select(EvidenceVersion)
            .where(EvidenceVersion.item_id == item_id,
                   EvidenceVersion.status == VersionStatus.ACTIVE.value)
            .order_by(EvidenceVersion.seq.desc()).limit(1)).first()
        if head is None or head.submitted_at > new_deadline_at:
            continue
        item = session.get(EvidenceItem, item_id)
        session.add(PackageItem(package_id=new_package.id, evidence_version_id=head.id,
                                item_id=item_id, sha256=head.sha256, kind=item.kind,
                                sensitivity=item.sensitivity, pinned_at=now))
        pinned.append(head.id)
    for req in open_requests:
        req.status = ReReviewStatus.ABSORBED.value
        req.absorbed_by_package_id = new_package.id
    record_event(session, clock, actor_id=actor.id, action="package.rereview_opened",
                 entity_type="package", entity_id=new_package.id,
                 payload={"old_package_id": old.id, "cycle": cycle,
                          "absorbed_requests": [r.id for r in open_requests],
                          "pinned_versions": pinned})
    return new_package, True
