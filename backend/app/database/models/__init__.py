"""SQLAlchemy models mapping the validated DB schemas (one module per domain).

Only the tables exercised by the current vertical slice are mapped here; the
remaining ~50 tables follow the identical pattern (schema-qualified table args,
server-generated UUIDv7 ids, timestamptz audit columns).
"""
from app.database.models.analysis import (  # noqa: F401
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    ReconciliationCheck,
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
from app.database.models.ref import ExpenseCategory, IncomeType, Province  # noqa: F401
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
