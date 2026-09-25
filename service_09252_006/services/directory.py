"""机构与用户目录：首个质量官引导、角色与权限变更（变更即刻生效）。"""
from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import record_event
from ..errors import NotFoundError, PermissionDeniedError, ValidationError
from ..models import Institution, Role, User
from ..ports import Clock, IdGenerator

_INSTITUTION_ROLES = (Role.INSTITUTION_ADMIN.value, Role.COORDINATOR.value)
_GLOBAL_ROLES = (Role.QUALITY_OFFICER.value, Role.REVIEWER.value)

UNSET = object()


def require_officer(actor: User | None) -> None:
    if actor is None or actor.role != Role.QUALITY_OFFICER.value:
        raise PermissionDeniedError("仅质量官可执行该操作")


def _role_value(role: str) -> str:
    try:
        return Role(role).value
    except ValueError:
        raise ValidationError(f"非法角色: {role}",
                              details={"allowed": [r.value for r in Role]}) from None


def create_institution(session: Session, clock: Clock, ids: IdGenerator, actor: User,
                       *, code: str, name: str, timezone: str) -> tuple[Institution, bool]:
    """创建机构；code 为自然键，重复创建返回既有机构（幂等）。"""
    require_officer(actor)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(f"未知时区: {timezone}") from None
    existing = session.scalars(select(Institution).where(Institution.code == code)).first()
    if existing is not None:
        return existing, False
    inst = Institution(id=ids.new_id(), code=code, name=name, timezone=timezone,
                       created_at=clock.now())
    session.add(inst)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:  # 并发创建同 code：以先提交者为准
        existing = session.scalars(select(Institution).where(Institution.code == code)).one()
        return existing, False
    record_event(session, clock, actor_id=actor.id, action="institution.created",
                 entity_type="institution", entity_id=inst.id,
                 payload={"code": code, "name": name, "timezone": timezone})
    return inst, True


def create_user(session: Session, clock: Clock, ids: IdGenerator, actor: User | None,
                *, name: str, role: str, institution_id: str | None = None) -> User:
    """创建用户；系统内首个用户必须是质量官（引导），其后仅质量官可创建。"""
    role_v = _role_value(role)
    total = session.scalar(select(func.count(User.id))) or 0
    if total == 0:
        if role_v != Role.QUALITY_OFFICER.value:
            raise ValidationError("系统首个用户必须是质量官")
    else:
        require_officer(actor)
    if role_v in _INSTITUTION_ROLES and not institution_id:
        raise ValidationError("机构角色必须绑定机构")
    if role_v in _GLOBAL_ROLES:
        institution_id = None  # 全局角色不绑定机构
    if institution_id is not None and session.get(Institution, institution_id) is None:
        raise NotFoundError("机构不存在")
    user = User(id=ids.new_id(), name=name, role=role_v, institution_id=institution_id,
                active=True, created_at=clock.now())
    session.add(user)
    session.flush()
    record_event(session, clock, actor_id=actor.id if actor else "system",
                 action="user.created", entity_type="user", entity_id=user.id,
                 payload={"name": name, "role": role_v, "institution_id": institution_id})
    return user


def update_user(session: Session, clock: Clock, actor: User, *, user_id: str,
                role: str | None = None, institution_id=UNSET,
                active: bool | None = None) -> User:
    """变更角色/机构/停用；访问策略按请求实时计算，因此变更立即生效。"""
    require_officer(actor)
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError("用户不存在")
    before = {"role": user.role, "institution_id": user.institution_id, "active": user.active}

    new_role = _role_value(role) if role is not None else user.role
    new_institution = user.institution_id if institution_id is UNSET else institution_id
    if new_role in _GLOBAL_ROLES:
        new_institution = None
    if new_role in _INSTITUTION_ROLES and not new_institution:
        raise ValidationError("机构角色必须绑定机构")
    if new_institution is not None and session.get(Institution, new_institution) is None:
        raise NotFoundError("机构不存在")

    user.role = new_role
    user.institution_id = new_institution
    if active is not None:
        user.active = bool(active)
    session.flush()
    record_event(session, clock, actor_id=actor.id, action="user.updated",
                 entity_type="user", entity_id=user.id,
                 payload={"before": before,
                          "after": {"role": user.role, "institution_id": user.institution_id,
                                    "active": user.active}})
    return user
