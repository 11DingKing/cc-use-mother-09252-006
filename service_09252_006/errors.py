"""领域错误类型：每种错误映射到稳定的 HTTP 状态码与机器可读码。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务错误的基类。"""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class UnauthenticatedError(DomainError):
    status_code = 401
    code = "unauthenticated"


class PermissionDeniedError(DomainError):
    status_code = 403
    code = "permission_denied"


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"
