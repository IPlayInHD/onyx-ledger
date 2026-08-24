variable "name" { type = string }
variable "environment" {
  description = "Value of ONYX_ENVIRONMENT. `production` turns on the fail-closed validators."
  type        = string
}
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "alb_security_group_id" { type = string }
variable "tasks_security_group_id" { type = string }
variable "certificate_arn" { type = string }
variable "access_logs_bucket" { type = string }
variable "app_public_url" { type = string }
variable "email_sender_address" { type = string }
variable "ses_identity_arn" { type = string }

variable "image_uri" {
  description = "Image by DIGEST, never a floating tag. The pipeline supplies it."
  type        = string
}

variable "documents_bucket" { type = string }
variable "documents_bucket_arn" { type = string }
variable "legislation_bucket" { type = string }
variable "legislation_bucket_arn" { type = string }
variable "s3_kms_key_arn" { type = string }
variable "logs_kms_key_arn" { type = string }
variable "secrets_kms_key_arn" { type = string }

variable "jwt_secret_arn" { type = string }
variable "admission_secret_arn" { type = string }
variable "redis_url_arn" { type = string }
variable "db_secret_arns" { type = map(string) }
variable "all_secret_arns" {
  description = "Every secret the execution role may inject."
  type        = list(string)
}

variable "api_port" {
  type    = number
  default = 8000
}
# --- capacity, supplied by modules/capacity ----------------------------------
# Nothing here has a default. A missing size must fail the plan rather than
# quietly land an environment on whatever this file happened to say.
variable "api_cpu" { type = number }
variable "api_memory" { type = number }
variable "api_count" { type = number }
variable "api_max_count" { type = number }
variable "api_uvicorn_workers" { type = number }
variable "api_pool_size" { type = number }
variable "api_pool_overflow" { type = number }

variable "worker_app_cpu" { type = number }
variable "worker_app_memory" { type = number }
variable "worker_app_count" { type = number }
variable "worker_app_concurrency" { type = number }

variable "worker_freshness_cpu" { type = number }
variable "worker_freshness_memory" { type = number }
variable "worker_freshness_count" { type = number }
variable "worker_freshness_concurrency" { type = number }

variable "worker_privacy_cpu" { type = number }
variable "worker_privacy_memory" { type = number }
variable "worker_privacy_count" { type = number }
variable "worker_privacy_concurrency" { type = number }

variable "worker_pool_size" { type = number }
variable "worker_pool_overflow" { type = number }

variable "beat_cpu" { type = number }
variable "beat_memory" { type = number }

variable "migration_cpu" { type = number }
variable "migration_memory" { type = number }

# The HARD ceiling on how many API tasks autoscaling may ever run. Separate
# from api_max_count so that a mistake in a profile cannot become an unbounded
# bill: the autoscaling target refuses to plan if the profile asks for more.
variable "api_absolute_max_count" {
  type    = number
  default = 6
}

variable "ecr_repository_url" {
  description = "Persistent registry, created in envs/shared. NOT owned here, so destroying an environment does not destroy its images."
  type        = string
}

variable "origin_verify_secret" {
  description = "Shared with modules/edge. The load balancer refuses any request that does not carry it."
  type        = string
  sensitive   = true
}

variable "log_retention_days" {
  description = "Operational log retention. NOT a customer-data retention policy."
  type        = number
  default     = 30
}
variable "deletion_protection" { type = bool }
variable "enable_execute_command" {
  description = "ECS Exec. On in staging for debugging; off in production."
  type        = bool
  default     = false
}
variable "tags" {
  type    = map(string)
  default = {}
}
