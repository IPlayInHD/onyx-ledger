# =============================================================================
# STAGING
# =============================================================================
# A real deployment, deliberately small. Same shape as production so that what
# it proves transfers; a fraction of the capacity so that proving it is cheap.
#
# Differences from production, each on purpose:
#   * one NAT gateway, one cache node, single-AZ database
#   * ECS Exec enabled, so a person can open a shell in a task to debug
#   * deletion protection off, so the environment can be torn down and rebuilt
#   * secret recovery window 0, so a rebuild is not blocked by a name in the bin
#   * ONYX_ENVIRONMENT=staging, so the production fail-closed validators are NOT
#     exercised here — DRAFT legal documents are allowed to be accepted, which
#     is exactly what makes staging able to run the journeys at all

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Filled in by `terraform init -backend-config=backend.hcl`, so the bucket
  # name — which contains the account id — is not committed.
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "onyx-ledger"
      Environment = "staging"
      ManagedBy   = "terraform"
    }
  }
}

# CloudFront's certificate and its web ACL must live in us-east-1 whatever the
# rest of the estate does.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = {
      Project     = "onyx-ledger"
      Environment = "staging"
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

# Every size in this root comes from here. Nothing below invents a number.
module "capacity" {
  source  = "../../modules/capacity"
  profile = var.capacity_profile
}

# The secret CloudFront sends and the load balancer's listener demands. Created
# HERE rather than in either module, because both need it and modules/compute
# cannot depend on modules/edge — edge already depends on compute for the load
# balancer's DNS name.
resource "random_password" "origin_verify" {
  length  = 48
  special = false
}

locals {
  name = "staging"
  tags = { Environment = "staging" }
}

# The log encryption key, created here because network, cache and compute all
# need it before observability can exist. See modules/observability/README.md.
resource "aws_kms_key" "logs" {
  description             = "staging CloudWatch Logs encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountRoot"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = { "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:*" }
        }
      },
    ]
  })
}

module "network" {
  source             = "../../modules/network"
  name               = local.name
  region             = var.region
  vpc_cidr           = "10.30.0.0/16"
  single_nat_gateway = module.capacity.single_nat_gateway
  logs_kms_key_arn   = aws_kms_key.logs.arn
  tags               = local.tags
}

module "database" {
  source                = "../../modules/database"
  name                  = local.name
  data_subnet_ids       = module.network.data_subnet_ids
  security_group_id     = module.network.database_security_group_id
  instance_class        = module.capacity.db_instance_class
  allocated_storage     = module.capacity.db_allocated_storage
  max_allocated_storage = module.capacity.db_max_allocated_storage
  multi_az              = module.capacity.db_multi_az
  backup_retention_days = module.capacity.db_backup_retention_days
  deletion_protection   = false
  tags                  = local.tags
}

module "cache" {
  source            = "../../modules/cache"
  name              = local.name
  data_subnet_ids   = module.network.data_subnet_ids
  security_group_id = module.network.cache_security_group_id
  logs_kms_key_arn  = aws_kms_key.logs.arn
  node_type         = module.capacity.cache_node_type
  node_count        = module.capacity.cache_node_count
  tags              = local.tags
}

module "storage" {
  source = "../../modules/storage"
  name   = local.name
  tags   = local.tags
}

module "secrets" {
  source               = "../../modules/secrets"
  name                 = local.name
  database_endpoint    = module.database.endpoint
  database_port        = module.database.port
  cache_endpoint       = module.cache.primary_endpoint
  cache_port           = module.cache.port
  cache_auth_token     = module.cache.auth_token
  recovery_window_days = 0
  tags                 = local.tags
}

module "compute" {
  source      = "../../modules/compute"
  name        = local.name
  environment = "staging"
  region      = var.region

  vpc_id                  = module.network.vpc_id
  public_subnet_ids       = module.network.public_subnet_ids
  private_subnet_ids      = module.network.private_subnet_ids
  alb_security_group_id   = module.network.alb_security_group_id
  tasks_security_group_id = module.network.tasks_security_group_id

  certificate_arn    = var.alb_certificate_arn
  access_logs_bucket = "${local.name}-onyx-access-logs"
  app_public_url     = var.app_public_url

  email_sender_address = var.email_sender_address
  ses_identity_arn     = var.ses_identity_arn

  image_uri = var.image_uri

  documents_bucket       = module.storage.documents_bucket
  documents_bucket_arn   = module.storage.documents_bucket_arn
  legislation_bucket     = module.storage.legislation_bucket
  legislation_bucket_arn = module.storage.legislation_bucket_arn
  s3_kms_key_arn         = module.storage.kms_key_arn
  logs_kms_key_arn       = aws_kms_key.logs.arn
  secrets_kms_key_arn    = module.secrets.kms_key_arn

  jwt_secret_arn       = module.secrets.jwt_secret_arn
  admission_secret_arn = module.secrets.admission_secret_arn
  redis_url_arn        = module.secrets.redis_url_arn
  db_secret_arns       = module.secrets.db_secret_arns
  all_secret_arns = concat(
    [module.secrets.jwt_secret_arn, module.secrets.admission_secret_arn, module.secrets.redis_url_arn],
    values(module.secrets.db_secret_arns),
  )

  api_cpu             = module.capacity.api_cpu
  api_memory          = module.capacity.api_memory
  api_count           = module.capacity.api_count
  api_max_count       = module.capacity.api_max_count
  api_uvicorn_workers = module.capacity.api_uvicorn_workers
  api_pool_size       = module.capacity.api_pool_size
  api_pool_overflow   = module.capacity.api_pool_overflow

  worker_app_cpu         = module.capacity.worker_app_cpu
  worker_app_memory      = module.capacity.worker_app_memory
  worker_app_count       = module.capacity.worker_app_count
  worker_app_concurrency = module.capacity.worker_app_concurrency

  worker_freshness_cpu         = module.capacity.worker_freshness_cpu
  worker_freshness_memory      = module.capacity.worker_freshness_memory
  worker_freshness_count       = module.capacity.worker_freshness_count
  worker_freshness_concurrency = module.capacity.worker_freshness_concurrency

  worker_privacy_cpu         = module.capacity.worker_privacy_cpu
  worker_privacy_memory      = module.capacity.worker_privacy_memory
  worker_privacy_count       = module.capacity.worker_privacy_count
  worker_privacy_concurrency = module.capacity.worker_privacy_concurrency

  worker_billshield_cpu         = module.capacity.worker_billshield_cpu
  worker_billshield_memory      = module.capacity.worker_billshield_memory
  worker_billshield_count       = module.capacity.worker_billshield_count
  worker_billshield_concurrency = module.capacity.worker_billshield_concurrency

  worker_pool_size     = module.capacity.worker_pool_size
  worker_pool_overflow = module.capacity.worker_pool_overflow

  beat_cpu    = module.capacity.beat_cpu
  beat_memory = module.capacity.beat_memory

  migration_cpu    = module.capacity.migration_cpu
  migration_memory = module.capacity.migration_memory

  ecr_repository_url   = var.ecr_repository_url
  origin_verify_secret = random_password.origin_verify.result

  log_retention_days     = module.capacity.log_retention_days
  deletion_protection    = false
  enable_execute_command = true

  tags = local.tags
}

module "edge" {
  source = "../../modules/edge"
  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }
  name                      = local.name
  aliases                   = var.aliases
  certificate_arn           = var.cloudfront_certificate_arn
  alb_dns_name              = module.compute.alb_dns_name
  origin_verify_secret      = random_password.origin_verify.result
  access_logs_bucket_domain = "${local.name}-onyx-access-logs.s3.amazonaws.com"
  tags                      = local.tags
}

module "observability" {
  source                     = "../../modules/observability"
  name                       = local.name
  region                     = var.region
  account_id                 = data.aws_caller_identity.current.account_id
  logs_kms_key_id            = aws_kms_key.logs.id
  alert_emails               = var.alert_emails
  alb_suffix                 = module.compute.alb_arn_suffix
  target_group_suffix        = module.compute.target_group_arn_suffix
  db_identifier              = module.database.identifier
  cache_replication_group_id = "${local.name}-valkey"
  log_group_name             = module.compute.log_group_name
  max_connections_alarm      = module.capacity.connection_room
  tags                       = local.tags
}

module "github_oidc" {
  source          = "../../modules/github_oidc"
  name            = local.name
  region          = var.region
  account_id      = data.aws_caller_identity.current.account_id
  create_provider = false # the provider is created once, in envs/shared

  # The default branch only. Not `*`, not a pull request, not a fork.
  trusted_subjects = ["repo:${var.github_repository}:ref:refs/heads/main"]

  ecr_repository_arn  = var.ecr_repository_arn
  cluster_arn         = module.compute.cluster_arn
  cluster_name        = module.compute.cluster_name
  passable_role_arns  = module.compute.passable_role_arns
  frontend_bucket_arn = module.edge.frontend_bucket_arn
  distribution_arn    = module.edge.distribution_arn
  tags                = local.tags
}

# ------------------------------------------------------------ cost --------
module "cost_guardrails" {
  source             = "../../modules/cost_guardrails"
  name               = local.name
  monthly_budget_usd = module.capacity.monthly_budget_usd
  alert_emails       = var.alert_emails
  tags               = local.tags
}
