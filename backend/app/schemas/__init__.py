from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth ----
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


# ---- profile ----
class TaxProfileIn(BaseModel):
    province_code: str | None = None
    marital_status: str | None = None
    is_student: bool = False
    has_disability: bool = False
    first_time_home_buyer: bool = False
    is_self_employed: bool = False
    owns_home: bool = False
    has_investments: bool = False
    has_rental_income: bool = False


class TaxProfileOut(ORMModel):
    user_id: uuid.UUID
    province_code: str | None
    marital_status: str | None
    is_student: bool
    is_self_employed: bool
    has_rental_income: bool


# ---- financials ----
class IncomeIn(BaseModel):
    tax_year: int = Field(ge=1900, le=2200)
    income_type_code: str
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    source_name: str | None = None


class IncomeOut(ORMModel):
    id: uuid.UUID
    tax_year: int
    income_type_id: uuid.UUID
    amount: Decimal
    source_name: str | None


class ExpenseIn(BaseModel):
    tax_year: int = Field(ge=1900, le=2200)
    expense_category_code: str
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    description: str | None = None


# ---- analysis ----
class AnalysisRequest(BaseModel):
    tax_year: int = Field(ge=1900, le=2200)


class LineItemOut(BaseModel):
    kind: str
    label: str
    amount: Decimal


class RecommendationOut(ORMModel):
    id: uuid.UUID
    opportunity_code: str
    title: str
    category: str | None
    estimated_impact: Decimal | None
    confidence_score: int | None
    priority: int
    citation: str | None
    status: str


class AnalysisOut(ORMModel):
    id: uuid.UUID
    tax_year: int
    province_code: str | None
    engine_version: str
    taxable_income: Decimal | None
    estimated_tax: Decimal | None
    marginal_rate: Decimal | None
    average_rate: Decimal | None
    confidence_score: int | None
    created_at: datetime
