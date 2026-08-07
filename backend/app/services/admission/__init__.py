"""Admission control (Entry 10).

Bounds how much expensive work one principal, or the platform, can have in
flight — decided before the expensive work starts.
"""
from app.services.admission.guard import (
    admission_guard,
    owned_dedupe_key,
    user_scope,
)
from app.services.admission.policy import (
    POLICIES,
    AdmissionPolicy,
    OperationClass,
    RejectionReason,
    ScopeType,
    StoreFailurePolicy,
    policy_for,
)
from app.services.admission.service import (
    ADMISSION_POLICY_VERSION,
    AdmissionRejected,
    AdmissionService,
    AdmissionTicket,
    admission_metrics,
    reset_admission_metrics,
)

__all__ = [
    "ADMISSION_POLICY_VERSION",
    "POLICIES",
    "AdmissionPolicy",
    "AdmissionRejected",
    "AdmissionService",
    "AdmissionTicket",
    "OperationClass",
    "RejectionReason",
    "ScopeType",
    "StoreFailurePolicy",
    "admission_guard",
    "admission_metrics",
    "owned_dedupe_key",
    "policy_for",
    "reset_admission_metrics",
    "user_scope",
]
