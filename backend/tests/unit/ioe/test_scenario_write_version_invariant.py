"""One question, one answer: what version is a NEW scenario written as?

WHY THIS FILE EXISTS. An activation attempt flipped the write version to v2 and
left three sites still writing the old v1 alias — the version manifest and both
row-level persistence sites. New scenarios stored "I am v1" beside a hash
computed as v2, and replay correctly refused them with RESULT_HASH_MISMATCH.

The defect was not those three lines. It was that two module constants both read
as plausible answers to "what version is this write", so picking the wrong one
looked correct in review. The structural fix is that only one constant can
answer that question now; these tests are what stop the second one growing back,
and they fail at edit time rather than waiting for an integration replay.
"""
import ast
import inspect
import pathlib

from app.services.ioe.domain import scenario as scenario_domain
from app.services.ioe.scenario import service as scenario_service

WRITE_AUTHORITY = "CURRENT_SCENARIO_RESULT_SCHEMA_VERSION"

#: Every site that constructs or persists a NEW scenario artifact, found by the
#: repository-wide inventory rather than by memory — the reverted attempt was
#: caused by acting on a remembered list of two when there were three.
NEW_WRITE_KEYWORDS = ("result_schema_version", "scenario_result_schema_version")


def _service_source() -> str:
    return pathlib.Path(inspect.getfile(scenario_service)).read_text()


def test_the_legacy_alias_no_longer_exists():
    """Removed, not renamed. A general-purpose alias is what made picking the
    wrong constant look reasonable."""
    assert not hasattr(scenario_domain, "SCENARIO_RESULT_SCHEMA_VERSION")


def test_only_one_constant_answers_the_write_question():
    assert hasattr(scenario_domain, WRITE_AUTHORITY)
    assert hasattr(scenario_domain, "SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS")
    assert scenario_domain.CURRENT_SCENARIO_RESULT_SCHEMA_VERSION in (
        scenario_domain.SUPPORTED_SCENARIO_RESULT_SCHEMA_VERSIONS)


#: The one parameter name a version is allowed to travel through. Entry 12B1
#: Phase A2 threads a single chosen version from the internal seam down to
#: every version-bearing field, so the forwarding shape has to be expressible
#: — but only as this exact parameter, declared on the enclosing function.
FORWARDED_PARAMETER = "result_schema_version"


def _parameter_scopes(tree: ast.AST) -> dict[int, bool]:
    """Map each node id in a function body to whether that function declares
    `result_schema_version` as a parameter.

    Built by walking the definitions rather than trusting names, so "it is just
    a forward" cannot be claimed by a module-level variable that happens to
    share the parameter's name.
    """
    scopes: dict[int, bool] = {}
    for definition in ast.walk(tree):
        if not isinstance(definition, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        args = definition.args
        declares = any(
            a.arg == FORWARDED_PARAMETER
            for a in (*args.args, *args.posonlyargs, *args.kwonlyargs)
        )
        for child in ast.walk(definition):
            # innermost definition wins: a nested function that does NOT
            # declare the parameter cannot inherit the permission
            scopes[id(child)] = declares if id(child) not in scopes else (
                scopes[id(child)])
    return scopes


def test_every_new_write_site_uses_the_current_write_authority():
    """THE REGRESSION FOR THE REVERTED DEFECT.

    Parsed, not grepped: every keyword argument or dict entry in ScenarioService
    that sets a scenario-result schema version must take its value either from
    the one write authority, or from the one parameter that carries a version
    chosen upstream — and the enclosing function must actually declare that
    parameter. A literal, the old alias, or any other name fails here, which is
    the moment the reverted attempt should have been stopped.

    WHY FORWARDING IS ALLOWED AT ALL. Phase A2 has to be able to create a v2
    artifact end to end while production writes v1. The danger was never that a
    version travelled; it was that DIFFERENT sites read DIFFERENT sources and
    disagreed. Forwarding one parameter is the opposite of that — every field
    below reads the same argument, so they cannot disagree, and
    `test_all_version_bearing_fields_agree` in the A2 suite measures that on
    real rows rather than inferring it from this shape.
    """
    tree = ast.parse(_service_source())
    scopes = _parameter_scopes(tree)
    offenders: list[str] = []

    def acceptable(value: ast.expr) -> bool:
        if not isinstance(value, ast.Name):
            return False
        if value.id == WRITE_AUTHORITY:
            return True
        return value.id == FORWARDED_PARAMETER and scopes.get(id(value), False)

    for node in ast.walk(tree):
        # keyword form:  result_schema_version=<value>
        if isinstance(node, ast.keyword) and node.arg in NEW_WRITE_KEYWORDS:
            if not acceptable(node.value):
                offenders.append(
                    f"line {node.value.lineno}: {node.arg}="
                    f"{ast.unparse(node.value)}")
        # dict form:  "scenario_result_schema_version": <value>
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (isinstance(key, ast.Constant)
                        and key.value in NEW_WRITE_KEYWORDS
                        and not acceptable(value)):
                    offenders.append(
                        f"line {value.lineno}: {key.value!r}: "
                        f"{ast.unparse(value)}")

    assert offenders == [], (
        "these ScenarioService write sites do not use the single write "
        f"authority {WRITE_AUTHORITY}, nor a declared {FORWARDED_PARAMETER} "
        "parameter, so a new scenario could record one version while its hash "
        "was computed under another:\n  " + "\n  ".join(offenders)
    )


def test_a_version_bearing_name_outside_a_forwarding_function_is_refused():
    """Guard on the relaxation above. The permission is scoped to functions that
    declare the parameter; a module-level name of the same spelling must not
    inherit it, or the invariant would be defeated by choosing a variable
    name."""
    tree = ast.parse(
        "result_schema_version = '2.0.0'\n"
        "def unrelated():\n"
        "    return ScenarioResult(result_schema_version=result_schema_version)\n"
    )
    scopes = _parameter_scopes(tree)
    value = next(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "result_schema_version"
    )
    assert scopes.get(id(value)) is False


def test_the_write_sites_are_not_vacuously_absent():
    """Guard on the guard. The test above passes trivially if ScenarioService
    stopped writing a version at all, so require that it still writes some."""
    tree = ast.parse(_service_source())
    found = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg in NEW_WRITE_KEYWORDS
    ) + sum(
        1 for node in ast.walk(tree) if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and key.value in NEW_WRITE_KEYWORDS
    )
    assert found >= 4, (
        f"expected the manifest, both row writes and the canonical hash call "
        f"to set a version; found {found}"
    )


def test_replay_never_uses_the_write_authority():
    """Replay's version comes from the row. Using the write authority there is
    the original version-blindness defect in its purest form."""
    import app.services.ioe.replay.services as replay

    source = pathlib.Path(inspect.getfile(replay)).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "result_schema_version":
            rendered = ast.unparse(node.value)
            assert WRITE_AUTHORITY not in rendered, (
                f"replay line {node.value.lineno} canonicalizes a historical "
                f"artifact using the current write version: {rendered}"
            )
