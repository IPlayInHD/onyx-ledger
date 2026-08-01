"""SQLAlchemy models mapping the validated DB schemas (one module per domain).

Every base table in backend/db/sql is mapped here so the app can query it and
`alembic revision --autogenerate` sees the whole schema. Partitioning, RLS,
triggers, CHECK constraints, and the HNSW index remain hand-written SQL (they
are not modelled by autogenerate) — see backend/db/sql.
"""
from app.database.models.admin import (  # noqa: F401
    AdminUser,
    AdminUserRole,
    Permission,
    Role,
    RolePermission,
    RuleChangeRequest,
    RulePublication,
)
from app.database.models.ai import (  # noqa: F401
    AiConversation,
    AiExplanation,
    AiMessage,
    AiMessageCitation,
    AiPromptContext,
    KnowledgeEmbedding,
)
from app.database.models.analysis import (  # noqa: F401
    AnalysisAssumption,
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    ReconciliationCheck,
)
from app.database.models.audit import (  # noqa: F401
    AuditLog,
    ConsentLog,
    DataDeletionRequest,
    DataExportRequest,
    SecurityEvent,
)
from app.database.models.billing import (  # noqa: F401
    Entitlement,
    Invoice,
    PaymentMethodRef,
    Plan,
    Subscription,
)
from app.database.models.docs import (  # noqa: F401
    Document,
    DocumentExtraction,
    DocumentLink,
    ExtractionField,
)
from app.database.models.finance import ExpenseRecord, IncomeSource  # noqa: F401
from app.database.models.identity import (  # noqa: F401
    AuthSession,
    EmailVerificationToken,
    LoginEvent,
    MfaMethod,
    PasswordResetToken,
    UserAccount,
    UserCredential,
)
from app.database.models.profile import (  # noqa: F401
    Dependent,
    SpouseProfile,
    TaxProfile,
    UserPreference,
    UserPrivacySetting,
    UserProfile,
)
from app.database.models.reco import (  # noqa: F401
    Recommendation,
    RecommendationFeedback,
    RecommendationStatusEvent,
)
from app.database.models.ref import (  # noqa: F401
    AccountRegisteredType,
    AssetCategory,
    ConditionOperator,
    Currency,
    DocumentType,
    EmploymentType,
    ExpenseCategory,
    HousingStatus,
    IncomeType,
    Jurisdiction,
    LiabilityCategory,
    MaritalStatus,
    Province,
    ResidencyStatus,
    RuleCategory,
    TaxYear,
    VerificationStatus,
)
from app.database.models.tax_kb import (  # noqa: F401
    BenefitParameter,
    BenefitProgram,
    CalcConstant,
    CalcFormula,
    CalcFormulaInput,
    ConditionValueSet,
    ConditionValueSetItem,
    ContributionLimit,
    FactDefinition,
    GovSource,
    LegislationReference,
    RuleCondition,
    RuleConditionGroup,
    RuleOutcome,
    TaxBracket,
    TaxBracketSet,
    TaxRule,
    TaxRuleVersion,
)
from app.database.models.wealth import (  # noqa: F401
    Asset,
    AssetValuation,
    Liability,
    LiabilityBalance,
    RegisteredAccountDetail,
)
