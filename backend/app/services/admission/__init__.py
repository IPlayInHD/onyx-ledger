"""Admission control (Entry 10).

Bounds how much expensive work one principal, or the platform, can have in
flight — decided before the expensive work starts.
"""
from app.services.admission.auth import admit_auth_attempt
from app.services.admission.guard import (
    admin_scope,
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
    AdmissionOutcome,
    AdmissionRejected,
    AdmissionService,
    AdmissionTicket,
    AuthAdmissionDecision,
    admission_metrics,
    reset_admission_metrics,
)

__all__ = [
    "ADMISSION_POLICY_VERSION",
    "POLICIES",
    "AdmissionOutcome",
    "AdmissionPolicy",
    "AdmissionRejected",
    "AdmissionService",
    "AdmissionTicket",
    "AuthAdmissionDecision",
    "OperationClass",
    "RejectionReason",
    "ScopeType",
    "StoreFailurePolicy",
    "admin_scope",
    "admission_guard",
    "admission_metrics",
    "admit_auth_attempt",
    "owned_dedupe_key",
    "policy_for",
    "reset_admission_metrics",
    "user_scope",
]
