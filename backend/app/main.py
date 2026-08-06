"""FastAPI application factory: middleware, exception handlers, routes, health."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.core.logging import configure_logging, correlation_id, get_logger
from app.core.middleware import CorrelationIdMiddleware

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
    async def _domain_error_handler(request: Request, exc: DomainError):
        # RFC-9457 application/problem+json
        return JSONResponse(
            status_code=exc.status_code,
            media_type="application/problem+json",
            content={
                "type": exc.error_type,
                "title": exc.title,
                "status": exc.status_code,
                "detail": exc.detail,
                "correlation_id": correlation_id.get(),
            },
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

    @app.get("/readyz", tags=["platform"])
    async def readyz() -> dict:
        from sqlalchemy import text

        from app.database.session import engine

        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"status": "not_ready", "detail": str(e)})

    return app


app = create_app()
