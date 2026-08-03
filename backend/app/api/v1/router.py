"""Aggregates all v1 sub-routers."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.admin.routes import router as admin_router
from app.api.v1.ai.routes import router as ai_router
from app.api.v1.analysis.routes import router as analysis_router
from app.api.v1.auth.routes import router as auth_router
from app.api.v1.documents.routes import router as documents_router
from app.api.v1.financials.routes import router as financials_router
from app.api.v1.ioe.routes import router as ioe_router
from app.api.v1.tax.routes import router as tax_router
from app.api.v1.tkms.routes import router as tkms_router
from app.api.v1.users.routes import router as users_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(financials_router)
api_router.include_router(tax_router)
api_router.include_router(analysis_router)
api_router.include_router(documents_router)
api_router.include_router(ai_router)
api_router.include_router(admin_router)
api_router.include_router(tkms_router)
api_router.include_router(ioe_router)
