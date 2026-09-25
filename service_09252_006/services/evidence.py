"""证据接收：内容寻址、版本链、迟到判定、撤回与复审请求触发。

- 同一材料重复提交相同内容 → 返回既有版本（天然幂等）；
- 版本通过 supersedes_id 串成链，任何机构都无法“覆盖”历史，只能追加新版本；
- 评审包封存后到达的版本（后补文件）只生成复审请求，不改动已封存材料；
- 撤回保留内容与历史（已签发结论仍可核验），但阻断其进入新评审包。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import record_event
from ..errors import (ConflictError, NotFoundError, PermissionDeniedError,
                      ValidationError)
from ..fingerprint import sha256_hex
from ..models import (EvidenceItem, EvidenceKind, EvidenceVersion, Package,
                      PackageItem, PackageStatus, ReReviewRequest, ReReviewStatus,
                      Role, Sensitivity, User, VersionStatus)
from ..ports import Clock, IdGenerator

_DECIDED_OR_ACTIVE = (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value,
                      PackageStatus.DECIDED.value)


@dataclass
class IngestResult:
    version: EvidenceVersion
    created: bool
    late: bool
    rereview_request_id: str | None


def _require_institution_writer(actor: User, institution_id: str) -> None:
    if actor.role == Role.QUALITY_OFFICER.value:
        return
    if (actor.institution_id == institution_id
            and actor.role in (Role.INSTITUTION_ADMIN.value, Role.COORDINATOR.value)):
        return
    raise PermissionDeniedError("无权为该机构提交证据")


def _enum_value(enum_cls, value: str, field: str) -> str:
    try:
        return enum_cls(value).value
    except ValueError:
        raise ValidationError(f"{field} 非法: {value}",
                              details={"allowed": [e.value for e in enum_cls]}) from None


def create_item(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                institution_id: str, term: str, kind: str, title: str,
                sensitivity: str) -> tuple[EvidenceItem, bool]:
    """登记逻辑材料；自然键（机构+学期+类型+标题）去重，重复创建幂等。"""
    _require_institution_writer(actor, institution_id)
    kind_v = _enum_value(EvidenceKind, kind, "kind")
    sensitivity_v = _enum_value(Sensitivity, sensitivity, "sensitivity")
    term = term.strip()
    title = title.strip()
    if not term or not title:
        raise ValidationError("term 与 title 不能为空")

    def _find() -> EvidenceItem | None:
        return session.scalars(
            select(EvidenceItem).where(
                EvidenceItem.institution_id == institution_id,
                EvidenceItem.term == term,
                EvidenceItem.kind == kind_v,
                EvidenceItem.title == title)).first()

    existing = _find()
    if existing is not None:
        return existing, False
    item = EvidenceItem(id=ids.new_id(), institution_id=institution_id, term=term,
                        kind=kind_v, title=title, sensitivity=sensitivity_v,
                        created_by=actor.id, created_at=clock.now())
    session.add(item)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发登记同一自然键：以先提交者为准
        return _find(), False
    record_event(session, clock, actor_id=actor.id, action="evidence_item.created",
                 entity_type="evidence_item", entity_id=item.id,
                 payload={"institution_id": institution_id, "term": term, "kind": kind_v,
                          "title": title, "sensitivity": sensitivity_v})
    return item, True


def _latest_package(session: Session, institution_id: str, term: str) -> Package | None:
    return session.scalars(
        select(Package)
        .where(Package.institution_id == institution_id, Package.term == term)
        .order_by(Package.cycle.desc()).limit(1)).first()


def ensure_rereview_request(session: Session, clock: Clock, ids: IdGenerator,
                            actor: User, *, package: Package,
                            version: EvidenceVersion, reason: str) -> str:
    """为（包, 版本）建立复审请求；唯一约束去重，并发下返回既有请求。"""
    existing = session.scalars(
        select(ReReviewRequest).where(
            ReReviewRequest.package_id == package.id,
            ReReviewRequest.evidence_version_id == version.id)).first()
    if existing is not None:
        return existing.id
    req = ReReviewRequest(id=ids.new_id(), package_id=package.id,
                          evidence_version_id=version.id, reason=reason,
                          status=ReReviewStatus.OPEN.value, created_by=actor.id,
                          created_at=clock.now())
    session.add(req)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        existing = session.scalars(
            select(ReReviewRequest).where(
                ReReviewRequest.package_id == package.id,
                ReReviewRequest.evidence_version_id == version.id)).one()
        return existing.id
    record_event(session, clock, actor_id=actor.id, action="rereview.requested",
                 entity_type="rereview_request", entity_id=req.id,
                 payload={"package_id": package.id, "evidence_version_id": version.id,
                          "reason": reason})
    return req.id


def ingest_version(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                   item_id: str, content: bytes) -> IngestResult:
    """接收一份证据内容：计算指纹、追加版本、判定迟到并触发复审请求。"""
    item = session.get(EvidenceItem, item_id)
    if item is None:
        raise NotFoundError("证据材料不存在")
    _require_institution_writer(actor, item.institution_id)
    if not content:
        raise ValidationError("证据内容为空")

    digest = sha256_hex(content)
    existing = session.scalars(
        select(EvidenceVersion).where(EvidenceVersion.item_id == item.id,
                                      EvidenceVersion.sha256 == digest)).first()
    if existing is not None:
        return IngestResult(version=existing, created=False, late=existing.is_late,
                            rereview_request_id=None)

    head = session.scalars(
        select(EvidenceVersion).where(EvidenceVersion.item_id == item.id)
        .order_by(EvidenceVersion.seq.desc()).limit(1)).first()
    now = clock.now()
    package = _latest_package(session, item.institution_id, item.term)
    late = False
    if package is not None:
        # 已封存（或更后状态）→ 任何新版本都是后补；草稿期则与截止时间比较
        late = package.status != PackageStatus.DRAFT.value or now > package.deadline_at

    version = EvidenceVersion(
        id=ids.new_id(), item_id=item.id, seq=(head.seq + 1) if head else 1,
        sha256=digest, byte_size=len(content), content=content,
        supersedes_id=head.id if head else None, submitted_by=actor.id,
        submitted_at=now, status=VersionStatus.ACTIVE.value, is_late=late)
    session.add(version)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发提交相同内容：以先提交者为准
        existing = session.scalars(
            select(EvidenceVersion).where(EvidenceVersion.item_id == item.id,
                                          EvidenceVersion.sha256 == digest)).one()
        return IngestResult(version=existing, created=False, late=existing.is_late,
                            rereview_request_id=None)

    request_id = None
    if package is not None and package.status in _DECIDED_OR_ACTIVE:
        request_id = ensure_rereview_request(session, clock, ids, actor,
                                             package=package, version=version,
                                             reason="late_submission")
    record_event(session, clock, actor_id=actor.id, action="evidence_version.ingested",
                 entity_type="evidence_version", entity_id=version.id,
                 payload={"item_id": item.id, "seq": version.seq, "sha256": digest,
                          "supersedes_id": version.supersedes_id, "late": late})
    return IngestResult(version=version, created=True, late=late,
                        rereview_request_id=request_id)


def withdraw_version(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                     version_id: str, reason: str) -> EvidenceVersion:
    """撤回材料版本：历史保留，但阻断其进入新评审包。

    - 草稿包：直接移除该条目；
    - 已封存/评审中的包：整体失效（材料集合不再完整可信）；
    - 已签发结论的包：结论保持不动（历史不可改写），自动生成复审请求。
    """
    version = session.get(EvidenceVersion, version_id)
    if version is None:
        raise NotFoundError("证据版本不存在")
    item = session.get(EvidenceItem, version.item_id)
    if not (actor.role == Role.QUALITY_OFFICER.value
            or (actor.role == Role.INSTITUTION_ADMIN.value
                and actor.institution_id == item.institution_id)):
        raise PermissionDeniedError("仅质量官或本机构管理员可撤回材料")
    if version.status == VersionStatus.WITHDRAWN.value:
        return version  # 幂等：重复撤回不产生新效果
    if not reason.strip():
        raise ValidationError("撤回必须填写原因")

    now = clock.now()
    version.status = VersionStatus.WITHDRAWN.value
    version.withdrawn_at = now
    version.withdrawn_by = actor.id
    version.withdrawal_reason = reason

    pins = session.scalars(
        select(PackageItem).where(PackageItem.evidence_version_id == version.id)).all()
    for pin in pins:
        package = session.get(Package, pin.package_id)
        if package.status == PackageStatus.DRAFT.value:
            session.execute(
                delete(PackageItem).where(PackageItem.package_id == package.id,
                                          PackageItem.evidence_version_id == version.id))
            record_event(session, clock, actor_id=actor.id, action="package.item_removed",
                         entity_type="package", entity_id=package.id,
                         payload={"evidence_version_id": version.id, "cause": "withdrawal"})
        elif package.status in (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value):
            package.status = PackageStatus.INVALIDATED.value
            package.invalidated_reason = f"材料撤回: {version.id}"
            record_event(session, clock, actor_id=actor.id, action="package.invalidated",
                         entity_type="package", entity_id=package.id,
                         payload={"withdrawn_version_id": version.id})
        elif package.status == PackageStatus.DECIDED.value:
            ensure_rereview_request(session, clock, ids, actor, package=package,
                                    version=version, reason="withdrawal")
    record_event(session, clock, actor_id=actor.id, action="evidence_version.withdrawn",
                 entity_type="evidence_version", entity_id=version.id,
                 payload={"item_id": item.id, "sha256": version.sha256, "reason": reason})
    return version
