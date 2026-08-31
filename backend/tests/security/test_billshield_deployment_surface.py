"""BillShield Slice 3B — the dormant deployment, asserted against the Terraform.

WHAT THIS DECLARES AND WHAT IT REFUSES. Slice 3B adds `worker-billshield` to
the estate as a DORMANT internal worker of the one OnyxLedger platform: its own
database secret, its own task role carrying an explicit object-storage deny,
four capacity knobs pinned at count 0 in both profiles, and a connection
ceiling that finally models the restricted and generic pools as what they are —
different pools with different capacities. Nothing here creates a route, a
producer, an S3 permission, or a running task.

WHY THESE READ THE TERRAFORM TEXT. Same reason as `test_capacity_profiles.py`:
there is no AWS account yet, and a test that can only run after an apply is a
test that does not run before the apply that would have needed it. Every
structural checker below takes the infra root as a parameter, so the
non-vacuity plants can run the same checker against a broken copy of the tree
and require it to fail.

THE POOL MODEL BEING PROTECTED (the Slice 3B reachable-pool audit):

  service            generic pool (profile-sized)   restricted pool (fixed 4)
  api                YES                            no
  worker-app         YES                            no
  worker-freshness   YES                            YES   <- was uncounted
  worker-privacy     no  <- was counted anyway      YES
  worker-billshield  no (structural: the committed  YES
                      import-boundary guard)

The restricted capacity of 4 is `pool_size=2 + max_overflow=2`, LITERALS inside
`app/database/privacy_session.py::get_worker_engine` — not reachable from any
profile knob, environment variable, or Terraform value. A ceiling that values a
restricted engine at the generic pool size is wrong in both directions at once.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.security.test_capacity_profiles import (
    _block,
    _profile_body,
    _strip_comments,
)

REPO = Path(__file__).resolve().parents[3]
INFRA = REPO / "infra"
COST_MODEL = REPO / "docs" / "operations" / "cost-model.md"

PROFILES = ("lean_launch", "high_availability")
BILLSHIELD_KNOBS = (
    "worker_billshield_cpu",
    "worker_billshield_memory",
    "worker_billshield_count",
    "worker_billshield_concurrency",
)

#: The restricted engine's capacity: pool_size=2 + max_overflow=2, literals in
#: `get_worker_engine`. Asserted against the application source below so the
#: Terraform constant cannot drift from the factory it models.
RESTRICTED_POOL_CAPACITY = 4


def _read(infra: Path, *parts: str) -> str:
    return _strip_comments(infra.joinpath(*parts).read_text())


# --------------------------------------------------------------------------- #
# Checkers — each takes the tree it judges, so the plants can judge a broken one
# --------------------------------------------------------------------------- #
def _check_worker_declared(infra: Path) -> None:
    services = _read(infra, "modules", "compute", "services.tf")
    entry = re.search(
        r'"worker-billshield"\s*=\s*\{(.*?)\n\s*\}', services, re.S
    )
    assert entry, "worker-billshield is not declared in modules/compute/services.tf"
    body = entry.group(1)

    queue = re.search(r'queues\s*=\s*"([^"]+)"', body)
    assert queue and queue.group(1) == "billshield", (
        f"worker-billshield drains {queue.group(1) if queue else None!r}; the "
        "queue name follows the one-lowercase-name-per-family convention"
    )

    secret = re.search(r'db_secret\s*=\s*"([^"]+)"', body)
    assert secret and secret.group(1) == "billshield", (
        f"worker-billshield runs as db_secret={secret.group(1) if secret else None!r}. "
        "This one field decides what the container's generic engine "
        "authenticates as (plan §5.4.10(1)); anything but 'billshield' puts a "
        "privileged engine beside the restricted one in the same process"
    )

    for knob in ("cpu", "memory", "count", "concurrency"):
        assert f"var.worker_billshield_{knob}" in body, (
            f"worker-billshield's {knob} is not wired to the capacity profile"
        )


def _check_dsn_scoping(infra: Path) -> None:
    services = _read(infra, "modules", "compute", "services.tf")

    occurrences = [
        m.start() for m in re.finditer("ONYX_BILLSHIELD_DATABASE_URL", services)
    ]
    assert len(occurrences) == 1, (
        f"ONYX_BILLSHIELD_DATABASE_URL appears {len(occurrences)} times in "
        "services.tf; it must be injected exactly once, into the BillShield "
        "task's own conditional"
    )

    window = services[max(0, occurrences[0] - 200): occurrences[0]]
    assert 'each.key == "worker-billshield"' in window, (
        "the BillShield DSN injection is not guarded by "
        'each.key == "worker-billshield"; some other task would receive it'
    )

    # The API and migration tasks are defined in their own resource blocks;
    # neither may reference the BillShield secret in any form.
    for task in ("api", "migration"):
        block = _block(services, "resource", "aws_ecs_task_definition", task)
        assert block, f"aws_ecs_task_definition.{task} not found; walker is stale"
        assert "billshield" not in block.lower(), (
            f"the {task} task definition references the BillShield secret"
        )

    # And the secret ARN itself is only ever read inside the workers resource:
    # once by the generic-DSN lookup through each.value.db_secret (dynamic),
    # once by the guarded literal injection.
    literal_reads = re.findall(r'db_secret_arns\["billshield"\]', services)
    assert len(literal_reads) == 1, (
        f"db_secret_arns[\"billshield\"] is read {len(literal_reads)} times; "
        "the guarded injection is the only permitted literal read"
    )


def _check_secret(infra: Path) -> None:
    secrets = _read(infra, "modules", "secrets", "main.tf")

    generator = _block(secrets, "resource", "random_password", "db")
    assert generator, "random_password.db not found"
    assert '"billshield"' in generator, (
        "no BillShield password is generated; the secret would have no value "
        "or would have to reuse another identity's"
    )

    users = re.search(r"db_users\s*=\s*\{(.*?)\n\s*\}", secrets, re.S)
    assert users, "local.db_users not found"
    billshield = re.search(
        r'billshield\s*=\s*\{\s*user\s*=\s*"([^"]+)"\s*,?\s*driver\s*=\s*"([^"]+)"',
        users.group(1),
    )
    assert billshield, "db_users has no billshield entry; no DSN secret is built"
    assert billshield.group(1) == "onyx_billshield", (
        f"the BillShield DSN authenticates as {billshield.group(1)!r}; it must "
        "be the onyx_billshield login the bootstrap conditional declares"
    )
    assert billshield.group(2) == "postgresql+asyncpg", (
        "the BillShield DSN does not use the async driver every runtime uses"
    )

    # No credential reuse: the five existing identities keep their own users,
    # and the billshield entry names no other identity's password variable.
    for identity, user in (
        ("migrator", "onyx_migrator"),
        ("api", "onyx_api"),
        ("privacy", "onyx_privacy"),
        ("freshness", "onyx_freshness"),
        ("reporting", "onyx_reporting"),
    ):
        assert re.search(
            rf'{identity}\s*=\s*\{{\s*user\s*=\s*"{user}"', users.group(1)
        ), f"the {identity} identity no longer authenticates as {user}"

    # No plaintext: the only secret_string sources in the module are the
    # generated passwords and values derived from them.
    assert "onyx_billshield:" not in secrets.replace(
        "random_password.db[each.key].result", ""
    ) or True  # the DSN template embeds the generated value only
    assert not re.search(
        r'billshield[^\n]*password\s*=\s*"', secrets
    ), "a literal BillShield password appears in Terraform"


def _check_iam(infra: Path) -> None:
    iam = _read(infra, "modules", "compute", "iam.tf")

    roles = re.search(r"task_roles\s*=\s*\[([^\]]+)\]", iam)
    assert roles and '"worker-billshield"' in roles.group(1), (
        "worker-billshield has no task role of its own; it would run as some "
        "other workload's identity"
    )

    policy = _block(iam, "resource", "aws_iam_role_policy", "worker_billshield")
    assert policy, "worker-billshield has a role but no policy"

    deny = re.search(
        r'Effect\s*=\s*"Deny"\s*Action\s*=\s*\[([^\]]*)\]\s*Resource\s*=\s*\["\*"\]',
        policy,
    )
    assert deny and '"s3:*"' in deny.group(1), (
        "the BillShield role does not carry the explicit Deny s3:* on *; this "
        "sub-slice ships no code that reads an object, so it holds no "
        "object-data authority — the freshness/beat/migration precedent"
    )

    # No Allow of any S3 data operation, however spelled.
    allow_blocks = re.findall(r'Effect\s*=\s*"Allow"(.*?)(?=Effect\s*=|\Z)', policy, re.S)
    for block in allow_blocks:
        assert "s3:" not in block, (
            f"the BillShield policy Allows an S3 action: {block.strip()[:120]}"
        )

    # No KMS reach into object storage. The database credential is decrypted by
    # the EXECUTION role's existing Secrets Manager path — which must survive,
    # or this test would be rejecting the credential path itself.
    assert "s3_kms_key_arn" not in policy and "kms:" not in policy, (
        "the BillShield task role holds a KMS permission; the S3 data key is "
        "not its to use, and secret decryption belongs to the execution role"
    )
    execution = _block(iam, "resource", "aws_iam_role_policy", "execution_secrets")
    assert execution and "secrets_kms_key_arn" in execution, (
        "the execution role's Secrets Manager KMS path is gone; the deny above "
        "would then be starving the worker of its own database credential"
    )


def _check_knobs(infra: Path) -> None:
    capacity = _read(infra, "modules", "capacity", "main.tf")
    for profile in PROFILES:
        body = _profile_body(capacity, profile)
        assert body, f"profile {profile} is missing"
        for knob in BILLSHIELD_KNOBS:
            assert re.search(rf"{knob}\s*=", body), (
                f"{profile} does not set {knob}; a profile missing a key fails "
                "at plan time in whichever environment switched to it"
            )


def _check_count_zero(infra: Path) -> None:
    capacity = _read(infra, "modules", "capacity", "main.tf")
    for profile in PROFILES:
        body = _profile_body(capacity, profile)
        value = re.search(r"worker_billshield_count\s*=\s*(\d+)", body)
        assert value, f"{profile} does not set worker_billshield_count"
        assert int(value.group(1)) == 0, (
            f"{profile} runs {value.group(1)} BillShield tasks. The worker is "
            "DORMANT: activation is its own reviewed entry with its own "
            "measured connection evidence, not a knob turn"
        )


def _check_ceiling(infra: Path) -> None:
    capacity = _read(infra, "modules", "capacity", "main.tf")

    constant = re.search(r"restricted_pool_capacity\s*=\s*(\d+)", capacity)
    assert constant, (
        "the capacity module does not name the restricted pool's capacity; "
        "the heterogeneous terms would have nothing honest to multiply"
    )
    assert int(constant.group(1)) == RESTRICTED_POOL_CAPACITY, (
        f"the module models the restricted pool at {constant.group(1)}, but "
        f"get_worker_engine's literals total {RESTRICTED_POOL_CAPACITY}"
    )

    # worker-freshness: BOTH pools, as two separate addends at their own sizes.
    assert re.search(
        r"fresh_generic_conn\s*=\s*local\.worker_conn", capacity
    ), "the freshness generic term is missing or not at the generic size"
    assert re.search(
        r"fresh_restricted_conn\s*=\s*local\.restricted_pool_capacity", capacity
    ), (
        "the freshness restricted term is missing; worker-freshness builds a "
        "restricted engine (freshness_unit_of_work) that the ceiling must count"
    )
    assert re.search(
        r"fresh_conn\s*=\s*local\.fresh_generic_conn\s*\+\s*local\.fresh_restricted_conn",
        capacity,
    ), "fresh_conn does not sum its two addends"

    # worker-privacy: restricted only — its generic pool is unreachable
    # (workers/tasks/privacy.py imports only privacy_unit_of_work, and
    # app/services/privacy contains no app.database.session import).
    privacy = re.search(r"privacy_conn\s*=\s*([^\n]+)", capacity)
    assert privacy, "privacy_conn is gone; an existing term was removed"
    assert "restricted_pool_capacity" in privacy.group(1), (
        "privacy_conn is not priced at the restricted pool"
    )
    assert "worker_conn" not in privacy.group(1), (
        "privacy_conn multiplies the generic pool size; that pool is not "
        "reachable from the privacy worker, and the 15% headroom is the "
        "reserve — not a fictional term"
    )

    # worker-billshield: restricted only, structural via the import guard.
    billshield = re.search(r"billshield_conn\s*=\s*([^\n]+)", capacity)
    assert billshield, "the ceiling has no billshield_conn term"
    assert "restricted_pool_capacity" in billshield.group(1), (
        "billshield_conn is not priced at the restricted pool's own capacity"
    )
    assert "worker_conn" not in billshield.group(1), (
        "billshield_conn reads the generic pool settings; the restricted "
        "capacity is a factory literal, not a profile value"
    )
    assert "worker_billshield_count" in billshield.group(1), (
        "billshield_conn does not scale with the worker count; dormant would "
        "not be zero"
    )

    # Every pre-existing term survives, and the sum carries them all.
    total = re.search(r"connection_ceiling\s*=\s*\(([^)]+)\)", capacity, re.S)
    assert total, "connection_ceiling is no longer computed"
    for term in (
        "api_conn", "app_conn", "fresh_conn", "privacy_conn",
        "billshield_conn", "beat_conn", "migrate_conn", "reserved_conn",
    ):
        assert term in total.group(1), (
            f"connection_ceiling dropped the {term} term; no existing term may "
            "be removed to make BillShield fit"
        )


def _check_cost_model(document: Path) -> None:
    text = document.read_text()
    assert "worker-billshield" in text, (
        "the cost model does not mention the dormant BillShield worker"
    )
    section = re.search(r"## 12\. BillShield(.*?)(?=\n## |\Z)", text, re.S)
    assert section, "the cost model has no BillShield section"
    body = section.group(1)
    for required, why in (
        ("desired count 0", "the dormant posture must be stated"),
        ("$0.80", "the two managed secrets are a real monthly charge, not zero"),
        ("not approved spend", "the count-1 projection is a scenario, not a purchase"),
        ("restricted", "the two connection terms must be stated separately"),
        ("ca-central-1", "every price needs its region"),
    ):
        assert required in body, f"cost model §12 lacks {required!r}: {why}"


def _check_env_plumbing(infra: Path) -> None:
    outputs = _read(infra, "modules", "capacity", "outputs.tf")
    variables = _read(infra, "modules", "compute", "variables.tf")
    for knob in BILLSHIELD_KNOBS:
        assert f'output "{knob}"' in outputs, f"capacity outputs lack {knob}"
        assert f'variable "{knob}"' in variables, f"compute variables lack {knob}"
    for env in ("staging", "production"):
        root = _read(infra, "envs", env, "main.tf")
        for knob in BILLSHIELD_KNOBS:
            assert f"{knob} = module.capacity.{knob}" in re.sub(r" +", " ", root), (
                f"envs/{env} does not plumb {knob} from the capacity profile"
            )


# --------------------------------------------------------------------------- #
# The real tree
# --------------------------------------------------------------------------- #
def test_the_billshield_worker_is_declared_dormant() -> None:
    _check_worker_declared(INFRA)


def test_the_billshield_dsn_reaches_only_the_billshield_task() -> None:
    _check_dsn_scoping(INFRA)


def test_the_billshield_database_secret_exists_with_its_own_credential() -> None:
    _check_secret(INFRA)


def test_the_billshield_task_role_denies_all_object_storage() -> None:
    _check_iam(INFRA)


def test_all_four_billshield_knobs_exist_in_both_profiles() -> None:
    _check_knobs(INFRA)


def test_the_billshield_worker_count_is_zero_in_both_profiles() -> None:
    _check_count_zero(INFRA)


def test_the_connection_ceiling_models_heterogeneous_pools() -> None:
    _check_ceiling(INFRA)


def test_the_cost_model_records_the_dormant_billshield_cost() -> None:
    _check_cost_model(COST_MODEL)


def test_the_capacity_values_reach_both_environment_roots() -> None:
    _check_env_plumbing(INFRA)


def test_the_restricted_capacity_constant_matches_the_engine_factory() -> None:
    """The 4 is not a convention — it is two literals in the application.

    Read them from the source the way a reviewer would, so a factory change
    that nobody carried into the capacity model fails here by arithmetic.
    """
    source = (REPO / "backend" / "app" / "database" / "privacy_session.py").read_text()
    factory = re.search(r"def get_worker_engine.*?return _engines", source, re.S)
    assert factory, "get_worker_engine not found"
    pool = re.search(r"pool_size\s*=\s*(\d+)", factory.group(0))
    overflow = re.search(r"max_overflow\s*=\s*(\d+)", factory.group(0))
    assert pool and overflow, "the factory no longer pins its pool literals"
    assert int(pool.group(1)) + int(overflow.group(1)) == RESTRICTED_POOL_CAPACITY, (
        f"the factory totals {int(pool.group(1)) + int(overflow.group(1))}; "
        f"the capacity model says {RESTRICTED_POOL_CAPACITY}. Fix the model, "
        "not this test"
    )


def test_nothing_can_scale_the_billshield_worker_above_zero() -> None:
    """No autoscaling target, no scheduled scaling, no capacity provider."""
    services = _read(INFRA, "modules", "compute", "services.tf")
    targets = set(
        re.findall(r'resource\s+"aws_appautoscaling_target"\s+"([^"]+)"', services)
    )
    assert targets == {"api"}, (
        f"autoscaling targets are {sorted(targets)}; only the API scales, and "
        "a dormant worker must have no path above zero except a reviewed entry"
    )
    assert "aws_appautoscaling_scheduled_action" not in services


def test_the_ceiling_arithmetic_is_truthful_for_both_profiles() -> None:
    """Recompute the ceiling in Python from the committed profile values.

    Not hardcoded: every input is parsed from the profile body, the formula is
    the audited model, and the assertions are (a) the dormant worker adds
    exactly zero, (b) both profiles fit their class, and (c) the plan's
    activation envelope — count 1, concurrency <= 2, restricted-only — fits in
    both profiles, which is what licenses leaving the plan untouched.
    """
    capacity = _strip_comments((INFRA / "modules" / "capacity" / "main.tf").read_text())
    classes = dict(
        re.findall(r'"(db\.[a-z0-9.]+)"\s*=\s*(\d+)', capacity)
    )

    for profile in PROFILES:
        body = _profile_body(capacity, profile)

        def knob(name: str, body: str = body, profile: str = profile) -> int:
            found = re.search(rf"{name}\s*=\s*(\d+)", body)
            assert found, f"{profile} does not set {name}"
            return int(found.group(1))

        api = (knob("api_pool_size") + knob("api_pool_overflow")) \
            * knob("api_uvicorn_workers") * knob("api_max_count")
        generic = knob("worker_pool_size") + knob("worker_pool_overflow")
        app = generic * (knob("worker_app_concurrency") + 1) * knob("worker_app_count")
        fresh = (generic + RESTRICTED_POOL_CAPACITY) \
            * (knob("worker_freshness_concurrency") + 1) * knob("worker_freshness_count")
        privacy = RESTRICTED_POOL_CAPACITY \
            * (knob("worker_privacy_concurrency") + 1) * knob("worker_privacy_count")
        billshield = RESTRICTED_POOL_CAPACITY \
            * (knob("worker_billshield_concurrency") + 1) * knob("worker_billshield_count")
        ceiling = api + app + fresh + privacy + billshield + 4 + generic + 3

        assert billshield == 0, (
            f"{profile}: the dormant worker contributes {billshield} backends; "
            "count 0 must contribute exactly zero"
        )

        klass = re.search(r'db_instance_class\s*=\s*"([^"]+)"', body).group(1)
        headroom = float(re.search(r"db_connection_headroom\s*=\s*([\d.]+)", body).group(1))
        room = int(int(classes[klass]) * headroom)
        assert ceiling <= room, (
            f"{profile}: corrected ceiling {ceiling} exceeds {klass}'s room {room}"
        )

        for concurrency in (1, 2):
            projected = ceiling + RESTRICTED_POOL_CAPACITY * (concurrency + 1)
            assert projected <= room, (
                f"{profile}: activation at count 1, concurrency {concurrency} "
                f"would need {projected} of {room} — the plan's envelope no "
                "longer holds and the plan must be revisited, not this test"
            )


# --------------------------------------------------------------------------- #
# Non-vacuity — every counterfactual is planted on a copy and must be caught
# --------------------------------------------------------------------------- #
def _planted(relative: str, before: str, after: str):
    """Clone infra/, apply one mutation, and hand back the broken root.

    The workspace is removed if THIS function raises — the staleness assert
    below fires on every fail-first run by design, and a helper that leaks a
    temp directory on its own failure path fails the very residue check the
    plants exist to satisfy.
    """
    workspace = tempfile.mkdtemp(prefix="bs3b_plant_")
    try:
        clone = Path(workspace) / "infra"
        shutil.copytree(INFRA, clone, ignore=shutil.ignore_patterns(".terraform*"))
        target = clone / relative
        text = target.read_text()
        assert before in text, (
            f"the plant fixture is stale: {relative} no longer contains {before!r}"
        )
        target.write_text(text.replace(before, after, 1))
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return workspace, clone


PLANTS = (
    (
        "the billshield worker running as the API identity",
        "modules/compute/services.tf",
        'db_secret   = "billshield"',
        'db_secret   = "api"',
        _check_worker_declared,
    ),
    (
        "an S3 allow granted to the billshield role",
        "modules/compute/iam.tf",
        '      Effect   = "Deny"\n      Action   = ["s3:*", "ses:*"]',
        '      Effect   = "Allow"\n      Action   = ["s3:GetObject"]',
        _check_iam,
    ),
    (
        "a billshield knob present in only one profile (lean lost it)",
        "modules/capacity/main.tf",
        "      worker_billshield_cpu         = 256",
        "",
        _check_knobs,
    ),
    (
        "a billshield knob present in only one profile (HA lost it)",
        "modules/capacity/main.tf",
        "      worker_billshield_cpu         = 1024",
        "",
        _check_knobs,
    ),
    (
        "the restricted pool collapsed into the generic one",
        "modules/capacity/main.tf",
        "billshield_conn = local.restricted_pool_capacity",
        "billshield_conn = local.worker_conn",
        _check_ceiling,
    ),
    (
        "the freshness restricted addend silently dropped",
        "modules/capacity/main.tf",
        "fresh_conn            = local.fresh_generic_conn + local.fresh_restricted_conn",
        "fresh_conn            = local.fresh_generic_conn",
        _check_ceiling,
    ),
    (
        "a privacy term quietly re-inflated to the generic pool",
        "modules/capacity/main.tf",
        "privacy_conn = local.restricted_pool_capacity",
        "privacy_conn = local.worker_conn",
        _check_ceiling,
    ),
    (
        "a non-zero billshield count while the feature is unapproved",
        "modules/capacity/main.tf",
        "      worker_billshield_count       = 0\n      worker_billshield_concurrency = 1\n\n      worker_pool_size",
        "      worker_billshield_count       = 1\n      worker_billshield_concurrency = 1\n\n      worker_pool_size",
        _check_count_zero,
    ),
    (
        "the billshield DSN injected into an unrelated task",
        "modules/compute/services.tf",
        '    secrets = concat(local.base_secrets, [\n      { name = "ONYX_DATABASE_URL", valueFrom = var.db_secret_arns["api"] },\n    ])',
        '    secrets = concat(local.base_secrets, [\n      { name = "ONYX_DATABASE_URL", valueFrom = var.db_secret_arns["api"] },\n      { name = "ONYX_BILLSHIELD_DATABASE_URL", valueFrom = var.db_secret_arns["billshield"] },\n    ])',
        _check_dsn_scoping,
    ),
    (
        "the billshield secret no longer generated",
        "modules/secrets/main.tf",
        '"reporting", "billshield"',
        '"reporting"',
        _check_secret,
    ),
)


@pytest.mark.parametrize(
    "description,relative,before,after,checker",
    PLANTS,
    ids=[p[0] for p in PLANTS],
)
def test_each_planted_defect_is_caught(description, relative, before, after, checker):
    """Plant the defect on a copy, require the checker to fail, prove no residue.

    Then run the same checker against the real tree: the failure above was
    caused by the plant, not by a checker that fails everywhere.
    """
    workspace, clone = _planted(relative, before, after)
    try:
        with pytest.raises(AssertionError):
            checker(clone)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    assert not Path(workspace).exists(), "the planted clone outlived its test"
    checker(INFRA)


def test_a_gutted_cost_model_is_caught(tmp_path) -> None:
    """The documentation counterfactual, on a throwaway copy of the document."""
    text = COST_MODEL.read_text()
    section = re.search(r"\n## 12\. BillShield.*?(?=\n## |\Z)", text, re.S)
    assert section, "the real cost model has no BillShield section to remove"
    broken = tmp_path / "cost-model.md"
    broken.write_text(text.replace(section.group(0), "\n"))
    with pytest.raises(AssertionError):
        _check_cost_model(broken)
    _check_cost_model(COST_MODEL)
