# =============================================================================
# DATABASE — RDS PostgreSQL 16
# =============================================================================
# The application's identity model only means something if the database is
# provisioned the way `scripts/ci_provision_postgres.sh` provisions it: the
# migrator creates the schema and therefore OWNS it, and the runtime logs in as
# somebody else. `identity.subject_key_for` is SECURITY DEFINER owned by
# `onyx_migrator`, and `audit.log_change` calls it on every write — apply the
# schema as any other identity and the first customer registration fails inside
# an audit trigger with "permission denied for schema identity", with nothing
# before it going wrong. `bootstrap.sql` in this module is that ordering.

locals { tags = merge(var.tags, { Module = "database" }) }

resource "aws_kms_key" "this" {
  description             = "${var.name} RDS encryption at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}-rds"
  target_key_id = aws_kms_key.this.key_id
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.data_subnet_ids
  tags       = local.tags
}

# `rds.force_ssl` is the setting that makes "TLS in transit" true rather than
# available. Without it a client that omits sslmode connects in cleartext and
# nothing complains.
resource "aws_db_parameter_group" "this" {
  name        = "${var.name}-pg16"
  family      = "postgres16"
  description = "Onyx ${var.name}"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  # Log statements that take longer than a second. Not every statement: the
  # application logs its own request timings, and a full statement log on a tax
  # product is a second copy of customer data in a different retention regime.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  # pgvector, used by the knowledge base.
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  lifecycle { create_before_destroy = true }
}

resource "random_password" "master" {
  length  = 48
  special = false # RDS rejects several punctuation characters in master passwords
}

# `deletion_protection` is an input: true in production, false in staging so a
# throwaway environment stays throwaway. tfsec cannot resolve the variable from
# inside the module and reads the unknown as "off".
# tfsec:ignore:aws-rds-enable-deletion-protection
resource "aws_db_instance" "this" {
  identifier     = "${var.name}-postgres"
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.this.arn

  db_name  = "onyx"
  username = var.master_username
  password = random_password.master.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name

  # The property the closure report has to be able to assert. A data-tier subnet
  # has no route to a gateway either, so this is belt and braces on purpose.
  publicly_accessible = false

  multi_az                 = var.multi_az
  backup_retention_period  = var.backup_retention_days
  backup_window            = "07:00-08:00" # 02:00 Montréal
  maintenance_window       = "Mon:08:30-Mon:09:30"
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  # PITR is what `backup_retention_period > 0` buys on RDS: transaction logs are
  # shipped continuously and any second in the window can be restored to.
  # `docs/operations/runbooks.md` carries the drill.
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name}-postgres-final"

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.this.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.monitoring.arn
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]

  auto_minor_version_upgrade = true
  apply_immediately          = false

  # Enabled, not used yet. The application holds a password from Secrets Manager
  # today; IAM auth would replace it with a 15-minute token and remove the
  # long-lived credential entirely. That is an application change — SQLAlchemy
  # would need to refresh the token on reconnect — so it is switched on here and
  # recorded as the follow-up rather than half-adopted.
  iam_database_authentication_enabled = true

  tags = local.tags
}

resource "aws_iam_role" "monitoring" {
  name = "${var.name}-rds-monitoring"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  role       = aws_iam_role.monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
