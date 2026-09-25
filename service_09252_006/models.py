"""持久化模型：证据版本链、评审包、评审、异议、复审请求与审计事件。

设计要点：
- 所有时间戳经 UtcDateTime 以 UTC 存储、以 aware UTC 读出，跨时区比较不发生歧义；
- 证据内容随版本行保存（内容寻址），离线核验不需要外部文件；
- 评审包封存后只读，结论通过 manifest_hash 固定到一组明确材料。
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, LargeBinary,
                        String, Text, TypeDecorator, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator):
    """以 UTC 存储、读出时恢复 tz-aware 的时间戳类型；拒绝朴素时间。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("时间戳必须带时区")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class Role(str, enum.Enum):
    QUALITY_OFFICER = "quality_officer"      # 质量官（全局）
    INSTITUTION_ADMIN = "institution_admin"  # 机构管理员
    COORDINATOR = "coordinator"              # 机构协调员（提交材料）
    REVIEWER = "reviewer"                    # 评审员（按包指派）


class Sensitivity(str, enum.Enum):
    PUBLIC = "public"            # 机构内可见
    INSTITUTION = "institution"  # 机构内可见
    RESTRICTED = "restricted"    # 敏感（如企业反馈）：最小披露


class EvidenceKind(str, enum.Enum):
    SYLLABUS = "syllabus"
    FACULTY = "faculty"
    ASSESSMENT = "assessment"
    ENTERPRISE_FEEDBACK = "enterprise_feedback"
    OTHER = "other"


class VersionStatus(str, enum.Enum):
    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class PackageStatus(str, enum.Enum):
    DRAFT = "draft"
    SEALED = "sealed"
    IN_REVIEW = "in_review"
    DECIDED = "decided"
    INVALIDATED = "invalidated"


class AssignmentStatus(str, enum.Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Recommendation(str, enum.Enum):
    APPROVE = "approve"
    CONDITIONAL = "conditional"
    REJECT = "reject"


class DecisionOutcome(str, enum.Enum):
    APPROVED = "approved"
    CONDITIONAL = "conditional"
    REJECTED = "rejected"


class ObjectionStatus(str, enum.Enum):
    OPEN = "open"
    UPHELD = "upheld"
    DISMISSED = "dismissed"


class ReReviewStatus(str, enum.Enum):
    OPEN = "open"
    ABSORBED = "absorbed"
    DISMISSED = "dismissed"


class Base(DeclarativeBase):
    pass


class Institution(Base):
    __tablename__ = "institutions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))  # IANA 时区，用于截止时间的本地展示
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32))
    institution_id: Mapped[str | None] = mapped_column(ForeignKey("institutions.id"), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class EvidenceItem(Base):
    """逻辑材料（同一机构+学期+类型+标题唯一），其下挂版本链。"""

    __tablename__ = "evidence_items"
    __table_args__ = (
        UniqueConstraint("institution_id", "term", "kind", "title", name="uq_item_natural"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey("institutions.id"))
    term: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    sensitivity: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class EvidenceVersion(Base):
    """内容寻址的版本：sha256 即指纹，supersedes_id 串起版本关系。"""

    __tablename__ = "evidence_versions"
    __table_args__ = (
        UniqueConstraint("item_id", "sha256", name="uq_version_content"),
        UniqueConstraint("item_id", "seq", name="uq_version_seq"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("evidence_items.id"))
    seq: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("evidence_versions.id"), nullable=True)
    submitted_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(16), default=VersionStatus.ACTIVE.value)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    withdrawn_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    withdrawn_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    withdrawal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class Package(Base):
    """评审包：一个机构一个学期一个轮次；封存后材料集合与清单指纹不可变。"""

    __tablename__ = "packages"
    __table_args__ = (
        UniqueConstraint("institution_id", "term", "cycle", name="uq_package_cycle"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    institution_id: Mapped[str] = mapped_column(ForeignKey("institutions.id"))
    term: Mapped[str] = mapped_column(String(32))
    cycle: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=PackageStatus.DRAFT.value)
    deadline_at: Mapped[datetime] = mapped_column(UtcDateTime)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    sealed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supersedes_package_id: Mapped[str | None] = mapped_column(ForeignKey("packages.id"), nullable=True)
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PackageItem(Base):
    """封存时钉住的材料条目：版本与指纹随包固定。"""

    __tablename__ = "package_items"

    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), primary_key=True)
    evidence_version_id: Mapped[str] = mapped_column(ForeignKey("evidence_versions.id"), primary_key=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("evidence_items.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    sensitivity: Mapped[str] = mapped_column(String(32))
    pinned_at: Mapped[datetime] = mapped_column(UtcDateTime)


class ReviewAssignment(Base):
    __tablename__ = "review_assignments"
    __table_args__ = (
        UniqueConstraint("package_id", "reviewer_id", name="uq_assignment"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"))
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(16), default=AssignmentStatus.ACTIVE.value)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("package_id", "reviewer_id", name="uq_review"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"))
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    recommendation: Mapped[str] = mapped_column(String(16))
    comments: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(UtcDateTime)


class Decision(Base):
    """评审结论：通过 manifest_hash 固定到一组明确材料，decision_hash 防篡改。"""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"), unique=True)
    outcome: Mapped[str] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(Text, default="")
    issued_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    decision_hash: Mapped[str] = mapped_column(String(64))


class Objection(Base):
    """异议：机构或质量官对评审过程/结论提出，未处理时阻止签发。"""

    __tablename__ = "objections"
    __table_args__ = (
        UniqueConstraint("package_id", "raised_by", "reason_hash", name="uq_objection"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"))
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    raised_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    reason_hash: Mapped[str] = mapped_column(String(64))
    raised_at: Mapped[datetime] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(16), default=ObjectionStatus.OPEN.value)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class ReReviewRequest(Base):
    """复审请求：后补文件或材料撤回触发；开启新一轮评审包时被吸收。"""

    __tablename__ = "rereview_requests"
    __table_args__ = (
        UniqueConstraint("package_id", "evidence_version_id", name="uq_rereview_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id"))
    evidence_version_id: Mapped[str] = mapped_column(ForeignKey("evidence_versions.id"))
    reason: Mapped[str] = mapped_column(String(32))  # late_submission / withdrawal
    status: Mapped[str] = mapped_column(String(16), default=ReReviewStatus.OPEN.value)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    absorbed_by_package_id: Mapped[str | None] = mapped_column(ForeignKey("packages.id"), nullable=True)


class AuditEvent(Base):
    """追加式审计日志：事件以 prev_hash/event_hash 串成哈希链，可离线复算。"""

    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime)
    actor_id: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))


class AuditHead(Base):
    """审计链头锚点（单行）：使尾部删除也可被离线核验发现。"""

    __tablename__ = "audit_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    last_seq: Mapped[int] = mapped_column(Integer)
    last_hash: Mapped[str] = mapped_column(String(64))


class IdempotencyRecord(Base):
    """幂等键：同一操作者同一键重放返回首个响应，不同请求体则冲突。"""

    __tablename__ = "idempotency_records"

    actor_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
