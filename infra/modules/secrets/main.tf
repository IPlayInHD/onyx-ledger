# =============================================================================
# SECRETS — one path per environment
# =============================================================================
# The application reads whole connection strings (ONYX_DATABASE_URL and the
# three siblings), so that is what is stored: assembled here, never assembled in
# a task definition where the pieces would show up in a console.
#
# ECS injects these by ARN. A secret's VALUE never appears in a task definition,
# an image, a log line or a CI variable — the task role is granted
# GetSecretValue on exactly these ARNs and reads them at container start.
#
# Terraform state holds these values in cleartext, which is why the state bucket
# in `bootstrap/` is encrypted, versioned and blocked from public access, and why
# nobody should keep a local copy of a production state file.

locals { tags = merge(var.tags, { Module = "secrets" }) }

resource "aws_kms_key" "this" {
  description             = "${var.name} application secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}-secrets"
  target_key_id = aws_kms_key.this.key_id
}

# --------------------------------------------------- application identities --
# `Settings._production_secrets_are_real` refuses the development default and
# anything under 32 characters. 64 hex characters is 32 bytes of entropy and
# clears that bar with room to spare.
resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "admission" {
  length  = 64
  special = false
}

# Rotating the JWT secret signs every live session out. It touches no sealed tax
# artefact — those are hashed over canonical content, not signed with this key —
# so it is a sign-out event, not a data event. runbooks.md carries the drill.
resource "aws_secretsmanager_secret" "jwt" {
  name                    = "${var.name}/onyx/jwt-secret"
  description             = "Access-token signing key. Rotation signs everyone out."
  kms_key_id              = aws_kms_key.this.id
  recovery_window_in_days = var.recovery_window_days
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "jwt" {
  secret_id     = aws_secretsmanager_secret.jwt.id
  secret_string = random_password.jwt.result
}

resource "aws_secretsmanager_secret" "admission" {
  name                    = "${var.name}/onyx/admission-identity-secret"
  description             = "Keys the per-identity admission counters."
  kms_key_id              = aws_kms_key.this.id
  recovery_window_in_days = var.recovery_window_days
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "admission" {
  secret_id     = aws_secretsmanager_secret.admission.id
  secret_string = random_password.admission.result
}

# ------------------------------------------------------ database identities --
# Four connection strings for four principals. They are separate secrets rather
# than one document so that a task role can be granted the one it needs: the API
# has no business being able to read the privacy worker's credentials.
resource "random_password" "db" {
  for_each = toset(["migrator", "api", "privacy", "freshness", "reporting"])
  length   = 48
  special  = false
}

resource "aws_secretsmanager_secret" "db" {
  for_each                = random_password.db
  name                    = "${var.name}/onyx/db/${each.key}"
  description             = "PostgreSQL connection string for the ${each.key} identity."
  kms_key_id              = aws_kms_key.this.id
  recovery_window_in_days = var.recovery_window_days
  tags                    = local.tags
}

locals {
  # asyncpg for the application, psycopg2 for Alembic. sslmode=verify-full is
  # what makes `rds.force_ssl` meaningful from the client's side — force_ssl
  # stops cleartext, verify-full stops a man in the middle presenting any cert.
  db_users = {
    migrator  = { user = "onyx_migrator", driver = "postgresql+psycopg2" }
    api       = { user = "onyx_api", driver = "postgresql+asyncpg" }
    privacy   = { user = "onyx_privacy", driver = "postgresql+asyncpg" }
    freshness = { user = "onyx_freshness", driver = "postgresql+asyncpg" }
    reporting = { user = "onyx_reporting", driver = "postgresql+asyncpg" }
  }
}

resource "aws_secretsmanager_secret_version" "db" {
  for_each  = local.db_users
  secret_id = aws_secretsmanager_secret.db[each.key].id
  secret_string = format(
    "%s://%s:%s@%s:%d/onyx?ssl=verify-full",
    each.value.driver,
    each.value.user,
    random_password.db[each.key].result,
    var.database_endpoint,
    var.database_port,
  )
}

# The raw passwords, separately, because `bootstrap_runtime_logins.sql` needs
# them as psql variables and must not have to parse a URL to find them.
resource "aws_secretsmanager_secret" "db_password" {
  for_each                = random_password.db
  name                    = "${var.name}/onyx/db-password/${each.key}"
  description             = "Password only, for the bootstrap SQL."
  kms_key_id              = aws_kms_key.this.id
  recovery_window_in_days = var.recovery_window_days
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "db_password" {
  for_each      = random_password.db
  secret_id     = aws_secretsmanager_secret.db_password[each.key].id
  secret_string = each.value.result
}

# ------------------------------------------------------------------- cache --
resource "aws_secretsmanager_secret" "redis" {
  name                    = "${var.name}/onyx/redis-url"
  description             = "Celery broker URL, including the AUTH token."
  kms_key_id              = aws_kms_key.this.id
  recovery_window_in_days = var.recovery_window_days
  tags                    = local.tags
}

# `rediss://` — TLS. The replication group has transit encryption on, so the
# plain `redis://` scheme would simply fail to connect, which is the right
# failure but a confusing one to debug at three in the morning.
resource "aws_secretsmanager_secret_version" "redis" {
  secret_id     = aws_secretsmanager_secret.redis.id
  secret_string = "rediss://:${var.cache_auth_token}@${var.cache_endpoint}:${var.cache_port}/0"
}

# --------------------------------------------------------- not wired yet -----
# Created empty so that the day a provider is chosen, the plumbing — task role
# grants, injection, rotation runbook — already exists and nobody is tempted to
# paste a key into an environment variable "just to test it".
resource "aws_secretsmanager_secret" "placeholder" {
  for_each                = toset(["ai-provider", "payments", "error-reporting"])
  name                    = "${var.name}/onyx/${each.key}"
  description             = "Reserved. No provider is wired; see the closure report."
  kms_key_id              = aws_kms_key.this.id
  recovery_window_in_days = var.recovery_window_days
  tags                    = merge(local.tags, { Status = "reserved-unwired" })
}
