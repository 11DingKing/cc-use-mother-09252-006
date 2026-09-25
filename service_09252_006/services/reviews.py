"""评审执行：指派、评审提交、异议记录与结论签发。

结论通过 manifest_hash 固定到一组明确材料；签发后包进入 decided，
评审员的内容访问窗口随之关闭（最小披露的时间边界）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import record_event
from ..errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from ..fingerprint import hash_json, iso_utc, sha256_hex
from ..models import (AssignmentStatus, Decision, DecisionOutcome, Objection,
                      ObjectionStatus, Package, PackageStatus, Recommendation,
                      Review, ReviewAssignment, Role, User)
from ..ports import Clock, IdGenerator
from .directory import require_officer
from .packages import get_package


def assign_reviewer(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                    package_id: str, reviewer_id: str) -> ReviewAssignment:
    """指派评审员；首次指派把包推进到 in_review。重复指派幂等。"""
    require_officer(actor)
    package = get_package(session, package_id)
    if package.status not in (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value):
        raise ConflictError("仅已封存且未签发结论的评审包可指派评审")
    reviewer = session.get(User, reviewer_id)
    if reviewer is None or reviewer.role != Role.REVIEWER.value or not reviewer.active:
        raise ValidationError("指派对象不是有效评审员")

    existing = session.scalars(
        select(ReviewAssignment).where(ReviewAssignment.package_id == package.id,
                                       ReviewAssignment.reviewer_id == reviewer_id)).first()
    if existing is not None:
        if existing.status == AssignmentStatus.ACTIVE.value:
            return existing  # 幂等
        existing.status = AssignmentStatus.ACTIVE.value  # 重新指派：复用原记录
        existing.revoked_at = None
        existing.assigned_at = clock.now()
        record_event(session, clock, actor_id=actor.id, action="assignment.restored",
                     entity_type="review_assignment", entity_id=existing.id,
                     payload={"package_id": package.id, "reviewer_id": reviewer_id})
        return existing

    assignment = ReviewAssignment(id=ids.new_id(), package_id=package.id,
                                  reviewer_id=reviewer_id, assigned_by=actor.id,
                                  assigned_at=clock.now(),
                                  status=AssignmentStatus.ACTIVE.value)
    session.add(assignment)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发指派同一人：以先提交者为准
        return session.scalars(
            select(ReviewAssignment).where(
                ReviewAssignment.package_id == package.id,
                ReviewAssignment.reviewer_id == reviewer_id)).one()
    if package.status == PackageStatus.SEALED.value:
        package.status = PackageStatus.IN_REVIEW.value
    record_event(session, clock, actor_id=actor.id, action="assignment.created",
                 entity_type="review_assignment", entity_id=assignment.id,
                 payload={"package_id": package.id, "reviewer_id": reviewer_id})
    return assignment


def revoke_assignment(session: Session, clock: Clock, actor: User, *, package_id: str,
                      reviewer_id: str) -> ReviewAssignment:
    """撤销指派：评审员的内容访问立即关闭。重复撤销幂等。"""
    require_officer(actor)
    package = get_package(session, package_id)
    if package.status not in (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value):
        raise ConflictError("仅评审进行中可撤销指派")
    assignment = session.scalars(
        select(ReviewAssignment).where(ReviewAssignment.package_id == package.id,
                                       ReviewAssignment.reviewer_id == reviewer_id)).first()
    if assignment is None:
        raise NotFoundError("指派不存在")
    if assignment.status == AssignmentStatus.REVOKED.value:
        return assignment
    assignment.status = AssignmentStatus.REVOKED.value
    assignment.revoked_at = clock.now()
    record_event(session, clock, actor_id=actor.id, action="assignment.revoked",
                 entity_type="review_assignment", entity_id=assignment.id,
                 payload={"package_id": package.id, "reviewer_id": reviewer_id})
    return assignment


def _review_hash(package_id: str, reviewer_id: str, recommendation: str,
                 comments: str) -> str:
    return hash_json({"package_id": package_id, "reviewer_id": reviewer_id,
                      "recommendation": recommendation, "comments": comments})


def submit_review(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                  package_id: str, recommendation: str, comments: str) -> Review:
    """提交评审意见；结论签发前同一评审员可修订，重放相同内容幂等。"""
    if actor.role != Role.REVIEWER.value:
        raise PermissionDeniedError("仅评审员可提交评审意见")
    package = get_package(session, package_id)
    assignment = session.scalars(
        select(ReviewAssignment).where(
            ReviewAssignment.package_id == package.id,
            ReviewAssignment.reviewer_id == actor.id,
            ReviewAssignment.status == AssignmentStatus.ACTIVE.value)).first()
    if assignment is None:
        raise PermissionDeniedError("未被指派评审该包")
    if package.status != PackageStatus.IN_REVIEW.value:
        raise ConflictError("评审未开放或已结束")
    try:
        recommendation_v = Recommendation(recommendation).value
    except ValueError:
        raise ValidationError(f"非法评审结论: {recommendation}") from None

    content_hash = _review_hash(package.id, actor.id, recommendation_v, comments)
    existing = session.scalars(
        select(Review).where(Review.package_id == package.id,
                             Review.reviewer_id == actor.id)).first()
    if existing is not None:
        if existing.content_hash == content_hash:
            return existing  # 幂等重放
        existing.recommendation = recommendation_v
        existing.comments = comments
        existing.content_hash = content_hash
        existing.submitted_at = clock.now()
        record_event(session, clock, actor_id=actor.id, action="review.updated",
                     entity_type="review", entity_id=existing.id,
                     payload={"package_id": package.id, "content_hash": content_hash})
        return existing

    review = Review(id=ids.new_id(), package_id=package.id, reviewer_id=actor.id,
                    recommendation=recommendation_v, comments=comments,
                    content_hash=content_hash, submitted_at=clock.now())
    session.add(review)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发重复提交：以先提交者为准
        return session.scalars(
            select(Review).where(Review.package_id == package.id,
                                 Review.reviewer_id == actor.id)).one()
    record_event(session, clock, actor_id=actor.id, action="review.submitted",
                 entity_type="review", entity_id=review.id,
                 payload={"package_id": package.id, "content_hash": content_hash})
    return review


def issue_decision(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                   package_id: str, outcome: str, rationale: str) -> Decision:
    """签发结论：固定到包清单指纹；全量评审完成且无未处理异议才可签发。"""
    require_officer(actor)
    package = get_package(session, package_id)
    try:
        outcome_v = DecisionOutcome(outcome).value
    except ValueError:
        raise ValidationError(f"非法结论: {outcome}") from None

    if package.status == PackageStatus.DECIDED.value:
        existing = session.scalars(
            select(Decision).where(Decision.package_id == package.id)).first()
        if existing is not None and existing.outcome == outcome_v \
                and existing.rationale == rationale:
            return existing  # 幂等重放
        raise ConflictError("结论已签发，不可更改；如需变更请走复审流程")
    if package.status != PackageStatus.IN_REVIEW.value:
        raise ConflictError("评审尚未完成，不能签发结论")

    assignments = session.scalars(
        select(ReviewAssignment).where(
            ReviewAssignment.package_id == package.id,
            ReviewAssignment.status == AssignmentStatus.ACTIVE.value)).all()
    if not assignments:
        raise ConflictError("尚未指派评审员")
    missing = [a.reviewer_id for a in assignments
               if session.scalars(select(Review).where(Review.package_id == package.id,
                                                       Review.reviewer_id == a.reviewer_id)).first() is None]
    if missing:
        raise ConflictError("存在未提交的评审意见", details={"pending_reviewers": missing})
    open_objections = session.scalars(
        select(Objection).where(Objection.package_id == package.id,
                                Objection.status == ObjectionStatus.OPEN.value)).all()
    if open_objections:
        raise ConflictError("存在未处理的异议",
                            details={"open_objections": [o.id for o in open_objections]})

    now = clock.now()
    decision_hash = hash_json({
        "package_id": package.id, "manifest_hash": package.manifest_hash,
        "outcome": outcome_v, "rationale": rationale, "issued_by": actor.id,
        "issued_at": iso_utc(now)})
    decision = Decision(id=ids.new_id(), package_id=package.id, outcome=outcome_v,
                        rationale=rationale, issued_by=actor.id, issued_at=now,
                        manifest_hash=package.manifest_hash,
                        decision_hash=decision_hash)
    session.add(decision)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发签发：唯一约束裁决
        existing = session.scalars(
            select(Decision).where(Decision.package_id == package.id)).one()
        if existing.outcome == outcome_v and existing.rationale == rationale:
            return existing
        raise ConflictError("结论已由他人签发") from None
    package.status = PackageStatus.DECIDED.value
    record_event(session, clock, actor_id=actor.id, action="decision.issued",
                 entity_type="decision", entity_id=decision.id,
                 payload={"package_id": package.id, "outcome": outcome_v,
                          "manifest_hash": package.manifest_hash,
                          "decision_hash": decision_hash})
    return decision


def raise_objection(session: Session, clock: Clock, ids: IdGenerator, actor: User, *,
                    package_id: str, reason: str) -> Objection:
    """记录异议；相同提出者+相同理由去重（幂等）。"""
    package = get_package(session, package_id)
    if not (actor.role == Role.QUALITY_OFFICER.value
            or (actor.institution_id == package.institution_id
                and actor.role in (Role.INSTITUTION_ADMIN.value, Role.COORDINATOR.value))):
        raise PermissionDeniedError("仅质量官或本机构成员可提出异议")
    if package.status not in (PackageStatus.SEALED.value, PackageStatus.IN_REVIEW.value,
                              PackageStatus.DECIDED.value):
        raise ConflictError("当前状态不可提出异议")
    if not reason.strip():
        raise ValidationError("异议理由不能为空")
    reason_hash = sha256_hex(reason.encode("utf-8"))
    existing = session.scalars(
        select(Objection).where(Objection.package_id == package.id,
                                Objection.raised_by == actor.id,
                                Objection.reason_hash == reason_hash)).first()
    if existing is not None:
        return existing
    decision = session.scalars(
        select(Decision).where(Decision.package_id == package.id)).first()
    objection = Objection(id=ids.new_id(), package_id=package.id,
                          decision_id=decision.id if decision else None,
                          raised_by=actor.id, reason=reason, reason_hash=reason_hash,
                          raised_at=clock.now(), status=ObjectionStatus.OPEN.value)
    session.add(objection)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        return session.scalars(
            select(Objection).where(Objection.package_id == package.id,
                                    Objection.raised_by == actor.id,
                                    Objection.reason_hash == reason_hash)).one()
    record_event(session, clock, actor_id=actor.id, action="objection.raised",
                 entity_type="objection", entity_id=objection.id,
                 payload={"package_id": package.id, "reason_hash": reason_hash})
    return objection


def resolve_objection(session: Session, clock: Clock, actor: User, *, objection_id: str,
                      outcome: str, note: str) -> Objection:
    """处理异议：upheld（成立，可触发复审）或 dismissed（驳回）。"""
    require_officer(actor)
    objection = session.get(Objection, objection_id)
    if objection is None:
        raise NotFoundError("异议不存在")
    if objection.status != ObjectionStatus.OPEN.value:
        raise ConflictError("异议已处理")
    try:
        outcome_v = ObjectionStatus(outcome).value
    except ValueError:
        raise ValidationError(f"非法处理结果: {outcome}") from None
    if outcome_v == ObjectionStatus.OPEN.value:
        raise ValidationError("处理结果必须是 upheld 或 dismissed")
    objection.status = outcome_v
    objection.resolution_note = note
    objection.resolved_by = actor.id
    objection.resolved_at = clock.now()
    record_event(session, clock, actor_id=actor.id, action="objection.resolved",
                 entity_type="objection", entity_id=objection.id,
                 payload={"package_id": objection.package_id, "outcome": outcome_v})
    return objection
