"""Cross-cutting HTTP middleware: correlation id + request timing."""
from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import correlation_id, get_logger

log = get_logger("onyx.request")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
        correlation_id.set(cid)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["X-Correlation-Id"] = cid
        log.info("request", method=request.method, path=request.url.path,
                 status=response.status_code, elapsed_ms=elapsed_ms)
        return response
