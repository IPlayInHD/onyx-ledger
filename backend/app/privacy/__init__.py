"""Privacy data-lifecycle governance (Entry 11A).

Specification-first: this package declares WHAT each piece of user-derived data
is and what must happen to it. It performs no deletion. Entry 11B implements
the mechanisms these declarations govern.
"""
from app.privacy.classification import (
    LIFECYCLE,
    DeletionAction,
    LifecycleState,
    PrivacyClass,
    RetentionClass,
    SourceKind,
    TableLifecycle,
)

__all__ = [
    "LIFECYCLE",
    "DeletionAction",
    "LifecycleState",
    "PrivacyClass",
    "RetentionClass",
    "SourceKind",
    "TableLifecycle",
]
