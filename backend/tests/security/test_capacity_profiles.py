"""What a capacity profile may and may not change.

`infra/modules/capacity` exists so that "how big is this environment" is one
reviewed value. The risk it creates is the obvious one: a profile is a lever,
and a lever that can reach a security property is a way to trade a control for a
few dollars a month without anyone noticing at review time.

So these tests assert the SEPARATION. Capacity and redundancy live in the
profile. Isolation, identity, privilege, reachability and fail-closed behaviour
live in the architecture, and every one of them must hold in BOTH profiles and
in every environment root.

WHY THEY READ THE TERRAFORM RATHER THAN AN APPLIED ESTATE. There is no AWS
account yet. A test that can only run after an apply is a test that does not run
before the apply that would have needed it. Each one is written against the text
that Terraform will act on, and `test_the_guards_are_not_vacuous` breaks each
property on a copy of the tree and requires the corresponding assertion to fail.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INFRA = REPO / "infra"
CAPACITY = INFRA / "modules" / "capacity" / "main.tf"
ENV_ROOTS = ("staging", "production")


def _tf(*parts: str) -> str:
    return (INFRA.joinpath(*parts)).read_text()


def _all_tf(root: Path | None = None) -> dict[Path, str]:
    base = root or INFRA
    return {p: p.read_text() for p in sorted(base.rglob("*.tf"))}


def _strip_comments(text: str) -> str:
    """Prose about a property is not the property.

    Every assertion below is about configuration, and this file's Terraform is
    heavily commented — including comments that quote the very strings being
    searched for. Matching those would make a test pass because someone
    described the control rather than because they configured it.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        out.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(out)


def _block(text: str, kind: str, *labels: str) -> str:
    """Return the body of one `kind "label" ... { ... }` block, braces matched."""
    header = re.compile(
        r'^\s*' + kind + r'\s+' + r'\s+'.join(f'"{re.escape(x)}"' for x in labels) + r'\s*\{',
        re.M,
    )
    match = header.search(text)
    if match is None:
        return ""
    depth, start = 0, match.end() - 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return ""


def _profile_body(text: str, profile: str) -> str:
    """The body of one profile inside the `profiles = { ... }` map."""
    match = re.search(rf"^\s*{re.escape(profile)}\s*=\s*\{{", text, re.M)
    if match is None:
        return ""
    depth, start = 0, match.end() - 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return ""


# ---------------------------------------------------------------------------
# 1. The production database is not reachable from the internet
# ---------------------------------------------------------------------------
def test_the_production_database_is_never_publicly_accessible() -> None:
    """`publicly_accessible` is false, and no profile can reach it.

    The second half is the point. `test_deployment_surface.py` already asserts
    the literal; this asserts that the literal cannot become a variable, which
    is how a capacity lever would acquire the ability to expose a database.
    """
    database = _strip_comments(_tf("modules", "database", "main.tf"))
    instance = _block(database, "resource", "aws_db_instance", "this")
    assert instance, "aws_db_instance.this not found"

    assignment = re.search(r"publicly_accessible\s*=\s*(\S+)", instance)
    assert assignment, "publicly_accessible is not set at all"
    assert assignment.group(1) == "false", (
        f"publicly_accessible is {assignment.group(1)!r}; it must be the literal "
        "false so that no variable, and therefore no capacity profile, can change it"
    )

    capacity = _strip_comments(CAPACITY.read_text())
    assert "publicly_accessible" not in capacity, (
        "the capacity module mentions publicly_accessible; capacity may not "
        "reach network exposure"
    )


# ---------------------------------------------------------------------------
# 2. The data subnet has no route to the internet
# ---------------------------------------------------------------------------
def test_the_data_subnet_route_table_has_no_default_route() -> None:
    """Isolation by absence of a route, in both profiles.

    `single_nat_gateway` is a capacity knob — one NAT or one per zone. What it
    must never do is put a default route on the tier that holds the database and
    the cache. That tier reaches nothing, which is why a stolen credential in a
    task cannot be used to exfiltrate from the data tier directly.
    """
    network = _strip_comments(_tf("modules", "network", "main.tf"))

    data_routes = [
        name
        for name in re.findall(r'resource\s+"aws_route"\s+"([^"]+)"', network)
        if "data" in name
    ]
    assert not data_routes, f"the data tier has explicit routes: {data_routes}"

    # Every aws_route in the module, and which route table it attaches to.
    for match in re.finditer(r'resource\s+"aws_route"\s+"([^"]+)"\s*\{', network):
        body = _block(network, "resource", "aws_route", match.group(1))
        table = re.search(r"route_table_id\s*=\s*([^\n]+)", body)
        assert table, f"aws_route.{match.group(1)} names no route table"
        assert "data" not in table.group(1), (
            f"aws_route.{match.group(1)} puts a route on the data route table: "
            f"{table.group(1).strip()}"
        )

    # And the table itself must exist, or the assertion above is about nothing.
    assert re.search(r'resource\s+"aws_route_table"\s+"data"', network), (
        "there is no data route table to be isolated"
    )


# ---------------------------------------------------------------------------
# 3. The privacy and freshness identities stay separate
# ---------------------------------------------------------------------------
def test_privacy_and_freshness_run_as_their_own_database_identities() -> None:
    """No profile may collapse the privileged workers or share their identity.

    Three distinct services, three distinct task roles, three distinct database
    secrets. The privacy worker is the only identity in the estate that can
    destroy customer data, and the freshness worker is the only one that can
    write the integrity outbox. Collapsing them to save a Fargate task would
    hand the application identity both capabilities.
    """
    services = _strip_comments(_tf("modules", "compute", "services.tf"))
    workers = _block(services, "locals") or services

    for name, secret in (
        ("worker-app", "api"),
        ("worker-freshness", "freshness"),
        ("worker-privacy", "privacy"),
    ):
        entry = re.search(
            rf'"{re.escape(name)}"\s*=\s*\{{(.*?)\n\s*\}}', workers, re.S
        )
        assert entry, f"{name} is not defined in modules/compute/services.tf"
        db_secret = re.search(r'db_secret\s*=\s*"([^"]+)"', entry.group(1))
        assert db_secret and db_secret.group(1) == secret, (
            f"{name} runs as {db_secret.group(1) if db_secret else None!r}, "
            f"expected {secret!r}"
        )

    iam = _strip_comments(_tf("modules", "compute", "iam.tf"))
    roles = re.search(r"task_roles\s*=\s*\[([^\]]+)\]", iam)
    assert roles, "the task role list is not declared"
    named = set(re.findall(r'"([^"]+)"', roles.group(1)))
    assert {"worker-app", "worker-freshness", "worker-privacy"} <= named, (
        f"a privileged worker has no role of its own: {sorted(named)}"
    )

    capacity = _strip_comments(CAPACITY.read_text())
    assert "db_secret" not in capacity and "task_role" not in capacity, (
        "the capacity module reaches database identity; it may only size things"
    )

    # A profile may set a worker's COUNT, but never to zero: a privileged queue
    # with no consumer is a deletion that silently never happens.
    for profile in ("lean_launch", "high_availability"):
        body = _profile_body(capacity, profile)
        assert body, f"profile {profile} is missing"
        for knob in (
            "worker_app_count",
            "worker_freshness_count",
            "worker_privacy_count",
        ):
            value = re.search(rf"{knob}\s*=\s*(\d+)", body)
            assert value, f"{profile} does not set {knob}"
            assert int(value.group(1)) >= 1, (
                f"{profile} runs {int(value.group(1))} {knob.replace('_count', '')} "
                "tasks; a privileged queue must always have a consumer"
            )


# ---------------------------------------------------------------------------
# 4. Lean cannot autoscale past its hard maximum
# ---------------------------------------------------------------------------
def test_the_lean_profile_cannot_autoscale_beyond_its_hard_maximum() -> None:
    """Two ceilings, and both are enforced by the plan rather than by review.

    `api_max_count` is what autoscaling may reach. `api_absolute_max_count` is
    what the module will accept at all, and the precondition on the autoscaling
    target refuses to plan if a profile asks for more. There is no autoscaling
    on any worker or on beat, so nothing else has a runaway path.
    """
    capacity = _strip_comments(CAPACITY.read_text())
    lean = _profile_body(capacity, "lean_launch")

    desired = int(re.search(r"api_count\s*=\s*(\d+)", lean).group(1))
    maximum = int(re.search(r"api_max_count\s*=\s*(\d+)", lean).group(1))
    assert desired == 1, f"lean runs {desired} API tasks, expected 1"
    assert maximum == 2, f"lean allows {maximum} API tasks, expected 2"

    services = _strip_comments(_tf("modules", "compute", "services.tf"))
    target = _block(services, "resource", "aws_appautoscaling_target", "api")
    assert target, "there is no autoscaling target to be capped"
    assert "api_absolute_max_count" in target, (
        "the autoscaling target has no absolute ceiling; a mistyped maximum "
        "would become a bill rather than a failed plan"
    )
    assert "precondition" in target, "the ceiling is declared but not enforced"

    # Nothing else scales. A second autoscaling target on a worker would be a
    # second runaway path that this test would otherwise not see.
    targets = set(
        re.findall(r'resource\s+"aws_appautoscaling_target"\s+"([^"]+)"', services)
    )
    assert targets == {"api"}, f"unexpected autoscaling targets: {sorted(targets)}"


# ---------------------------------------------------------------------------
# 5. The high-availability profile still exists
# ---------------------------------------------------------------------------
def test_the_high_availability_profile_is_preserved_and_complete() -> None:
    """Lean is a posture, not an amputation.

    The entry that introduced lean_launch required that high_availability be
    preserved rather than deleted, so that scaling up is the same one-line
    change in the other direction. "Preserved" means every knob the lean profile
    sets is also set here — a profile missing a key does not fail at review, it
    fails at plan time in whichever environment switched to it.
    """
    capacity = _strip_comments(CAPACITY.read_text())
    lean = _profile_body(capacity, "lean_launch")
    high = _profile_body(capacity, "high_availability")
    assert lean and high, "a profile is missing entirely"

    def knobs(body: str) -> set[str]:
        return set(re.findall(r"^\s*([a-z_]+)\s*=", body, re.M))

    missing = knobs(lean) - knobs(high)
    assert not missing, f"high_availability is missing: {sorted(missing)}"

    # And it must actually be more available, or it is lean under another name.
    assert re.search(r"db_multi_az\s*=\s*true", high), "HA is not multi-AZ"
    assert re.search(r"single_nat_gateway\s*=\s*false", high), "HA has one NAT"
    assert int(re.search(r"cache_node_count\s*=\s*(\d+)", high).group(1)) >= 2, (
        "HA runs a single cache node"
    )
    assert int(re.search(r"api_count\s*=\s*(\d+)", high).group(1)) >= 2, (
        "HA runs a single API task"
    )


# ---------------------------------------------------------------------------
# 6. Production still fails closed on legal acceptance
# ---------------------------------------------------------------------------
def test_production_still_fails_closed_and_staging_still_does_not() -> None:
    """`ONYX_ENVIRONMENT` is what makes the production validators fail closed.

    Staging is now ephemeral and cheap, which makes it tempting to point a lean
    production at the staging settings and save the difference. This asserts the
    two roots still declare different environments, and that neither the
    capacity module nor a profile can reach that value.
    """
    for env in ENV_ROOTS:
        root = _strip_comments(_tf("envs", env, "main.tf"))
        declared = re.search(r'environment\s*=\s*"([^"]+)"', root)
        assert declared, f"envs/{env} does not declare an environment name"
        assert declared.group(1) == env, (
            f"envs/{env} declares environment {declared.group(1)!r}"
        )

    compute = _strip_comments(_tf("modules", "compute", "main.tf"))
    assert 'name = "ONYX_ENVIRONMENT", value = var.environment' in compute.replace(
        '{ name', 'name'
    ).replace("  ", " ") or "ONYX_ENVIRONMENT" in compute, (
        "tasks are not told which environment they are in"
    )

    capacity = _strip_comments(CAPACITY.read_text())
    assert "ONYX_ENVIRONMENT" not in capacity and "environment" not in capacity, (
        "the capacity module can reach the environment name, and therefore the "
        "production fail-closed validators"
    )


# ---------------------------------------------------------------------------
# 7. The load balancer refuses anything that did not come through CloudFront
# ---------------------------------------------------------------------------
def test_the_load_balancer_refuses_requests_without_the_origin_header() -> None:
    """The control that replaced the regional WAF, asserted directly.

    The listener's DEFAULT action must not reach a target group, and the only
    rule that does must be conditioned on the secret header. This is the
    property the removed web ACL held; removing an enforcement point without a
    test for its replacement is how a saving becomes a hole.
    """
    compute = _strip_comments(_tf("modules", "compute", "main.tf"))

    listener = _block(compute, "resource", "aws_lb_listener", "https")
    assert listener, "there is no HTTPS listener"
    default = _block(listener, "default_action") or listener[listener.index("default_action"):]
    assert "target_group_arn" not in default, (
        "the listener's default action forwards to a target group; a request "
        "without the origin header would reach the application"
    )
    assert re.search(r'status_code\s*=\s*"403"', default), (
        "the default action does not refuse with 403"
    )

    rule = _block(compute, "resource", "aws_lb_listener_rule", "from_our_distribution")
    assert rule, "no listener rule forwards to the API"
    assert "target_group_arn" in rule, "the forwarding rule forwards nothing"
    header = re.search(r'http_header_name\s*=\s*"([^"]+)"', rule)
    assert header and header.group(1).lower() == "x-onyx-origin-verify", (
        f"the rule matches {header.group(1) if header else None!r}, not the origin header"
    )
    assert "var.origin_verify_secret" in rule, (
        "the rule does not compare against the generated secret"
    )

    # Exactly one rule may forward. A second, unconditioned one would reopen it.
    forwarding = [
        name
        for name in re.findall(
            r'resource\s+"aws_lb_listener_rule"\s+"([^"]+)"', compute
        )
    ]
    assert forwarding == ["from_our_distribution"], (
        f"unexpected listener rules: {forwarding}"
    )

    # And the distribution must actually send it.
    edge = _strip_comments(_tf("modules", "edge", "main.tf"))
    assert "X-Onyx-Origin-Verify" in edge and "var.origin_verify_secret" in edge, (
        "CloudFront does not send the header the load balancer demands"
    )


# ---------------------------------------------------------------------------
# The connection ceiling
# ---------------------------------------------------------------------------
def test_every_profile_declares_a_database_connection_ceiling() -> None:
    """Sizing a database without counting connections is guessing.

    Measured: one worker at concurrency 2 held 97 backends while draining a
    burst of scheduled tasks, because `workers/runtime.py` disposes the engines
    after every invocation and the next one reconnects. At the application's
    default pool settings the whole estate could open 423 backends against a
    db.t4g.small's 225. The ceiling is arithmetic, so it can be computed, and
    the module refuses to plan when it exceeds what the class allows.
    """
    capacity = _strip_comments(CAPACITY.read_text())
    assert "connection_ceiling" in capacity, "no ceiling is computed"
    guard = _block(capacity, "resource", "terraform_data", "connection_ceiling_guard")
    assert guard and "precondition" in guard, "the ceiling is computed but not enforced"
    assert "connection_ceiling <= local.connection_room" in guard, (
        "the precondition does not compare the ceiling against the class limit"
    )

    for profile in ("lean_launch", "high_availability"):
        body = _profile_body(capacity, profile)
        for knob in ("api_pool_size", "api_pool_overflow", "worker_pool_size",
                     "worker_pool_overflow", "db_connection_headroom"):
            assert re.search(rf"{knob}\s*=", body), f"{profile} does not set {knob}"

    # The caps must actually reach the tasks, or the arithmetic is fiction.
    services = _strip_comments(_tf("modules", "compute", "services.tf"))
    assert "ONYX_DB_POOL_SIZE" in services and "ONYX_DB_MAX_OVERFLOW" in services, (
        "the computed pool caps are never given to a container"
    )


# ---------------------------------------------------------------------------
# The persistent tier
# ---------------------------------------------------------------------------
def test_destroying_an_environment_cannot_destroy_the_registry_or_evidence() -> None:
    """Ephemeral staging is only safe if the irreplaceable things are elsewhere."""
    shared = _strip_comments(_tf("envs", "shared", "main.tf"))
    for kind, label in (
        ("aws_ecr_repository", "backend"),
        ("aws_s3_bucket", "evidence"),
    ):
        body = _block(shared, "resource", kind, label)
        assert body, f"{kind}.{label} is not in the persistent root"
        assert "prevent_destroy = true" in body.replace("  ", " "), (
            f"{kind}.{label} has no prevent_destroy"
        )

    for env in ENV_ROOTS:
        root = _strip_comments(_tf("envs", env, "main.tf"))
        assert 'resource "aws_ecr_repository"' not in root, (
            f"envs/{env} owns a registry; destroying it would take the images"
        )
    compute = _strip_comments(_tf("modules", "compute", "main.tf"))
    assert 'resource "aws_ecr_repository"' not in compute, (
        "modules/compute still owns the registry"
    )


# ---------------------------------------------------------------------------
# Non-vacuity
# ---------------------------------------------------------------------------
BREAKAGES: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "test_the_production_database_is_never_publicly_accessible",
        "modules/database/main.tf",
        "publicly_accessible = false",
        "publicly_accessible = var.publicly_accessible",
        "a variable could expose the database",
    ),
    (
        "test_the_data_subnet_route_table_has_no_default_route",
        "modules/network/main.tf",
        'resource "aws_route_table" "data" {',
        'resource "aws_route" "data_default" {\n  route_table_id = aws_route_table.data.id\n  destination_cidr_block = "0.0.0.0/0"\n}\n\nresource "aws_route_table" "data" {',
        "a default route on the data tier",
    ),
    (
        "test_privacy_and_freshness_run_as_their_own_database_identities",
        "modules/compute/services.tf",
        'db_secret   = "privacy"',
        'db_secret   = "api"',
        "the privacy worker running as the application identity",
    ),
    (
        "test_the_lean_profile_cannot_autoscale_beyond_its_hard_maximum",
        "modules/capacity/main.tf",
        "      api_max_count       = 2",
        "      api_max_count       = 40",
        "an unbounded lean maximum",
    ),
    (
        "test_the_high_availability_profile_is_preserved_and_complete",
        "modules/capacity/main.tf",
        "      db_multi_az              = true",
        "      db_multi_az              = false",
        "a high-availability profile that is not highly available",
    ),
    (
        "test_production_still_fails_closed_and_staging_still_does_not",
        "envs/production/main.tf",
        'environment = "production"',
        'environment = "staging"',
        "production running with the staging validators",
    ),
    (
        "test_the_load_balancer_refuses_requests_without_the_origin_header",
        "modules/compute/main.tf",
        '''  default_action {
    type = "fixed-response"''',
        '''  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
    xtype = "fixed-response"''',
        "a listener that forwards without the origin header",
    ),
    (
        "test_every_profile_declares_a_database_connection_ceiling",
        "modules/capacity/main.tf",
        "      condition = local.connection_ceiling <= local.connection_room",
        "      condition = true",
        "a connection ceiling that is computed and ignored",
    ),
    (
        "test_destroying_an_environment_cannot_destroy_the_registry_or_evidence",
        "envs/shared/main.tf",
        "  lifecycle { prevent_destroy = true }\n\n  tags = local.tags\n}\n\nresource \"aws_ecr_lifecycle_policy\"",
        "  tags = local.tags\n}\n\nresource \"aws_ecr_lifecycle_policy\"",
        "a destroyable container registry",
    ),
)


@pytest.mark.parametrize(
    "test_name,relative,before,after,description",
    BREAKAGES,
    ids=[b[4] for b in BREAKAGES],
)
def test_the_guards_are_not_vacuous(
    test_name: str,
    relative: str,
    before: str,
    after: str,
    description: str,
) -> None:
    """Break the property on a copy of the tree; require the guard to notice.

    A configuration assertion that greps for a string passes for two reasons —
    the property holds, or the grep never matched anything. This tells them
    apart, once per property, by editing a throwaway copy of `infra/` and
    re-running the single test that is supposed to care.
    """
    real_infra, real_capacity = INFRA, CAPACITY
    try:
        with tempfile.TemporaryDirectory() as workspace:
            clone = Path(workspace) / "infra"
            shutil.copytree(INFRA, clone, ignore=shutil.ignore_patterns(".terraform*"))

            target = clone / relative
            text = target.read_text()
            assert before in text, (
                f"the non-vacuity fixture is stale: {relative} no longer contains\n"
                f"{before!r}\nUpdate BREAKAGES rather than deleting this case."
            )
            target.write_text(text.replace(before, after, 1))

            globals()["INFRA"] = clone
            globals()["CAPACITY"] = clone / "modules" / "capacity" / "main.tf"

            with pytest.raises(AssertionError):
                globals()[test_name]()
    finally:
        # RESTORED BEFORE THE RE-RUN, not at teardown: the check below has to
        # read the real tree, and the temporary directory no longer exists.
        globals()["INFRA"], globals()["CAPACITY"] = real_infra, real_capacity

    # The same test now passes — so the failure above was caused by the
    # breakage, and not by pointing the test at a directory that was not there.
    globals()[test_name]()
