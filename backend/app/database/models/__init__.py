"""SQLAlchemy models mapping the validated DB schemas (one module per domain).

Only the tables exercised by the current vertical slice are mapped here; the
remaining ~50 tables follow the identical pattern (schema-qualified table args,
server-generated UUIDv7 ids, timestamptz audit columns).
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
    AiMessage,
    AiMessageCitation,
    AiPromptContext,
    KnowledgeEmbedding,
)
from app.database.models.analysis import (  # noqa: F401
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    ReconciliationCheck,
)
from app.database.models.docs import (  # noqa: F401
    Document,
    DocumentExtraction,
    DocumentLink,
    ExtractionField,
)
from app.database.models.finance import ExpenseRecord, IncomeSource  # noqa: F401
from app.database.models.identity import (  # noqa: F401,E501
    AuthSession,
    LoginEvent,
    UserAccount,
    UserCredential,
)
from app.database.models.profile import TaxProfile  # noqa: F401
from app.database.models.reco import Recommendation  # noqa: F401
from app.database.models.ref import (  # noqa: F401
    DocumentType,
    ExpenseCategory,
    IncomeType,
    Jurisdiction,
    Province,
)
from app.database.models.tax_kb import (  # noqa: F401
    CalcFormula,
    CalcFormulaInput,
    FactDefinition,
    RuleCondition,
    RuleConditionGroup,
    RuleOutcome,
    TaxRule,
    TaxRuleVersion,
)
