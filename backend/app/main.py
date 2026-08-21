"""FastAPI application factory: middleware, exception handlers, routes, health."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.core.logging import configure_logging, correlation_id, get_logger
from app.core.middleware import CorrelationIdMiddleware
from app.services.admission import AdmissionRejected
from app.services.ioe.retention.service import StaleAcknowledgement
from app.services.ioe.scenario.before_you_act import ComparisonUnavailable

settings = get_settings()
configure_logging(settings.debug)
log = get_logger("onyx")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Onyx Ledger API",
        version="0.1.0",
        description="AI-powered Canadian tax optimization SaaS — educational, not filing.",
        openapi_url=f"{settings.api_v1_prefix}/openapi.json",
    )
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(
        request: Request, exc: DomainError
    ) -> JSONResponse:
        # RFC-9457 application/problem+json
        body: dict[str, object] = {
            "type": exc.error_type,
            "title": exc.title,
            "status": exc.status_code,
            "detail": exc.detail,
            "correlation_id": correlation_id.get(),
        }
        headers: dict[str, str] = {}

        if isinstance(exc, AdmissionRejected):
            # A rejected caller gets a closed operation code, a closed reason
            # code, and when to come back. Deliberately NOT: queue depth, worker
            # counts, how much of the limit is left, or anything about another
            # principal's activity — each of those turns a rejection into a
            # reconnaissance signal.
            body["operation_code"] = exc.operation.value
            body["error_code"] = exc.reason.value
            body["retry_after_seconds"] = exc.retry_after_seconds
            headers["Retry-After"] = str(exc.retry_after_seconds)

        if isinstance(exc, StaleAcknowledgement):
            # Which guard refused: the state moved, or the baseline was already
            # superseded. A client that cannot tell them apart cannot recover
            # correctly — one calls for a re-read, the other for showing the
            # user what someone else acknowledged first. A closed code only.
            body["error_code"] = exc.reason

        if isinstance(exc, ComparisonUnavailable):
            # Same reasoning, different question. The caller already proved they
            # own this scenario, so naming the governed condition tells them
            # nothing about anyone else — and without it, "cannot compare" and
            # "comparison is empty" would be indistinguishable to a client.
            # A closed enum value only: never a message, a row, or a path.
            body["error_code"] = exc.reason.value

        return JSONResponse(
            status_code=exc.status_code,
            media_type="application/problem+json",
            content=body,
            headers=headers or None,
        )

    @app.on_event("startup")
    async def _activate_calculation_versions() -> None:
        """Reconcile the ACTIVE calculation versions with what is running.

        Startup is the trigger, not the authority. Each component is activated
        through `VersionActivationService`, which compare-and-swaps a database
        row under `FOR UPDATE` and emits at most one event per real transition,
        in the same transaction. So:

          * ten replicas starting together produce one activation, not ten;
          * a restart with nothing changed produces no update and no event;
          * a failed event insert rolls the activation back.

        A startup that cannot reach the database must not stop the API serving —
        the hourly sweep and read-time evaluation remain the safety nets — so
        this is logged and swallowed rather than fatal.
        """
        from app.database.session import unit_of_work
        from app.services.ioe.freshness_producers import RUNNING_VERSIONS
        from app.services.ioe.version_activation import VersionActivationService

        try:
            activated = []
            async with unit_of_work(actor_type="system") as session:
                service = VersionActivationService(session)
                for event, resolve in RUNNING_VERSIONS.items():
                    outcome = await service.activate(event, resolve())
                    if outcome.activated:
                        activated.append(outcome.version_type)
            if activated:
                log.info("freshness_versions_activated", components=activated)
        except Exception:  # noqa: BLE001 - never block startup on this
            log.warning("freshness_version_activation_skipped")

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/healthz", tags=["platform"])
    async def healthz() -> dict:
        return {"status": "ok"}

    # The return annotation admits both branches — a bare `dict` claimed a shape
    # the 503 path never produces. `response_model=None` is required alongside
    # it: FastAPI otherwise tries to build a Pydantic response model from the
    # union and refuses `JSONResponse` as a field type.
    @app.get("/readyz", tags=["platform"], response_model=None)
    async def readyz() -> dict | JSONResponse:
        from sqlalchemy import text

        from app.database.session import engine

        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception:  # noqa: BLE001
            # THE REASON DOES NOT GO IN THE RESPONSE. This endpoint is
            # unauthenticated — a load balancer has to reach it, so anyone can.
            # `str(e)` on a connection failure is not a tidy sentence: SQLAlchemy
            # and asyncpg put the host, the port, the database name and the
            # runtime username into it, and a DSN parse error can carry the
            # password itself. That is a free map of the private network handed
            # to whoever asks.
            #
            # The prober only needs the verdict. The operator needs the reason,
            # and gets it from the log, where it is already correlated.
            log.warning("readiness_check_failed", exc_info=True)
            return JSONResponse(status_code=503, content={"status": "not_ready"})

    return app


app = create_app()
