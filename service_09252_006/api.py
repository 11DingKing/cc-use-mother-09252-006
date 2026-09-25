"""HTTP 接口边界：认证、幂等键、错误映射与路由。

认证采用可替换的 X-Actor-Id 头（服务端内部身份端口）；所有变更类端点
支持 Idempotency-Key：同键重放返回首个响应，同键不同请求体返回 409。
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .access import can_view_content, can_view_metadata
from .config import Settings, load_settings
from .db import Database, open_database
from .errors import (ConflictError, DomainError, NotFoundError,
                     PermissionDeniedError, UnauthenticatedError, ValidationError)
from .fingerprint import hash_json, iso_utc
from .models import (AssignmentStatus, Decision, EvidenceItem, EvidenceVersion,
                     IdempotencyRecord, Institution, Objection, Package,
                     PackageItem, ReReviewRequest, ReReviewStatus, Review,
                     ReviewAssignment, Role, User)
from .ports import Clock, IdGenerator, SystemClock, UuidGenerator
from .services import directory as directory_svc
from .services import evidence as evidence_svc
from .services import packages as package_svc
from .services import reviews as review_svc

MAX_CONTENT_BYTES = 20 * 1024 * 1024


# ---------------------------------------------------------------- 请求模型

class InstitutionCreate(BaseModel):
    code: str
    name: str
    timezone: str = "UTC"


class UserCreate(BaseModel):
    name: str
    role: str
    institution_id: str | None = None


class UserUpdate(BaseModel):
    role: str | None = None
    institution_id: str | None = None
    active: bool | None = None


class ItemCreate(BaseModel):
    institution_id: str
    term: str
    kind: str
    title: str
    sensitivity: str = "public"


class VersionCreate(BaseModel):
    content_base64: str


class WithdrawRequest(BaseModel):
    reason: str


class PackageCreate(BaseModel):
    institution_id: str
    term: str
    deadline_at: datetime


class PinRequest(BaseModel):
    evidence_version_id: str


class AssignmentCreate(BaseModel):
    reviewer_id: str


class ReviewSubmit(BaseModel):
    recommendation: str
    comments: str = ""


class DecisionIssue(BaseModel):
    outcome: str
    rationale: str = ""


class ObjectionRaise(BaseModel):
    reason: str


class ObjectionResolve(BaseModel):
    outcome: str
    note: str = ""


class ReReviewOpen(BaseModel):
    new_deadline_at: datetime


# ---------------------------------------------------------------- 序列化

def _iso(value: datetime | None) -> str | None:
    return iso_utc(value) if value is not None else None


def _institution_dict(inst: Institution) -> dict:
    return {"id": inst.id, "code": inst.code, "name": inst.name,
            "timezone": inst.timezone, "created_at": _iso(inst.created_at)}


def _user_dict(user: User) -> dict:
    return {"id": user.id, "name": user.name, "role": user.role,
            "institution_id": user.institution_id, "active": user.active,
            "created_at": _iso(user.created_at)}


def _item_dict(item: EvidenceItem) -> dict:
    return {"id": item.id, "institution_id": item.institution_id, "term": item.term,
            "kind": item.kind, "title": item.title, "sensitivity": item.sensitivity,
            "created_by": item.created_by, "created_at": _iso(item.created_at)}


def _version_dict(version: EvidenceVersion, *, content_accessible: bool) -> dict:
    return {"id": version.id, "item_id": version.item_id, "seq": version.seq,
            "sha256": version.sha256, "byte_size": version.byte_size,
            "supersedes_id": version.supersedes_id,
            "submitted_by": version.submitted_by,
            "submitted_at": _iso(version.submitted_at),
            "status": version.status, "is_late": version.is_late,
            "withdrawn_at": _iso(version.withdrawn_at),
            "withdrawal_reason": version.withdrawal_reason,
            "content_accessible": content_accessible}


def _decision_dict(decision: Decision) -> dict:
    return {"id": decision.id, "package_id": decision.package_id,
            "outcome": decision.outcome, "rationale": decision.rationale,
            "issued_by": decision.issued_by, "issued_at": _iso(decision.issued_at),
            "manifest_hash": decision.manifest_hash,
            "decision_hash": decision.decision_hash}


def _objection_dict(obj: Objection) -> dict:
    return {"id": obj.id, "package_id": obj.package_id, "raised_by": obj.raised_by,
            "reason": obj.reason, "raised_at": _iso(obj.raised_at),
            "status": obj.status, "resolution_note": obj.resolution_note,
            "resolved_by": obj.resolved_by, "resolved_at": _iso(obj.resolved_at)}


def _rerequest_dict(req: ReReviewRequest) -> dict:
    return {"id": req.id, "package_id": req.package_id,
            "evidence_version_id": req.evidence_version_id, "reason": req.reason,
            "status": req.status, "created_at": _iso(req.created_at),
            "absorbed_by_package_id": req.absorbed_by_package_id}


def _aware(value: datetime) -> datetime:
    """朴素时间按 UTC 解释；带偏移时间统一换算 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------- 应用工厂

def create_app(settings: Settings | None = None, *, clock: Clock | None = None,
               id_generator: IdGenerator | None = None) -> FastAPI:
    settings = settings or load_settings()
    database = open_database(settings.database_url)
    database.create_schema()

    app = FastAPI(title="国际课程质量证据链", version="1.0.0")
    app.state.database = database
    app.state.clock = clock or SystemClock()
    app.state.ids = id_generator or UuidGenerator()

    @app.exception_handler(DomainError)
    def _domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message,
                               "details": exc.details}})

    # ------------------------------------------------------------ 依赖

    def read_session():
        session = database.read_factory()
        try:
            yield session
        finally:
            session.close()

    def write_session():
        session = database.write_factory()
        try:
            yield session
        finally:
            session.close()

    def _load_actor(actor_id: str | None) -> User | None:
        if not actor_id:
            return None
        with database.read_factory() as session:
            user = session.get(User, actor_id)
        if user is None or not user.active:
            raise UnauthenticatedError("身份无效或已停用")
        return user

    def actor_dep(x_actor_id: str | None = Header(default=None)) -> User:
        user = _load_actor(x_actor_id)
        if user is None:
            raise UnauthenticatedError("缺少 X-Actor-Id 请求头")
        return user

    def optional_actor_dep(x_actor_id: str | None = Header(default=None)) -> User | None:
        return _load_actor(x_actor_id)

    def run_idempotent(session: Session, actor: User | None, endpoint: str,
                       key: str | None, payload: Any, handler):
        """执行写操作并按幂等键去重；handler 返回 (status, body)。"""
        actor_id = actor.id if actor else "anonymous"
        request_hash = hash_json(payload)
        if key:
            record = session.get(IdempotencyRecord, (actor_id, key))
            if record is not None:
                if record.request_hash != request_hash:
                    raise ConflictError("幂等键已被不同请求体使用")
                return record.response_status, json.loads(record.response_body)
        status, body = handler()
        if key:
            record = IdempotencyRecord(
                actor_id=actor_id, key=key, endpoint=endpoint,
                request_hash=request_hash, response_status=status,
                response_body=json.dumps(body, ensure_ascii=False),
                created_at=app.state.clock.now())
            session.add(record)
            try:
                with session.begin_nested():
                    session.flush()
            except IntegrityError:  # 并发同键：返回先提交者的响应
                record = session.get(IdempotencyRecord, (actor_id, key))
                return record.response_status, json.loads(record.response_body)
        return status, body

    # ------------------------------------------------------------ 包视图

    def can_view_package(session: Session, actor: User, package: Package) -> bool:
        if actor.role == Role.QUALITY_OFFICER.value:
            return True
        if actor.institution_id and actor.institution_id == package.institution_id:
            return True
        if actor.role == Role.REVIEWER.value:
            count = session.scalar(
                select(func.count(ReviewAssignment.id)).where(
                    ReviewAssignment.package_id == package.id,
                    ReviewAssignment.reviewer_id == actor.id,
                    ReviewAssignment.status == AssignmentStatus.ACTIVE.value))
            return bool(count)
        return False

    def package_dict(session: Session, package: Package, actor: User) -> dict:
        institution = session.get(Institution, package.institution_id)
        deadline_local = None
        if institution is not None:
            deadline_local = package.deadline_at.astimezone(
                ZoneInfo(institution.timezone)).isoformat()
        pins = session.scalars(
            select(PackageItem).where(PackageItem.package_id == package.id)).all()
        items = []
        for pin in pins:
            version = session.get(EvidenceVersion, pin.evidence_version_id)
            item = session.get(EvidenceItem, pin.item_id)
            items.append({
                "item_id": pin.item_id, "version_id": pin.evidence_version_id,
                "sha256": pin.sha256, "kind": pin.kind, "sensitivity": pin.sensitivity,
                "title": item.title if item else None,
                "version_status": version.status if version else None,
                "pinned_at": _iso(pin.pinned_at),
                "content_accessible": (
                    can_view_content(session, actor, item, version)
                    if item is not None and version is not None else False),
            })

        reviews_out: list[dict] = []
        if actor.role == Role.QUALITY_OFFICER.value:
            reviews = session.scalars(
                select(Review).where(Review.package_id == package.id)).all()
        elif actor.role == Role.REVIEWER.value:
            reviews = session.scalars(
                select(Review).where(Review.package_id == package.id,
                                     Review.reviewer_id == actor.id)).all()
        else:
            reviews = []
        for r in reviews:
            reviews_out.append({"reviewer_id": r.reviewer_id,
                                "recommendation": r.recommendation,
                                "comments": r.comments,
                                "submitted_at": _iso(r.submitted_at),
                                "content_hash": r.content_hash})

        decision = session.scalars(
            select(Decision).where(Decision.package_id == package.id)).first()
        objections_out: list[dict] = []
        if (actor.role == Role.QUALITY_OFFICER.value
                or actor.institution_id == package.institution_id):
            objections = session.scalars(
                select(Objection).where(Objection.package_id == package.id)).all()
            objections_out = [_objection_dict(o) for o in objections]
        open_requests = session.scalar(
            select(func.count(ReReviewRequest.id)).where(
                ReReviewRequest.package_id == package.id,
                ReReviewRequest.status == ReReviewStatus.OPEN.value)) or 0

        return {
            "id": package.id, "institution_id": package.institution_id,
            "term": package.term, "cycle": package.cycle, "status": package.status,
            "deadline_at": _iso(package.deadline_at),
            "deadline_local": deadline_local,
            "created_at": _iso(package.created_at),
            "sealed_at": _iso(package.sealed_at),
            "manifest_hash": package.manifest_hash,
            "supersedes_package_id": package.supersedes_package_id,
            "invalidated_reason": package.invalidated_reason,
            "items": items, "reviews": reviews_out,
            "decision": _decision_dict(decision) if decision else None,
            "objections": objections_out,
            "open_rereview_requests": open_requests,
        }

    def get_visible_package(session: Session, package_id: str, actor: User) -> Package:
        package = session.get(Package, package_id)
        if package is None or not can_view_package(session, actor, package):
            raise NotFoundError("评审包不存在")
        return package

    # ------------------------------------------------------------ 基础

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/me")
    def me(actor: User = Depends(actor_dep)) -> dict:
        return _user_dict(actor)

    # ------------------------------------------------------------ 用户与机构

    @app.post("/users")
    def create_user_route(body: UserCreate, request: Request,
                          actor: User | None = Depends(optional_actor_dep),
                          session: Session = Depends(write_session),
                          idempotency_key: str | None = Header(default=None)):
        def handler():
            user = directory_svc.create_user(
                session, app.state.clock, app.state.ids, actor,
                name=body.name, role=body.role, institution_id=body.institution_id)
            return 201, _user_dict(user)

        status, payload = run_idempotent(session, actor, "POST /users",
                                         idempotency_key, body.model_dump(mode="json"),
                                         handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.patch("/users/{user_id}")
    def update_user_route(user_id: str, body: UserUpdate,
                          actor: User = Depends(actor_dep),
                          session: Session = Depends(write_session)):
        fields = body.model_fields_set
        user = directory_svc.update_user(
            session, app.state.clock, actor, user_id=user_id,
            role=body.role if "role" in fields else None,
            institution_id=(body.institution_id if "institution_id" in fields
                            else directory_svc.UNSET),
            active=body.active if "active" in fields else None)
        session.commit()
        return _user_dict(user)

    @app.post("/institutions")
    def create_institution_route(body: InstitutionCreate,
                                 actor: User = Depends(actor_dep),
                                 session: Session = Depends(write_session),
                                 idempotency_key: str | None = Header(default=None)):
        def handler():
            inst, created = directory_svc.create_institution(
                session, app.state.clock, app.state.ids, actor,
                code=body.code, name=body.name, timezone=body.timezone)
            return (201 if created else 200), _institution_dict(inst)

        status, payload = run_idempotent(session, actor, "POST /institutions",
                                         idempotency_key, body.model_dump(mode="json"),
                                         handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    # ------------------------------------------------------------ 证据

    @app.post("/evidence/items")
    def create_item_route(body: ItemCreate, actor: User = Depends(actor_dep),
                          session: Session = Depends(write_session),
                          idempotency_key: str | None = Header(default=None)):
        def handler():
            item, created = evidence_svc.create_item(
                session, app.state.clock, app.state.ids, actor, **body.model_dump())
            return (201 if created else 200), _item_dict(item)

        status, payload = run_idempotent(session, actor, "POST /evidence/items",
                                         idempotency_key, body.model_dump(mode="json"),
                                         handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.get("/evidence/items/{item_id}")
    def get_item_route(item_id: str, actor: User = Depends(actor_dep),
                       session: Session = Depends(read_session)):
        item = session.get(EvidenceItem, item_id)
        if item is None or not can_view_metadata(session, actor, item):
            raise NotFoundError("证据材料不存在")
        versions = session.scalars(
            select(EvidenceVersion).where(EvidenceVersion.item_id == item.id)
            .order_by(EvidenceVersion.seq)).all()
        return {**_item_dict(item),
                "versions": [_version_dict(
                    v, content_accessible=can_view_content(session, actor, item, v))
                    for v in versions]}

    @app.post("/evidence/items/{item_id}/versions")
    def ingest_version_route(item_id: str, body: VersionCreate,
                             actor: User = Depends(actor_dep),
                             session: Session = Depends(write_session),
                             idempotency_key: str | None = Header(default=None)):
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError("content_base64 不是合法的 Base64") from None
        if len(content) > MAX_CONTENT_BYTES:
            raise ValidationError("证据内容超过大小限制")

        def handler():
            result = evidence_svc.ingest_version(
                session, app.state.clock, app.state.ids, actor,
                item_id=item_id, content=content)
            body_out = {
                "version": _version_dict(
                    result.version,
                    content_accessible=can_view_content(
                        session, actor,
                        session.get(EvidenceItem, item_id), result.version)),
                "created": result.created, "late": result.late,
                "rereview_request_id": result.rereview_request_id,
            }
            return (201 if result.created else 200), body_out

        status, payload = run_idempotent(
            session, actor, f"POST /evidence/items/{item_id}/versions",
            idempotency_key, {"item_id": item_id, "sha256_source": body.content_base64},
            handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.get("/evidence/versions/{version_id}")
    def get_version_route(version_id: str, actor: User = Depends(actor_dep),
                          session: Session = Depends(read_session)):
        version = session.get(EvidenceVersion, version_id)
        if version is None:
            raise NotFoundError("证据版本不存在")
        item = session.get(EvidenceItem, version.item_id)
        if not can_view_metadata(session, actor, item):
            raise NotFoundError("证据版本不存在")
        return _version_dict(
            version, content_accessible=can_view_content(session, actor, item, version))

    @app.get("/evidence/versions/{version_id}/content")
    def get_version_content_route(version_id: str, actor: User = Depends(actor_dep),
                                  session: Session = Depends(read_session)):
        version = session.get(EvidenceVersion, version_id)
        if version is None:
            raise NotFoundError("证据版本不存在")
        item = session.get(EvidenceItem, version.item_id)
        if not can_view_metadata(session, actor, item):
            raise NotFoundError("证据版本不存在")
        if not can_view_content(session, actor, item, version):
            raise PermissionDeniedError("该材料按最小披露原则不对您开放内容")
        return Response(content=bytes(version.content),
                        media_type="application/octet-stream",
                        headers={"X-Content-Sha256": version.sha256})

    @app.post("/evidence/versions/{version_id}/withdraw")
    def withdraw_version_route(version_id: str, body: WithdrawRequest,
                               actor: User = Depends(actor_dep),
                               session: Session = Depends(write_session),
                               idempotency_key: str | None = Header(default=None)):
        def handler():
            version = evidence_svc.withdraw_version(
                session, app.state.clock, app.state.ids, actor,
                version_id=version_id, reason=body.reason)
            item = session.get(EvidenceItem, version.item_id)
            return 200, _version_dict(
                version, content_accessible=can_view_content(session, actor, item, version))

        status, payload = run_idempotent(
            session, actor, f"POST /evidence/versions/{version_id}/withdraw",
            idempotency_key, {"version_id": version_id, "reason": body.reason}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    # ------------------------------------------------------------ 评审包

    @app.post("/packages")
    def create_package_route(body: PackageCreate, actor: User = Depends(actor_dep),
                             session: Session = Depends(write_session),
                             idempotency_key: str | None = Header(default=None)):
        deadline = _aware(body.deadline_at)

        def handler():
            package, created = package_svc.create_package(
                session, app.state.clock, app.state.ids, actor,
                institution_id=body.institution_id, term=body.term,
                deadline_at=deadline)
            return (201 if created else 200), package_dict(session, package, actor)

        status, payload = run_idempotent(session, actor, "POST /packages",
                                         idempotency_key, body.model_dump(mode="json"),
                                         handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.get("/packages/{package_id}")
    def get_package_route(package_id: str, actor: User = Depends(actor_dep),
                          session: Session = Depends(read_session)):
        package = get_visible_package(session, package_id, actor)
        return package_dict(session, package, actor)

    @app.post("/packages/{package_id}/items")
    def pin_item_route(package_id: str, body: PinRequest,
                       actor: User = Depends(actor_dep),
                       session: Session = Depends(write_session),
                       idempotency_key: str | None = Header(default=None)):
        def handler():
            pin = package_svc.add_item(session, app.state.clock, actor,
                                       package_id=package_id,
                                       evidence_version_id=body.evidence_version_id)
            return 201, {"package_id": pin.package_id,
                         "evidence_version_id": pin.evidence_version_id,
                         "sha256": pin.sha256, "pinned_at": _iso(pin.pinned_at)}

        status, payload = run_idempotent(
            session, actor, f"POST /packages/{package_id}/items", idempotency_key,
            {"package_id": package_id, **body.model_dump(mode="json")}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.post("/packages/{package_id}/seal")
    def seal_package_route(package_id: str, actor: User = Depends(actor_dep),
                           session: Session = Depends(write_session),
                           idempotency_key: str | None = Header(default=None)):
        def handler():
            package = package_svc.seal_package(session, app.state.clock, actor,
                                               package_id=package_id)
            return 200, package_dict(session, package, actor)

        status, payload = run_idempotent(
            session, actor, f"POST /packages/{package_id}/seal", idempotency_key,
            {"package_id": package_id}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.post("/packages/{package_id}/assignments")
    def assign_reviewer_route(package_id: str, body: AssignmentCreate,
                              actor: User = Depends(actor_dep),
                              session: Session = Depends(write_session),
                              idempotency_key: str | None = Header(default=None)):
        def handler():
            assignment = review_svc.assign_reviewer(
                session, app.state.clock, app.state.ids, actor,
                package_id=package_id, reviewer_id=body.reviewer_id)
            return 201, {"id": assignment.id, "package_id": assignment.package_id,
                         "reviewer_id": assignment.reviewer_id,
                         "status": assignment.status,
                         "assigned_at": _iso(assignment.assigned_at)}

        status, payload = run_idempotent(
            session, actor, f"POST /packages/{package_id}/assignments", idempotency_key,
            {"package_id": package_id, **body.model_dump(mode="json")}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.delete("/packages/{package_id}/assignments/{reviewer_id}")
    def revoke_assignment_route(package_id: str, reviewer_id: str,
                                actor: User = Depends(actor_dep),
                                session: Session = Depends(write_session)):
        assignment = review_svc.revoke_assignment(
            session, app.state.clock, actor, package_id=package_id,
            reviewer_id=reviewer_id)
        session.commit()
        return {"id": assignment.id, "package_id": assignment.package_id,
                "reviewer_id": assignment.reviewer_id, "status": assignment.status,
                "revoked_at": _iso(assignment.revoked_at)}

    @app.post("/packages/{package_id}/reviews")
    def submit_review_route(package_id: str, body: ReviewSubmit,
                            actor: User = Depends(actor_dep),
                            session: Session = Depends(write_session),
                            idempotency_key: str | None = Header(default=None)):
        def handler():
            review = review_svc.submit_review(
                session, app.state.clock, app.state.ids, actor, package_id=package_id,
                recommendation=body.recommendation, comments=body.comments)
            return 201, {"id": review.id, "package_id": review.package_id,
                         "reviewer_id": review.reviewer_id,
                         "recommendation": review.recommendation,
                         "content_hash": review.content_hash,
                         "submitted_at": _iso(review.submitted_at)}

        status, payload = run_idempotent(
            session, actor, f"POST /packages/{package_id}/reviews", idempotency_key,
            {"package_id": package_id, **body.model_dump(mode="json")}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.post("/packages/{package_id}/decision")
    def issue_decision_route(package_id: str, body: DecisionIssue,
                             actor: User = Depends(actor_dep),
                             session: Session = Depends(write_session),
                             idempotency_key: str | None = Header(default=None)):
        def handler():
            decision = review_svc.issue_decision(
                session, app.state.clock, app.state.ids, actor, package_id=package_id,
                outcome=body.outcome, rationale=body.rationale)
            return 201, _decision_dict(decision)

        status, payload = run_idempotent(
            session, actor, f"POST /packages/{package_id}/decision", idempotency_key,
            {"package_id": package_id, **body.model_dump(mode="json")}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.post("/packages/{package_id}/objections")
    def raise_objection_route(package_id: str, body: ObjectionRaise,
                              actor: User = Depends(actor_dep),
                              session: Session = Depends(write_session),
                              idempotency_key: str | None = Header(default=None)):
        def handler():
            objection = review_svc.raise_objection(
                session, app.state.clock, app.state.ids, actor, package_id=package_id,
                reason=body.reason)
            return 201, _objection_dict(objection)

        status, payload = run_idempotent(
            session, actor, f"POST /packages/{package_id}/objections", idempotency_key,
            {"package_id": package_id, **body.model_dump(mode="json")}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.post("/objections/{objection_id}/resolve")
    def resolve_objection_route(objection_id: str, body: ObjectionResolve,
                                actor: User = Depends(actor_dep),
                                session: Session = Depends(write_session),
                                idempotency_key: str | None = Header(default=None)):
        def handler():
            objection = review_svc.resolve_objection(
                session, app.state.clock, actor, objection_id=objection_id,
                outcome=body.outcome, note=body.note)
            return 200, _objection_dict(objection)

        status, payload = run_idempotent(
            session, actor, f"POST /objections/{objection_id}/resolve", idempotency_key,
            {"objection_id": objection_id, **body.model_dump(mode="json")}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.post("/packages/{package_id}/rereview")
    def open_rereview_route(package_id: str, body: ReReviewOpen,
                            actor: User = Depends(actor_dep),
                            session: Session = Depends(write_session),
                            idempotency_key: str | None = Header(default=None)):
        new_deadline = _aware(body.new_deadline_at)

        def handler():
            package, created = package_svc.open_rereview(
                session, app.state.clock, app.state.ids, actor, package_id=package_id,
                new_deadline_at=new_deadline)
            return (201 if created else 200), package_dict(session, package, actor)

        status, payload = run_idempotent(
            session, actor, f"POST /packages/{package_id}/rereview", idempotency_key,
            {"package_id": package_id, **body.model_dump(mode="json")}, handler)
        session.commit()
        return JSONResponse(payload, status_code=status)

    @app.get("/packages/{package_id}/rereview-requests")
    def list_rerequests_route(package_id: str, actor: User = Depends(actor_dep),
                              session: Session = Depends(read_session)):
        package = get_visible_package(session, package_id, actor)
        requests = session.scalars(
            select(ReReviewRequest).where(ReReviewRequest.package_id == package.id)
            .order_by(ReReviewRequest.created_at)).all()
        return {"package_id": package.id,
                "requests": [_rerequest_dict(r) for r in requests]}

    return app
