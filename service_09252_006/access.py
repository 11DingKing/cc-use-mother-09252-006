"""最小披露访问策略：按机构与角色决定元数据/内容可见性。

规则（每次请求实时计算，角色或指派变更立即生效）：
- 质量官：全部可见；
- 本机构成员：元数据可见；内容默认可见，但 restricted（敏感企业反馈）
  仅机构管理员与提交者本人可见；
- 评审员：仅在被有效指派、且评审包处于评审窗口（sealed/in_review）内，
  可查看该包钉住材料的内容；结论签发或指派撤销后内容访问即刻关闭；
- 其他机构：连元数据也不可见（接口按不存在处理）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (AssignmentStatus, EvidenceItem, EvidenceVersion, Package,
                     PackageItem, PackageStatus, ReviewAssignment, Role,
                     Sensitivity, User)

_REVIEW_OPEN_STATES = (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value)


def _reviewer_has_item(session: Session, reviewer_id: str, item_id: str) -> bool:
    stmt = (
        select(PackageItem.evidence_version_id)
        .join(Package, Package.id == PackageItem.package_id)
        .join(ReviewAssignment, ReviewAssignment.package_id == Package.id)
        .where(ReviewAssignment.reviewer_id == reviewer_id,
               ReviewAssignment.status == AssignmentStatus.ACTIVE.value,
               PackageItem.item_id == item_id)
        .limit(1)
    )
    return session.scalars(stmt).first() is not None


def _reviewer_open_pins(session: Session, reviewer_id: str) -> set[str]:
    """评审员当前可接触内容的证据版本集合：有效指派且评审仍在进行。"""
    stmt = (
        select(PackageItem.evidence_version_id)
        .join(Package, Package.id == PackageItem.package_id)
        .join(ReviewAssignment, ReviewAssignment.package_id == Package.id)
        .where(ReviewAssignment.reviewer_id == reviewer_id,
               ReviewAssignment.status == AssignmentStatus.ACTIVE.value,
               Package.status.in_(_REVIEW_OPEN_STATES))
    )
    return set(session.scalars(stmt))


def can_view_metadata(session: Session, user: User, item: EvidenceItem) -> bool:
    if user.role == Role.QUALITY_OFFICER.value:
        return True
    if user.institution_id is not None and user.institution_id == item.institution_id:
        return True
    if user.role == Role.REVIEWER.value:
        return _reviewer_has_item(session, user.id, item.id)
    return False


def can_view_content(session: Session, user: User, item: EvidenceItem,
                     version: EvidenceVersion) -> bool:
    if user.role == Role.QUALITY_OFFICER.value:
        return True
    if user.institution_id is not None and user.institution_id == item.institution_id:
        if item.sensitivity == Sensitivity.RESTRICTED.value:
            # 敏感企业反馈：机构内也仅管理员与提交者可见
            return (user.role == Role.INSTITUTION_ADMIN.value
                    or version.submitted_by == user.id)
        return True
    if user.role == Role.REVIEWER.value:
        return version.id in _reviewer_open_pins(session, user.id)
    return False
