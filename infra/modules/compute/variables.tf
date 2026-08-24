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
variable "ecr_kms_key_arn" { type = string }
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
variable "api_cpu" { type = number }
variable "api_memory" { type = number }
variable "api_count" { type = number }
variable "api_max_count" { type = number }
variable "worker_cpu" { type = number }
variable "worker_memory" { type = number }
variable "worker_app_count" { type = number }

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
