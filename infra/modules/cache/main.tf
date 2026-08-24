# =============================================================================
# CACHE — ElastiCache, Valkey
# =============================================================================
# Celery's broker and result backend. Valkey rather than Redis OSS: it is the
# supported successor on ElastiCache, wire-compatible with the Redis client the
# application already uses, and materially cheaper for the same shape.
#
# REDIS IS NOT BUSINESS TRUTH HERE, and the architecture already reflects that:
# the freshness pipeline keeps its outbox in PostgreSQL and relays from it, so
# a flushed cache costs a re-delivery rather than a lost invalidation. Snapshots
# are therefore configured for operational convenience, not for durability of
# anything that matters.

locals { tags = merge(var.tags, { Module = "cache" }) }

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name}-cache"
  subnet_ids = var.data_subnet_ids
  tags       = local.tags
}

resource "random_password" "auth_token" {
  length  = 64
  special = false # ElastiCache AUTH tokens accept a restricted character set
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = "${var.name}-valkey"
  description          = "Onyx ${var.name} Celery broker"

  engine         = "valkey"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = 6379

  num_cache_clusters         = var.node_count
  automatic_failover_enabled = var.node_count > 1
  multi_az_enabled           = var.node_count > 1

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [var.security_group_id]

  # Both halves of "encrypted". In transit is the one that matters for an AUTH
  # token: without it the token crosses the VPC in cleartext on every connect.
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.auth_token.result
  kms_key_id                 = aws_kms_key.this.arn

  snapshot_retention_limit = var.snapshot_retention_days
  snapshot_window          = "06:00-07:00"
  maintenance_window       = "mon:09:30-mon:10:30"

  apply_immediately          = false
  auto_minor_version_upgrade = true

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.slow.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  tags = local.tags
}

resource "aws_kms_key" "this" {
  description             = "${var.name} ElastiCache encryption at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_cloudwatch_log_group" "slow" {
  name              = "/onyx/${var.name}/valkey-slow"
  retention_in_days = 14
  kms_key_id        = var.logs_kms_key_arn
  tags              = local.tags
}
