"""Domain exception hierarchy → RFC-9457 problem+json.

Services raise these framework-free errors; a single FastAPI handler maps them
to `application/problem+json` responses with a correlation id.
"""
from __future__ import annotations


class DomainError(Exception):
    """Base for all expected, mapped errors."""

    status_code: int = 400
    error_type: str = "about:blank"
    title: str = "Bad Request"

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.title
        super().__init__(self.detail)


class ValidationError(DomainError):
    status_code = 422
    error_type = "https://onyx.ledger/errors/validation"
    title = "Validation Error"


class Unauthorized(DomainError):
    status_code = 401
    error_type = "https://onyx.ledger/errors/unauthorized"
    title = "Unauthorized"


class Forbidden(DomainError):
    status_code = 403
    error_type = "https://onyx.ledger/errors/forbidden"
    title = "Forbidden"


class NotFound(DomainError):
    status_code = 404
    error_type = "https://onyx.ledger/errors/not-found"
    title = "Not Found"


class Conflict(DomainError):
    status_code = 409
    error_type = "https://onyx.ledger/errors/conflict"
    title = "Conflict"


class RateLimited(DomainError):
    status_code = 429
    error_type = "https://onyx.ledger/errors/rate-limited"
    title = "Too Many Requests"


class ExternalServiceError(DomainError):
    status_code = 502
    error_type = "https://onyx.ledger/errors/upstream"
    title = "Upstream Service Error"
