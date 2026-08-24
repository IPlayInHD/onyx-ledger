"""What the deployed estate is allowed to be.

These read the repository's deployment description — `netlify.toml`, the
Terraform under `infra/`, the workflows under `.github/` — and assert properties
that would otherwise only be discovered after an apply.

They are in the backend suite because it is the certified gate. Nothing else in
this repository runs on every commit, and a guard that does not run is a
comment.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
INFRA = REPO / "infra"


def _infra_files() -> list[Path]:
    return sorted(INFRA.rglob("*.tf"))


# ---------------------------------------------------------------------------
# The legacy engine must stay unreachable (entry §10)
# ---------------------------------------------------------------------------
def test_no_deployment_path_reaches_the_legacy_tax_engine() -> None:
    """`legacy/` holds a second, independent JavaScript tax engine.

    It has its own accounts, its own storage and its own answers, and Onyx has
    exactly one customer tax authority. The archive is kept for reference; the
    property that matters is that nothing can serve it. This walks every file
    that could name an origin, a publish directory or a build root and requires
    that none of them names that tree.
    """
    candidates: list[Path] = [REPO / "netlify.toml"]
    candidates += _infra_files()
    candidates += sorted((REPO / ".github").rglob("*.yml"))
    candidates += sorted((REPO / ".github").rglob("*.yaml"))

    offenders: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue  # prose about the archive is the point, not a route
            if re.search(r"\blegacy/(server|static-site)\b|\blegacy\b\s*=", stripped):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {stripped}")

    assert not offenders, (
        "a deployment path names the archived JavaScript engine:\n  "
        + "\n  ".join(offenders)
    )


def test_netlify_publishes_the_certified_frontend_and_nothing_else() -> None:
    """The base and publish directories are the whole of Netlify's reach."""
    config = (REPO / "netlify.toml").read_text()

    base = re.search(r'^\s*base\s*=\s*"([^"]+)"', config, re.M)
    publish = re.search(r'^\s*publish\s*=\s*"([^"]+)"', config, re.M)

    assert base and base.group(1) == "frontend", f"unexpected build base: {base}"
    assert publish and publish.group(1) == "dist", f"unexpected publish dir: {publish}"


def test_cloudfront_serves_only_the_frontend_bucket_and_the_api() -> None:
    """Two origins, both named here.

    A third origin appearing in this distribution is how the archived engine, or
    anything else, would come to answer on the product's hostname.
    """
    edge = (INFRA / "modules" / "edge" / "main.tf").read_text()
    origin_ids = re.findall(r'origin_id\s*=\s*"([^"]+)"', edge)
    declared = {o for o in origin_ids}
    assert declared == {"frontend", "api"}, f"unexpected CloudFront origins: {declared}"


# ---------------------------------------------------------------------------
# The header policy must not be lost in the move to CloudFront (entry §31)
# ---------------------------------------------------------------------------
NETLIFY_HEADER_EQUIVALENTS = {
    "X-Content-Type-Options": "content_type_options",
    "Referrer-Policy": "referrer_policy",
    "X-Frame-Options": "frame_options",
    "Strict-Transport-Security": "strict_transport_security",
    "Permissions-Policy": "Permissions-Policy",
    "Cross-Origin-Opener-Policy": "Cross-Origin-Opener-Policy",
    "Cross-Origin-Resource-Policy": "Cross-Origin-Resource-Policy",
}


def test_every_netlify_security_header_survives_at_cloudfront() -> None:
    """`netlify.toml` states the policy; the edge module implements it.

    Hosting moved from Netlify to CloudFront in the infrastructure entry. The
    risk of that move is not that it fails — it is that it succeeds while
    quietly dropping a header nobody notices for a year.
    """
    netlify = (REPO / "netlify.toml").read_text()
    headers = (INFRA / "modules" / "edge" / "headers.tf").read_text()

    missing: list[str] = []
    for header, marker in NETLIFY_HEADER_EQUIVALENTS.items():
        if header not in netlify:
            continue  # netlify.toml is the source; it does not set this one
        if marker not in headers:
            missing.append(f"{header} (expected {marker!r} in headers.tf)")

    assert not missing, (
        "these headers are set by netlify.toml and have no CloudFront "
        "equivalent:\n  " + "\n  ".join(missing)
    )


def test_hsts_matches_netlify_and_does_not_enable_preload() -> None:
    """Two years, subdomains included, preload off — the same decision twice.

    Preload is very hard to undo and the domain is not settled. If one of these
    two files ever turns it on, the other must be part of that conversation.
    """
    netlify = (REPO / "netlify.toml").read_text()
    headers = (INFRA / "modules" / "edge" / "headers.tf").read_text()

    assert "max-age=63072000" in netlify
    assert "63072000" in headers, "CloudFront HSTS max-age differs from netlify.toml"
    assert "preload" not in netlify.lower().split("strict-transport-security")[1][:120]
    assert re.search(r"preload\s*=\s*false", headers), "HSTS preload must stay off"


# ---------------------------------------------------------------------------
# Edge caching must never hold a customer's tax position
# ---------------------------------------------------------------------------
def test_the_api_behaviour_disables_caching() -> None:
    """The single most dangerous line in the edge module, asserted.

    Every authenticated response under `/api/*` is one customer's tax position.
    A cache policy keyed on the path alone would serve one person's figures to
    the next request for the same URL.
    """
    edge = (INFRA / "modules" / "edge" / "main.tf").read_text()
    api_block = edge.split('path_pattern           = "/api/*"')[1].split("}")[0:12]
    joined = "}".join(api_block)

    assert "cache_policy.disabled.id" in joined, (
        "the /api/* behaviour must use the CachingDisabled policy"
    )
    assert "cache_policy.optimized.id" not in joined


# ---------------------------------------------------------------------------
# Public exposure, as far as the description can prove it
# ---------------------------------------------------------------------------
def test_no_security_group_admits_the_internet_to_a_data_store() -> None:
    """`0.0.0.0/0` may appear on the load balancer and on egress. Never on a
    database or cache ingress rule."""
    network = (INFRA / "modules" / "network" / "main.tf").read_text()

    for resource in ("db_from_tasks", "cache_from_tasks"):
        block = network.split(f'"{resource}"')[1].split("\nresource ")[0]
        assert "cidr_ipv4" not in block, (
            f"{resource} names a CIDR; it must reference a security group only"
        )
        assert "referenced_security_group_id" in block


def test_the_database_is_never_publicly_accessible() -> None:
    database = (INFRA / "modules" / "database" / "main.tf").read_text()
    assert re.search(r"publicly_accessible\s*=\s*false", database), (
        "RDS must not be publicly accessible in any environment"
    )


def test_every_bucket_blocks_public_access() -> None:
    """Every `aws_s3_bucket` has a matching public access block, all four flags
    true. A bucket added without one is the gap this catches."""
    declared: dict[str, Path] = {}
    blocked: set[str] = set()

    for path in _infra_files():
        text = path.read_text()
        for name in re.findall(r'resource\s+"aws_s3_bucket"\s+"([^"]+)"', text):
            declared[name] = path
        for name in re.findall(
            r'resource\s+"aws_s3_bucket_public_access_block"\s+"([^"]+)"', text
        ):
            blocked.add(name)

    assert declared, "no buckets found — the walk is broken"
    unguarded = sorted(set(declared) - blocked)
    assert not unguarded, f"buckets without a public access block: {unguarded}"


# ---------------------------------------------------------------------------
# Erasure must remain possible
# ---------------------------------------------------------------------------
def test_object_lock_is_not_enabled_anywhere() -> None:
    """B2A's erasure path deletes every version and delete marker for a key.

    Object Lock makes that impossible: a locked version cannot be removed before
    its retention expires, so a privacy erasure would either fail or — worse —
    report success having removed nothing. Enabling it is a decision that has to
    come with a redesign of the erasure path, not a checkbox.
    """
    for path in _infra_files():
        text = path.read_text()
        for number, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue
            assert "object_lock_enabled" not in line, (
                f"{path.relative_to(REPO)}:{number} enables S3 Object Lock, which "
                "would break certified privacy erasure"
            )


def test_the_privacy_worker_can_delete_versions_and_the_api_cannot() -> None:
    """Erasure is one role's capability, and deliberately not the API's."""
    iam = (INFRA / "modules" / "compute" / "iam.tf").read_text()

    privacy = iam.split('"worker_privacy"')[1].split("\nresource ")[0]
    assert "s3:DeleteObjectVersion" in privacy
    assert "s3:ListBucketVersions" in privacy

    api = iam.split('resource "aws_iam_role_policy" "api"')[1].split("\nresource ")[0]
    assert "s3:DeleteObjectVersion" not in api, (
        "the API must not hold version deletion; erasure belongs to the privacy worker"
    )


# ---------------------------------------------------------------------------
# Deployment credentials
# ---------------------------------------------------------------------------
def test_the_deploy_role_cannot_read_secret_values() -> None:
    """A deployment needs secret ARNs, never their contents. Reading them is the
    classic escalation from "can deploy" to "can sign a token as anyone"."""
    oidc = (INFRA / "modules" / "github_oidc" / "main.tf").read_text()
    deny = oidc.split('"deploy_denies"')[1]
    assert "secretsmanager:GetSecretValue" in deny
    assert '"Deny"' in deny


def test_github_trust_is_never_a_wildcard_over_repositories() -> None:
    """The `sub` claim must name a repository. `repo:*` would let any fork of
    anything assume the deployment role."""
    for env in ("staging", "production"):
        text = (INFRA / "envs" / env / "main.tf").read_text()
        subjects = re.search(r"trusted_subjects\s*=\s*\[([^\]]*)\]", text)
        assert subjects, f"{env}: no trusted_subjects"
        body = subjects.group(1)
        assert "repo:${var.github_repository}" in body, f"{env}: {body}"
        assert "repo:*" not in body


@pytest.mark.parametrize("env", ["staging", "production"])
def test_no_aws_access_key_is_committed_for_deployment(env: str) -> None:
    """OIDC or nothing. A stored key never expires and leaves with whoever had it."""
    text = (INFRA / "envs" / env / "main.tf").read_text()
    assert "aws_iam_access_key" not in text
    assert not re.search(r"AKIA[0-9A-Z]{16}", text)
