"""The deterministic validators. Pure functions, no session, no clock.

Every check here fails CLOSED. That is the correction of a real weakness in the
validation this repository already had, where checks read

    if ctx.known_facts and cond.fact_key not in ctx.known_facts:

so an empty reference set — a context that failed to load, a fresh database, a
caller that passed nothing — turned every reference-integrity check into a
no-op that reported success. A vocabulary the pipeline cannot see is a question
it cannot answer, and an unanswerable question is not a pass.

The other correction is the formula checker. Its predecessor bound every
unrecognized token to a placeholder before evaluating:

    for token in expression.split():
        if not _is_number(token) and token not in _RPN_OPS and token not in variables:
            variables[token] = Decimal(1)

which means an UNDECLARED input made the expression valid. §53 requires exactly
the opposite, and the checker below resolves tokens only against what the author
declared.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.services.ioe.domain.levers import all_levers
from app.services.tax_engine.core.formula_sandbox import SUPPORTED_OPERATIONS
from app.services.tax_kb.authoring.codes import Family, ValidationCode
from app.services.tax_kb.authoring.report import Finding, error, warning
from app.services.tax_kb.authoring.spec import (
    ConditionGroupSpec,
    ConditionSpec,
    FormulaSpec,
    OutcomeSpec,
    ReferenceDataKind,
    ReferenceDataSpec,
    TaxKnowledgeDraftSpec,
)

# ---------------------------------------------------------------------------
# Vocabularies fixed by CHECK constraints rather than reference tables.
#
# Mirrored here so validation is a pure function, and drift-guarded by a test
# that reads `pg_get_constraintdef` and asserts the two agree — a mirror nobody
# checks is how a constraint and its validator come to disagree.
# ---------------------------------------------------------------------------
VALUE_TYPES = frozenset({
    "number", "money", "percent", "boolean", "text", "date", "set"})
LOGICAL_OPS = frozenset({"AND", "OR", "NOT"})
OUTCOME_TYPES = frozenset({
    "recommend", "apply_credit", "apply_deduction", "flag_benefit_eligibility"})
DEPENDENCY_TYPES = frozenset({"requires", "precedes", "excludes", "substitutes"})
EVIDENCE_NECESSITY = frozenset({"required", "recommended", "conditional"})
POOL_SCOPES = frozenset({"individual", "household"})
COST_TYPES = frozenset({
    "required_cash_contribution", "required_expenditure", "implementation_cost",
    "liquidity_commitment", "asset_transfer", "nonrecoverable_expenditure"})
ECONOMIC_EFFECT_TYPES = frozenset({
    "immediate_refund_impact", "current_year_tax_reduction", "tax_deferral",
    "refundable_benefit", "recurring_annual_benefit",
    "multi_year_projected_benefit", "future_option_value"})
REVERSIBILITY = frozenset({"reversible", "partially_reversible", "irreversible"})
BRACKET_SET_KINDS = frozenset({"income_tax", "surtax"})

#: Operators that need a numeric operand, and those that need none at all.
_NUMERIC_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "between"})
_SET_OPERATORS = frozenset({"in", "contains"})
_NULLARY_OPERATORS = frozenset({"exists", "is_true"})

#: The widest scale the canonical money/rate serializers accept. A literal with
#: more precision than the engine can store is a number that changes on the way
#: into the database, which is the one thing a tax formula may not do.
_MAX_LITERAL_EXPONENT = 6


@dataclass(frozen=True)
class KnowledgeContext:
    """Everything the validators must resolve names against.

    Loaded once by the service and passed in, so validation stays a pure
    function of (spec, context) and can be exercised without a database.
    """

    known_facts: frozenset[str] = frozenset()
    known_jurisdictions: frozenset[str] = frozenset()
    known_provinces: frozenset[str] = frozenset()
    known_years: frozenset[int] = frozenset()
    known_categories: frozenset[str] = frozenset()
    known_operators: frozenset[str] = frozenset()
    known_document_types: frozenset[str] = frozenset()
    known_registered_types: frozenset[str] = frozenset()
    known_deadline_codes: frozenset[str] = frozenset()
    known_assumption_codes: frozenset[str] = frozenset()
    #: Rule codes a dependency may name: everything published, plus the other
    #: members of the pack being validated — §47, so B need not wait for A to
    #: publish before it can be checked.
    known_rule_codes: frozenset[str] = frozenset()
    rule_jurisdictions: Mapping[str, str] = field(default_factory=dict)
    published_reference_data: frozenset[tuple[str, str, int]] = frozenset()
    published_formulas: Mapping[str, str] = field(default_factory=dict)
    #: Published (rule_code, tax_year) → the version id currently holding it.
    #: Its KEYS are the set of occupied slots; the values let a declared
    #: supersession target be checked against the version it claims to replace.
    published_rule_versions: Mapping[tuple[str, int], object] = field(
        default_factory=dict)
    #: rule_code → (jurisdiction, category, subcategory, province, name) as
    #: ALREADY REGISTERED. `tax_rule.code` is globally unique, so a draft
    #: reusing a code inherits that row's identity whatever the draft says —
    #: including the NAME, which is part of the specification hash and would
    #: otherwise surface only as an unexplained mismatch at publication.
    rule_identities: Mapping[str, tuple[str | None, ...]] = field(
        default_factory=dict)
    known_levers: frozenset[str] = field(
        default_factory=lambda: frozenset(x.code for x in all_levers()))


# ---------------------------------------------------------------------------
# Structure, scope, effective period
# ---------------------------------------------------------------------------
def validate_structure(spec: TaxKnowledgeDraftSpec,
                       ctx: KnowledgeContext) -> list[Finding]:
    ref = spec.rule_code
    out: list[Finding] = []

    if spec.category not in ctx.known_categories:
        out.append(error(ref, Family.STRUCTURE, ValidationCode.UNKNOWN_CATEGORY,
                         f"category {spec.category!r} is not a known rule category"))
    if spec.jurisdiction_code not in ctx.known_jurisdictions:
        out.append(error(ref, Family.STRUCTURE, ValidationCode.UNKNOWN_JURISDICTION,
                         f"jurisdiction {spec.jurisdiction_code!r} is not "
                         "registered in ref.jurisdiction"))
    if spec.province_code is not None:
        if spec.jurisdiction_code == "FED":
            out.append(error(
                ref, Family.STRUCTURE, ValidationCode.FEDERAL_RULE_WITH_PROVINCE,
                f"a federal rule carries province {spec.province_code!r}; a "
                "provincial rule must not publish as federal"))
        if spec.province_code not in ctx.known_provinces:
            out.append(error(ref, Family.STRUCTURE,
                             ValidationCode.UNKNOWN_PROVINCE,
                             f"province {spec.province_code!r} is not registered"))
    if spec.tax_year not in ctx.known_years:
        out.append(error(ref, Family.STRUCTURE, ValidationCode.UNKNOWN_TAX_YEAR,
                         f"tax year {spec.tax_year} is not a seeded reference year"))
    registered = ctx.rule_identities.get(spec.rule_code)
    if registered is not None:
        declared = (spec.jurisdiction_code, spec.category, spec.subcategory,
                    spec.province_code, spec.name)
        if tuple(registered) != declared:
            out.append(error(
                ref, Family.STRUCTURE, ValidationCode.RULE_CODE_CONFLICT,
                f"rule code {spec.rule_code!r} is already registered as "
                f"{registered}, but this draft declares {declared}. The code is "
                "globally unique, so the draft would silently inherit the "
                "registered identity and publish as something it does not say"))
    if not spec.eligibility_basis_codes:
        out.append(error(
            ref, Family.STRUCTURE, ValidationCode.MISSING_REQUIRED_FIELD,
            "eligibility_basis_codes is empty; the evaluator reports an "
            "opportunity with no basis codes as 'indeterminate', so publishing "
            "one would ship a rule that can never say a user is eligible"))
    out.extend(_effective_period(spec))
    return out


def _effective_period(spec: TaxKnowledgeDraftSpec) -> list[Finding]:
    """Publication timestamp, effective period and tax year are three things.

    The evaluator resolves the current rule set by `(tax_year, status)` and
    applies no date predicate, so within one tax year the effective period does
    not narrow anything. Publishing a version that only becomes effective partway
    through its own year would therefore make it current IMMEDIATELY — the exact
    failure §36 forbids. The period is refused here rather than published into a
    resolver that cannot honour it.

    Future-effective publication is still fully supported at the granularity the
    resolver actually has: a 2027 version publishes today and is invisible to
    every 2026 evaluation, because tax year is the dimension it selects on.
    """
    ref = spec.rule_code
    out: list[Finding] = []
    if spec.effective_to is not None and spec.effective_to < spec.effective_from:
        out.append(error(
            ref, Family.VERSIONING, ValidationCode.INVALID_EFFECTIVE_PERIOD,
            f"effective_to {spec.effective_to} precedes effective_from "
            f"{spec.effective_from}"))
        return out

    year_start, year_end = date(spec.tax_year, 1, 1), date(spec.tax_year, 12, 31)
    if spec.effective_from > year_start:
        out.append(error(
            ref, Family.VERSIONING,
            ValidationCode.INTRA_YEAR_EFFECTIVE_SCOPE_UNSUPPORTED,
            f"effective_from {spec.effective_from} is after the start of tax "
            f"year {spec.tax_year}. The rules evaluator selects on (tax_year, "
            "status) and applies no date predicate, so this version would take "
            "effect the moment it published rather than on the date authored"))
    if spec.effective_from < year_start or (
            spec.effective_to is not None and spec.effective_to > year_end):
        out.append(error(
            ref, Family.VERSIONING,
            ValidationCode.EFFECTIVE_PERIOD_OUTSIDE_TAX_YEAR,
            f"effective period falls outside tax year {spec.tax_year}"))
    return out


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------
def validate_conditions(spec: TaxKnowledgeDraftSpec,
                        ctx: KnowledgeContext) -> list[Finding]:
    ref = spec.rule_code
    out: list[Finding] = []
    if spec.condition_group is None:
        return out

    for group in spec.condition_group.walk_groups():
        if group.logical_op not in LOGICAL_OPS:
            out.append(error(
                ref, Family.CONDITIONS, ValidationCode.UNKNOWN_LOGICAL_OPERATOR,
                f"logical operator {group.logical_op!r} is not one of "
                f"{sorted(LOGICAL_OPS)}"))

    seen: set[tuple] = set()
    for i, cond in enumerate(spec.condition_group.walk()):
        where = f"condition {i} ({cond.fact_key} {cond.operator})"
        if cond.fact_key not in ctx.known_facts:
            out.append(error(
                ref, Family.CONDITIONS, ValidationCode.UNKNOWN_CONDITION_FIELD,
                f"{where}: fact {cond.fact_key!r} is not defined; the engine "
                "would evaluate it as absent rather than fail"))
        if cond.operator not in ctx.known_operators:
            out.append(error(ref, Family.CONDITIONS,
                             ValidationCode.UNKNOWN_OPERATOR,
                             f"{where}: operator is not a registered operator"))
        if cond.value_type not in VALUE_TYPES:
            out.append(error(ref, Family.CONDITIONS,
                             ValidationCode.UNKNOWN_VALUE_TYPE,
                             f"{where}: value_type {cond.value_type!r} is unknown"))
        out.extend(_operand(ref, where, cond))

        identity = (cond.fact_key, cond.operator, cond.value_type,
                    cond.value_number, cond.value_number_high, cond.value_text,
                    cond.value_boolean, cond.value_date, cond.value_set)
        if identity in seen:
            out.append(error(ref, Family.CONDITIONS,
                             ValidationCode.DUPLICATE_CONDITION,
                             f"{where}: the identical condition appears twice"))
        seen.add(identity)
    return out


def _operand(ref: str, where: str, cond: ConditionSpec) -> list[Finding]:
    """The operand a comparison needs must be the operand it was given."""
    out: list[Finding] = []
    op = cond.operator
    if op in _NUMERIC_OPERATORS and cond.value_number is None:
        out.append(error(ref, Family.CONDITIONS,
                         ValidationCode.CONDITION_OPERAND_MISSING,
                         f"{where}: a numeric comparison needs value_number"))
    if op == "between" and cond.value_number_high is None:
        out.append(error(ref, Family.CONDITIONS,
                         ValidationCode.CONDITION_OPERAND_MISSING,
                         f"{where}: 'between' needs value_number_high"))
    if (op == "between" and cond.value_number is not None
            and cond.value_number_high is not None
            and cond.value_number_high < cond.value_number):
        out.append(error(ref, Family.CONDITIONS,
                         ValidationCode.OPERAND_TYPE_MISMATCH,
                         f"{where}: 'between' bounds are inverted"))
    if op in _SET_OPERATORS and not cond.value_set:
        out.append(error(ref, Family.CONDITIONS,
                         ValidationCode.CONDITION_OPERAND_MISSING,
                         f"{where}: a set comparison needs value_set"))
    if op in _NULLARY_OPERATORS and any(
            v is not None for v in (cond.value_number, cond.value_text,
                                    cond.value_date)):
        out.append(error(ref, Family.CONDITIONS,
                         ValidationCode.OPERAND_TYPE_MISMATCH,
                         f"{where}: {op!r} takes no operand but one was supplied"))
    # The declared type and the operand actually present must agree, or the
    # engine compares a number against a string and silently never matches.
    expected = {
        "money": cond.value_number, "percent": cond.value_number,
        "number": cond.value_number, "text": cond.value_text,
        "boolean": cond.value_boolean, "date": cond.value_date,
    }.get(cond.value_type)
    if (cond.value_type in ("money", "percent", "number", "text", "boolean", "date")
            and op not in _NULLARY_OPERATORS and expected is None):
        out.append(error(ref, Family.CONDITIONS,
                         ValidationCode.OPERAND_TYPE_MISMATCH,
                         f"{where}: value_type {cond.value_type!r} but no "
                         "operand of that type was supplied"))
    if cond.value_type == "set" and not cond.value_set:
        out.append(error(ref, Family.CONDITIONS,
                         ValidationCode.CONDITION_OPERAND_MISSING,
                         f"{where}: value_type 'set' needs value_set"))
    return out


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def validate_dependencies(spec: TaxKnowledgeDraftSpec,
                          ctx: KnowledgeContext) -> list[Finding]:
    ref = spec.rule_code
    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for dep in spec.dependencies:
        target = dep.depends_on_rule_code
        if dep.dependency_type not in DEPENDENCY_TYPES:
            out.append(error(
                ref, Family.DEPENDENCIES, ValidationCode.UNKNOWN_DEPENDENCY_KIND,
                f"dependency kind {dep.dependency_type!r} is not one of "
                f"{sorted(DEPENDENCY_TYPES)}"))
        if target == spec.rule_code:
            out.append(error(ref, Family.DEPENDENCIES,
                             ValidationCode.SELF_DEPENDENCY,
                             "a rule depends on itself"))
        elif target not in ctx.known_rule_codes:
            out.append(error(
                ref, Family.DEPENDENCIES,
                ValidationCode.UNKNOWN_DEPENDENCY_TARGET,
                f"dependency target {target!r} is neither published nor part "
                "of this pack"))
        else:
            other = ctx.rule_jurisdictions.get(target)
            if (other is not None and other != spec.jurisdiction_code
                    and "FED" not in (other, spec.jurisdiction_code)):
                out.append(error(
                    ref, Family.DEPENDENCIES,
                    ValidationCode.CROSS_JURISDICTION_DEPENDENCY,
                    f"a {spec.jurisdiction_code} rule depends on {target!r} in "
                    f"{other}; one province's rule cannot condition on "
                    "another's"))
        key = (target, dep.dependency_type)
        if key in seen:
            out.append(error(ref, Family.DEPENDENCIES,
                             ValidationCode.DUPLICATE_DEPENDENCY,
                             f"dependency {key} is declared twice"))
        seen.add(key)
    return out


def detect_dependency_cycles(
        specs: Sequence[TaxKnowledgeDraftSpec],
        existing: Mapping[str, Iterable[str]] | None = None) -> list[Finding]:
    """Find cycles in the `requires` graph across a whole pack.

    Only `requires` is walked. `precedes` orders application and `excludes` /
    `substitutes` describe mutual incompatibility — a two-way `excludes` is a
    correct statement about two rules, not a cycle, so treating every edge kind
    alike would reject valid knowledge.

    Detected explicitly rather than left for recursion to blow the stack: a
    RecursionError names no rule, and an operator cannot fix what it will not
    name.
    """
    graph: dict[str, set[str]] = {
        code: set(targets) for code, targets in (existing or {}).items()}
    for spec in specs:
        graph.setdefault(spec.rule_code, set()).update(
            d.depends_on_rule_code for d in spec.dependencies
            if d.dependency_type == "requires")

    out: list[Finding] = []
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(graph, WHITE)
    reported: set[frozenset[str]] = set()

    def visit(node: str, path: list[str]) -> None:
        colour[node] = GREY
        path.append(node)
        for nxt in sorted(graph.get(node, ())):
            if colour.get(nxt, WHITE) == GREY:
                cycle = path[path.index(nxt):]
                signature = frozenset(cycle)
                if signature not in reported:
                    reported.add(signature)
                    out.append(error(
                        cycle[0], Family.DEPENDENCIES,
                        ValidationCode.DEPENDENCY_CYCLE,
                        "requires-cycle: " + " → ".join([*cycle, nxt])))
            elif colour.get(nxt, WHITE) == WHITE and nxt in graph:
                visit(nxt, path)
        path.pop()
        colour[node] = BLACK

    for node in sorted(graph):
        if colour.get(node, WHITE) == WHITE:
            visit(node, [])
    return out


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
def validate_outcomes(spec: TaxKnowledgeDraftSpec,
                      ctx: KnowledgeContext) -> list[Finding]:
    """Validate each outcome against what its kind requires.

    Note what is NOT here: a limit of one `recommend` outcome. A rule version
    emitting several outcomes is what the schema models and what the evaluator
    implements, and the crash that made this look like a cardinality problem was
    a collision in the IOE's semantic candidate key — fixed there. Restricting
    cardinality would not have fixed it either: two outcomes of DIFFERENT kinds
    collided identically.
    """
    ref = spec.rule_code
    out: list[Finding] = []
    if not spec.outcomes:
        out.append(error(ref, Family.OUTCOMES, ValidationCode.OUTCOME_REQUIRED,
                         "a rule with no outcome can never produce anything"))
        return out

    seen: set[tuple] = set()
    for i, outcome in enumerate(spec.outcomes):
        where = f"outcome {i} ({outcome.outcome_type})"
        if outcome.outcome_type not in OUTCOME_TYPES:
            out.append(error(ref, Family.OUTCOMES,
                             ValidationCode.UNKNOWN_OUTCOME_TYPE,
                             f"{where}: not one of {sorted(OUTCOME_TYPES)}"))
        if outcome.economic_effect_type is not None and \
                outcome.economic_effect_type not in ECONOMIC_EFFECT_TYPES:
            out.append(error(ref, Family.OUTCOMES,
                             ValidationCode.UNKNOWN_ECONOMIC_EFFECT_TYPE,
                             f"{where}: economic effect "
                             f"{outcome.economic_effect_type!r} is unknown"))
        if outcome.reversibility is not None and \
                outcome.reversibility not in REVERSIBILITY:
            out.append(error(ref, Family.OUTCOMES,
                             ValidationCode.UNKNOWN_REVERSIBILITY,
                             f"{where}: reversibility "
                             f"{outcome.reversibility!r} is unknown"))
        if outcome.outcome_type == "recommend" and not outcome.title_template:
            out.append(error(ref, Family.OUTCOMES,
                             ValidationCode.OUTCOME_FIELD_REQUIRED,
                             f"{where}: a recommendation needs a title_template"))
        out.extend(_lever(ref, where, outcome, ctx))
        if outcome.formula_code is not None and (
                spec.formula is None or spec.formula.code != outcome.formula_code):
            if outcome.formula_code not in ctx.published_formulas:
                out.append(error(
                    ref, Family.OUTCOMES, ValidationCode.FORMULA_REQUIRED,
                    f"{where}: names formula {outcome.formula_code!r}, which is "
                    "neither authored here nor already published"))
        if outcome.identity() in seen:
            out.append(error(
                ref, Family.OUTCOMES, ValidationCode.DUPLICATE_OUTCOME,
                f"{where}: the same economic consequence is declared twice; "
                "differing prose does not make it two outcomes"))
        seen.add(outcome.identity())
    return out


def _lever(ref: str, where: str, outcome: OutcomeSpec,
           ctx: KnowledgeContext) -> list[Finding]:
    """A lever reference must name a registered lever and supply its parameters."""
    out: list[Finding] = []
    code = outcome.portfolio_lever_code
    if code is None:
        if outcome.lever_parameters:
            out.append(error(
                ref, Family.OUTCOMES, ValidationCode.LEVER_PARAMETER_UNRESOLVABLE,
                f"{where}: lever parameters were supplied with no lever"))
        return out
    if code not in ctx.known_levers:
        out.append(error(ref, Family.OUTCOMES,
                         ValidationCode.UNKNOWN_PORTFOLIO_LEVER,
                         f"{where}: {code!r} is not a registered lever; the "
                         "optimizer would exclude the opportunity as not "
                         "portfolio-evaluable"))
        return out
    if not outcome.lever_parameters:
        out.append(error(
            ref, Family.OUTCOMES, ValidationCode.LEVER_PARAMETER_UNRESOLVABLE,
            f"{where}: lever {code!r} was named with no parameters"))
    return out


# ---------------------------------------------------------------------------
# Formula
# ---------------------------------------------------------------------------
def validate_formula(spec: TaxKnowledgeDraftSpec,
                     ctx: KnowledgeContext) -> list[Finding]:
    formula = spec.formula
    if formula is None:
        return []
    ref = spec.rule_code
    out: list[Finding] = []

    if formula.expression_lang != "rpn":
        out.append(error(
            ref, Family.FORMULA, ValidationCode.FORMULA_LANGUAGE_UNSUPPORTED,
            f"expression language {formula.expression_lang!r} is not evaluated "
            "by the engine; the only supported language is 'rpn'"))
        return out

    declared: dict[str, Decimal | None] = {}
    for inp in formula.inputs:
        if inp.fact_key is not None and inp.fact_key not in ctx.known_facts:
            out.append(error(
                ref, Family.FORMULA, ValidationCode.FORMULA_INPUT_UNKNOWN_FACT,
                f"input {inp.param_name!r} binds to fact {inp.fact_key!r}, "
                "which is not defined"))
        if inp.fact_key is None and inp.literal_value is None:
            out.append(error(
                ref, Family.FORMULA, ValidationCode.FORMULA_INPUT_UNDECLARED,
                f"input {inp.param_name!r} binds to neither a fact nor a "
                "literal, so it has no value at evaluation time"))
        declared[inp.param_name] = inp.literal_value

    out.extend(_expression(ref, formula.expression, declared))
    if formula.code in ctx.published_formulas and \
            ctx.published_formulas[formula.code] != formula.expression:
        out.append(error(
            ref, Family.FORMULA, ValidationCode.FORMULA_CONFLICTS_WITH_PUBLISHED,
            f"formula {formula.code!r} is already published with a different "
            "expression; a published formula is immutable, so a correction "
            "publishes under a new code"))
    out.extend(_vectors(ref, formula))
    return out


def _expression(ref: str, expression: str,
                declared: Mapping[str, Decimal | None]) -> list[Finding]:
    """Check an RPN expression against the ENGINE'S registered primitives.

    Tokens resolve as: a registered operation, a declared input, or an exact
    decimal literal. Nothing else. In particular an unrecognized identifier is
    an undeclared input rather than something to invent a binding for.
    """
    out: list[Finding] = []
    tokens = expression.split()
    if not tokens:
        out.append(error(ref, Family.FORMULA, ValidationCode.FORMULA_MALFORMED,
                         "the expression is empty"))
        return out

    # Simulated stack. Each entry is the literal value when statically known,
    # so `x 0 /` is caught before it reaches a user's tax figure.
    stack: list[Decimal | None] = []
    for token in tokens:
        if token in SUPPORTED_OPERATIONS:
            if len(stack) < 2:
                out.append(error(ref, Family.FORMULA,
                                 ValidationCode.FORMULA_MALFORMED,
                                 f"stack underflow at {token!r}"))
                return out
            divisor = stack.pop()
            stack.pop()
            if token == "/" and divisor == 0:
                out.append(error(
                    ref, Family.FORMULA, ValidationCode.FORMULA_DIVIDES_BY_ZERO,
                    "the expression divides by a literal zero; the evaluator "
                    "yields 0 for that rather than raising, so it would publish "
                    "as a silently wrong figure"))
            stack.append(None)
            continue
        if token in declared:
            stack.append(declared[token])
            continue
        literal = _literal(token)
        if literal is None:
            out.append(error(
                ref, Family.FORMULA, ValidationCode.FORMULA_INPUT_UNDECLARED,
                f"token {token!r} is neither a registered operation "
                f"{sorted(SUPPORTED_OPERATIONS)}, a declared input, nor a "
                "decimal literal"))
            stack.append(None)
            continue
        if not literal.is_finite():
            out.append(error(ref, Family.FORMULA,
                             ValidationCode.FORMULA_NON_DECIMAL_LITERAL,
                             f"literal {token!r} is not a finite number"))
        elif _decimal_places(literal) > _MAX_LITERAL_EXPONENT:
            out.append(error(
                ref, Family.FORMULA, ValidationCode.FORMULA_PRECISION_EXCEEDED,
                f"literal {token!r} carries more than {_MAX_LITERAL_EXPONENT} "
                "decimal places, which the canonical scales cannot store "
                "without rounding it to something else"))
        stack.append(literal)

    if len(stack) != 1:
        out.append(error(
            ref, Family.FORMULA, ValidationCode.FORMULA_MALFORMED,
            f"the expression leaves {len(stack)} values on the stack; a "
            "formula must reduce to exactly one"))
    return out


def _decimal_places(value: Decimal) -> int:
    """How many digits follow the point, for a value already proved finite.

    `as_tuple().exponent` is `'n'`, `'N'` or `'F'` for NaN and infinity, which
    is why finiteness is established before this is called.
    """
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _literal(token: str) -> Decimal | None:
    """Parse a token as an exact decimal, refusing the float spellings.

    `Decimal('nan')` and `Decimal('1e400')` both parse; neither is a tax figure,
    so they are rejected here rather than allowed to reach an evaluation.
    """
    try:
        value = Decimal(token)
    except (InvalidOperation, ValueError):
        return None
    return value


def _vectors(ref: str, formula: FormulaSpec) -> list[Finding]:
    """Run the author's own test vectors through the engine's evaluator.

    Exact Decimal equality, no tolerance: §28. Evaluated with the SAME sandbox
    the engine uses, so a vector that passes here passes in production for the
    same reason.
    """
    from app.services.tax_engine.core.formula_sandbox import evaluate_rpn

    out: list[Finding] = []
    declared = {i.param_name for i in formula.inputs}
    for vector in formula.vectors:
        unknown = sorted(set(vector.inputs) - declared)
        if unknown:
            out.append(error(
                ref, Family.EXAMPLES, ValidationCode.FORMULA_VECTOR_FAILED,
                f"vector {vector.name!r} supplies undeclared input(s) {unknown}"))
            continue
        bindings: dict[str, Decimal] = {}
        malformed = False
        for name, raw in vector.inputs.items():
            parsed = _literal(raw)
            if parsed is None or not parsed.is_finite():
                out.append(error(
                    ref, Family.EXAMPLES, ValidationCode.FORMULA_VECTOR_FAILED,
                    f"vector {vector.name!r} input {name!r} is not an exact "
                    f"decimal: {raw!r}"))
                malformed = True
                continue
            bindings[name] = parsed
        if malformed:
            continue
        for inp in formula.inputs:
            if inp.param_name not in bindings and inp.literal_value is not None:
                bindings[inp.param_name] = inp.literal_value
        missing = sorted(declared - set(bindings))
        if missing:
            out.append(error(
                ref, Family.EXAMPLES, ValidationCode.FORMULA_VECTOR_FAILED,
                f"vector {vector.name!r} leaves input(s) {missing} unbound"))
            continue
        try:
            actual = evaluate_rpn(formula.expression, bindings)
        except (ValueError, ArithmeticError) as exc:
            out.append(error(ref, Family.EXAMPLES,
                             ValidationCode.FORMULA_VECTOR_FAILED,
                             f"vector {vector.name!r} did not evaluate: {exc}"))
            continue
        if actual != vector.expected:
            out.append(error(
                ref, Family.EXAMPLES, ValidationCode.FORMULA_VECTOR_FAILED,
                f"vector {vector.name!r} expected {vector.expected} but the "
                f"engine computed {actual}"))
    return out


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------
def validate_reference_data_refs(spec: TaxKnowledgeDraftSpec,
                                 ctx: KnowledgeContext) -> list[Finding]:
    ref = spec.rule_code
    out: list[Finding] = []
    for pin in spec.reference_data_dependencies:
        if pin.kind not in ReferenceDataKind.ALL:
            out.append(error(
                ref, Family.REFERENCE_DATA,
                ValidationCode.UNKNOWN_REFERENCE_DATA_KIND,
                f"reference-data kind {pin.kind!r} is not one of "
                f"{sorted(ReferenceDataKind.ALL)}"))
            continue
        if (pin.kind, pin.key, pin.tax_year) not in ctx.published_reference_data:
            out.append(error(
                ref, Family.REFERENCE_DATA, ValidationCode.REFERENCE_DATA_MISSING,
                f"{pin.kind} {pin.key!r} for {pin.tax_year} is not published; a "
                "formula reading it would compute against nothing"))
    return out


def validate_reference_data(spec: ReferenceDataSpec,
                            ctx: KnowledgeContext) -> list[Finding]:
    """Structural validation of an authored reference-data object."""
    ref = spec.key
    out: list[Finding] = []
    if spec.kind not in ReferenceDataKind.ALL:
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.UNKNOWN_REFERENCE_DATA_KIND,
                         f"kind {spec.kind!r} is unknown"))
        return out
    if spec.tax_year not in ctx.known_years:
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.UNKNOWN_TAX_YEAR,
                         f"tax year {spec.tax_year} is not a seeded year"))
    if (spec.kind, spec.key, spec.tax_year) in ctx.published_reference_data:
        out.append(error(
            ref, Family.REFERENCE_DATA,
            ValidationCode.REFERENCE_DATA_ALREADY_PUBLISHED,
            f"{spec.kind} {spec.key!r} for {spec.tax_year} is already "
            "published. Published reference data is immutable — the tables are "
            "unique on their semantic key and carry no version column, so a "
            "correction would rewrite what sealed snapshots were computed from"))

    if spec.kind == ReferenceDataKind.TAX_BRACKET_SET:
        out.extend(_brackets(spec, ctx))
    elif spec.kind == ReferenceDataKind.CONTRIBUTION_LIMIT:
        if spec.key not in ctx.known_registered_types:
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.UNKNOWN_REGISTERED_TYPE,
                             f"registered type {spec.key!r} is not registered"))
        if spec.percent_of_income is not None and not (
                Decimal(0) <= spec.percent_of_income <= Decimal(1)):
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.RATE_OUT_OF_RANGE,
                             f"percent_of_income {spec.percent_of_income} is "
                             "outside 0..1"))
        if spec.annual_limit is None and spec.lifetime_limit is None and \
                spec.percent_of_income is None:
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.MISSING_REQUIRED_FIELD,
                             "a contribution limit that limits nothing"))
    elif spec.kind == ReferenceDataKind.CALC_CONSTANT:
        if spec.value is None:
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.MISSING_REQUIRED_FIELD,
                             "a constant with no value"))
    return out


def _brackets(spec: ReferenceDataSpec, ctx: KnowledgeContext) -> list[Finding]:
    """A bracket table must partition income, not merely list intervals."""
    ref = spec.key
    out: list[Finding] = []
    if spec.jurisdiction_code is None:
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.MISSING_REQUIRED_FIELD,
                         "a bracket set must name its jurisdiction"))
    elif spec.jurisdiction_code not in ctx.known_jurisdictions:
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.UNKNOWN_JURISDICTION,
                         f"jurisdiction {spec.jurisdiction_code!r} is unknown"))
    if not spec.brackets:
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.TERMINAL_BRACKET_MISSING,
                         "a bracket set with no brackets"))
        return out

    ordinals = [b.ordinal for b in spec.brackets]
    if ordinals != sorted(ordinals) or len(set(ordinals)) != len(ordinals):
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.BRACKETS_NOT_ORDERED,
                         f"bracket ordinals {ordinals} are not strictly "
                         "increasing"))
    for bracket in spec.brackets:
        if not (Decimal(0) <= bracket.rate <= Decimal(1)):
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.RATE_OUT_OF_RANGE,
                             f"bracket {bracket.ordinal} rate {bracket.rate} is "
                             "outside 0..1"))
        if bracket.upper_bound is not None and \
                bracket.upper_bound <= bracket.lower_bound:
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.BRACKETS_NOT_ORDERED,
                             f"bracket {bracket.ordinal} ends at or before it "
                             "begins"))

    ordered = sorted(spec.brackets, key=lambda b: b.ordinal)
    if ordered[0].lower_bound != Decimal(0):
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.BRACKETS_NOT_CONTIGUOUS,
                         f"the first bracket starts at {ordered[0].lower_bound} "
                         "rather than 0, so income below it is untaxed by "
                         "omission rather than by rule"))
    for lower, upper in zip(ordered, ordered[1:], strict=False):
        if lower.upper_bound is None:
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.TERMINAL_BRACKET_MISSING,
                             f"bracket {lower.ordinal} is open-ended but is not "
                             "the last one"))
        elif lower.upper_bound > upper.lower_bound:
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.BRACKETS_OVERLAP,
                             f"brackets {lower.ordinal} and {upper.ordinal} "
                             "overlap"))
        elif lower.upper_bound < upper.lower_bound:
            out.append(error(ref, Family.REFERENCE_DATA,
                             ValidationCode.BRACKETS_NOT_CONTIGUOUS,
                             f"income between {lower.upper_bound} and "
                             f"{upper.lower_bound} falls in no bracket"))
    if ordered[-1].upper_bound is not None:
        out.append(error(ref, Family.REFERENCE_DATA,
                         ValidationCode.TERMINAL_BRACKET_MISSING,
                         "the top bracket is bounded, so the highest incomes "
                         "fall outside every bracket"))
    return out


# ---------------------------------------------------------------------------
# Evidence, deadlines, assumptions, examples
# ---------------------------------------------------------------------------
def validate_evidence(spec: TaxKnowledgeDraftSpec,
                      ctx: KnowledgeContext) -> list[Finding]:
    ref = spec.rule_code
    out: list[Finding] = []
    seen: set[str] = set()
    for req in spec.evidence_requirements:
        if req.document_type_code not in ctx.known_document_types:
            out.append(error(
                ref, Family.EVIDENCE, ValidationCode.EVIDENCE_TYPE_UNKNOWN,
                f"document type {req.document_type_code!r} is not in the "
                "governed taxonomy"))
        if req.necessity not in EVIDENCE_NECESSITY:
            out.append(error(ref, Family.EVIDENCE,
                             ValidationCode.UNKNOWN_EVIDENCE_NECESSITY,
                             f"necessity {req.necessity!r} is unknown"))
        if req.document_type_code in seen:
            out.append(error(ref, Family.EVIDENCE,
                             ValidationCode.DUPLICATE_EVIDENCE,
                             f"document type {req.document_type_code!r} is "
                             "required twice"))
        seen.add(req.document_type_code)
    return out


def validate_deadlines(spec: TaxKnowledgeDraftSpec,
                       ctx: KnowledgeContext) -> list[Finding]:
    ref = spec.rule_code
    out: list[Finding] = []
    seen: set[str] = set()
    declared_deadlines = {d.deadline_code for d in spec.deadlines}
    for deadline in spec.deadlines:
        if deadline.deadline_code not in ctx.known_deadline_codes:
            out.append(error(
                ref, Family.DEADLINES, ValidationCode.DEADLINE_CODE_UNKNOWN,
                f"deadline code {deadline.deadline_code!r} is not registered in "
                "ref.deadline_type"))
        if deadline.jurisdiction_code is not None and \
                deadline.jurisdiction_code not in ctx.known_jurisdictions:
            out.append(error(ref, Family.DEADLINES,
                             ValidationCode.UNKNOWN_JURISDICTION,
                             f"deadline names jurisdiction "
                             f"{deadline.jurisdiction_code!r}, which is unknown"))
        if deadline.deadline_date is None:
            out.append(error(
                ref, Family.DEADLINES, ValidationCode.DEADLINE_INVALID,
                f"deadline {deadline.deadline_code!r} carries no date. The "
                "schema has no date-formula representation, so an undated "
                "deadline is one nothing can compute"))
        elif not (date(spec.tax_year, 1, 1) <= deadline.deadline_date
                  <= date(spec.tax_year + 2, 12, 31)):
            out.append(error(
                ref, Family.DEADLINES, ValidationCode.DEADLINE_INVALID,
                f"deadline {deadline.deadline_date} is not within tax year "
                f"{spec.tax_year} or the two years of filing and adjustment "
                "that follow it"))
        if deadline.deadline_code in seen:
            out.append(error(ref, Family.DEADLINES,
                             ValidationCode.DUPLICATE_DEADLINE,
                             f"deadline {deadline.deadline_code!r} twice"))
        seen.add(deadline.deadline_code)

    for action in spec.actions:
        if action.deadline_code is not None and \
                action.deadline_code not in declared_deadlines:
            out.append(error(
                ref, Family.DEADLINES, ValidationCode.DEADLINE_INVALID,
                f"action {action.action_code!r} points at deadline "
                f"{action.deadline_code!r}, which this rule does not declare"))
    return out


def validate_actions(spec: TaxKnowledgeDraftSpec) -> list[Finding]:
    ref = spec.rule_code
    out: list[Finding] = []
    for action in spec.actions:
        if action.cost_type is not None and action.cost_type not in COST_TYPES:
            out.append(error(ref, Family.STRUCTURE, ValidationCode.MALFORMED_VALUE,
                             f"action {action.action_code!r} has unknown cost "
                             f"type {action.cost_type!r}"))
        if not 1 <= action.effort_rating <= 5:
            out.append(error(ref, Family.STRUCTURE, ValidationCode.MALFORMED_VALUE,
                             f"action {action.action_code!r} effort rating "
                             f"{action.effort_rating} is outside 1..5"))
        if action.cost_amount is not None and action.cost_amount < 0:
            out.append(error(ref, Family.STRUCTURE, ValidationCode.MALFORMED_VALUE,
                             f"action {action.action_code!r} has a negative cost"))
    for resource in spec.shared_resources:
        if resource.pool_scope not in POOL_SCOPES:
            out.append(error(ref, Family.STRUCTURE, ValidationCode.MALFORMED_VALUE,
                             f"shared resource {resource.resource_code!r} has "
                             f"unknown pool scope {resource.pool_scope!r}"))
    return out


def validate_assumptions(spec: TaxKnowledgeDraftSpec,
                         ctx: KnowledgeContext) -> list[Finding]:
    """Every declared assumption must name something the platform knows.

    Fails closed on an empty vocabulary. If the platform has no assumption codes
    at all, a draft declaring one is declaring something nobody can satisfy —
    reporting that as fine would be inferring authority from absence.
    """
    ref = spec.rule_code
    out: list[Finding] = []
    for code in spec.assumption_codes:
        if code not in ctx.known_assumption_codes:
            out.append(error(
                ref, Family.ASSUMPTIONS, ValidationCode.ASSUMPTION_CODE_UNKNOWN,
                f"assumption {code!r} is not a known platform assumption, so "
                "nothing could ever report it satisfied"))
    return out


def validate_examples(spec: TaxKnowledgeDraftSpec,
                      ctx: KnowledgeContext) -> list[Finding]:
    """A production rule publishes with evidence that it does what it says.

    One positive and one negative case, minimum. Not an arbitrary count: a rule
    with only positive cases has never been shown to EXCLUDE anybody, and the
    gate is half the rule.
    """
    ref = spec.rule_code
    out: list[Finding] = []
    if not spec.examples:
        out.append(error(
            ref, Family.EXAMPLES, ValidationCode.EXAMPLES_REQUIRED,
            "a production rule publishes with executable examples; without one "
            "nothing has ever run this rule"))
        return out
    if not any(x.expect_eligible for x in spec.examples):
        out.append(error(ref, Family.EXAMPLES,
                         ValidationCode.POSITIVE_EXAMPLE_REQUIRED,
                         "no example expects eligibility"))
    if not any(not x.expect_eligible for x in spec.examples):
        out.append(error(
            ref, Family.EXAMPLES, ValidationCode.NEGATIVE_EXAMPLE_REQUIRED,
            "no example expects ineligibility, so the gate has never been shown "
            "to exclude anybody"))
    for example in spec.examples:
        unknown = sorted(set(example.facts) - ctx.known_facts)
        if unknown:
            out.append(error(ref, Family.EXAMPLES,
                             ValidationCode.EXAMPLE_FACT_UNKNOWN,
                             f"example {example.name!r} supplies undefined "
                             f"fact(s) {unknown}"))
        unknown_types = sorted(
            set(example.expected_outcome_types) - OUTCOME_TYPES)
        if unknown_types:
            out.append(error(ref, Family.EXAMPLES,
                             ValidationCode.EXAMPLE_FAILED,
                             f"example {example.name!r} expects unknown outcome "
                             f"type(s) {unknown_types}"))
    return out


def run_examples(spec: TaxKnowledgeDraftSpec) -> list[Finding]:
    """Execute each example against the ENGINE'S condition evaluator.

    The same `eval_group` the rules evaluator calls, so a passing example passes
    for the reason production will. Examples verify authored knowledge; they are
    never consulted at runtime and never become authority.
    """
    from app.services.tax_engine.core.condition_eval import eval_group

    ref = spec.rule_code
    out: list[Finding] = []
    tree = _engine_tree(spec)
    declared_types = {o.outcome_type for o in spec.outcomes}
    for example in spec.examples:
        facts = _coerce_facts(example.facts)
        actual = True if tree is None else bool(eval_group(tree, facts))
        if actual != example.expect_eligible:
            out.append(error(
                ref, Family.EXAMPLES, ValidationCode.EXAMPLE_FAILED,
                f"example {example.name!r} expected "
                f"{'eligible' if example.expect_eligible else 'ineligible'} but "
                f"the gate evaluated {'eligible' if actual else 'ineligible'}"))
            continue
        missing = sorted(set(example.expected_outcome_types) - declared_types)
        if example.expect_eligible and missing:
            out.append(error(ref, Family.EXAMPLES, ValidationCode.EXAMPLE_FAILED,
                             f"example {example.name!r} expects outcome type(s) "
                             f"{missing} that this rule does not declare"))
    return out


def _coerce_facts(facts: Mapping[str, str]) -> dict[str, object]:
    """Turn an example's text facts into the values the evaluator compares.

    Text is the wire form because a JSON number is a float; the type a fact
    should take is inferred from the literal here and nowhere else, so an
    example's `"true"` reaches the engine as a boolean rather than a string.
    """
    out: dict[str, object] = {}
    for key, raw in facts.items():
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            out[key] = lowered == "true"
            continue
        parsed = _literal(raw)
        if parsed is not None and parsed.is_finite():
            out[key] = parsed
            continue
        try:
            out[key] = date.fromisoformat(raw)
        except ValueError:
            out[key] = raw
    return out


def _engine_tree(spec: TaxKnowledgeDraftSpec) -> dict | None:
    """Render the authored condition group into the evaluator's tree shape."""
    if spec.condition_group is None:
        return None

    def node(group: ConditionGroupSpec) -> dict:
        return {
            "logical_op": group.logical_op,
            "conditions": [
                {
                    "fact_key": cond.fact_key,
                    "operator": cond.operator,
                    "value_type": cond.value_type,
                    "value_number": cond.value_number,
                    "value_number_high": cond.value_number_high,
                    "value_text": cond.value_text,
                    "value_boolean": cond.value_boolean,
                    "value_date": cond.value_date,
                    "value_set": list(cond.value_set),
                }
                for cond in group.conditions
            ],
            "groups": [node(child) for child in group.groups],
        }

    return node(spec.condition_group)


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------
def validate_versioning(spec: TaxKnowledgeDraftSpec,
                        ctx: KnowledgeContext) -> list[Finding]:
    """Whether this draft may take its place among published versions.

    `uq_rule_version_published` permits exactly one published version per rule
    and tax year, so replacing one is supersession and must be declared. An
    undeclared replacement is not a smaller version of the same act — it is a
    caller who did not know a published version existed.
    """
    ref = spec.rule_code
    out: list[Finding] = []
    current = ctx.published_rule_versions.get((spec.rule_code, spec.tax_year))
    if current is not None and spec.supersedes_version_id is None:
        out.append(error(
            ref, Family.VERSIONING, ValidationCode.SUPERSESSION_REQUIRED,
            f"{spec.rule_code} already has a published version for "
            f"{spec.tax_year}; publishing another without naming what it "
            "supersedes would replace live authority silently"))
    elif current is None and spec.supersedes_version_id is not None:
        out.append(error(
            ref, Family.VERSIONING, ValidationCode.SUPERSESSION_TARGET_MISSING,
            "this draft declares a supersession target, but no published "
            f"version of {spec.rule_code} exists for {spec.tax_year}"))
    elif (current is not None and spec.supersedes_version_id is not None
            and str(current) != str(spec.supersedes_version_id)):
        out.append(error(
            ref, Family.VERSIONING, ValidationCode.SUPERSESSION_TARGET_INVALID,
            f"this draft declares it supersedes {spec.supersedes_version_id}, "
            f"but the published version of {spec.rule_code} for "
            f"{spec.tax_year} is {current}. Replacing live authority while "
            "naming a different predecessor is a correction aimed at the wrong "
            "version"))
    return out


def warn_unpublished_dependency(spec: TaxKnowledgeDraftSpec,
                                pack_codes: frozenset[str]) -> list[Finding]:
    """Advisory: a dependency satisfied only by an unpublished pack member.

    A warning, not an error. Publishing them together satisfies it, and pack
    publication is atomic — but an operator publishing them separately should
    see it said so.
    """
    return [
        warning(spec.rule_code, Family.DEPENDENCIES,
                ValidationCode.UNKNOWN_DEPENDENCY_TARGET,
                f"dependency {dep.depends_on_rule_code!r} is satisfied only by "
                "another member of this pack")
        for dep in spec.dependencies
        if dep.depends_on_rule_code in pack_codes
    ]
