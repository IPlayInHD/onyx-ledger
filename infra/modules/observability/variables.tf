variable "name" { type = string }
variable "region" { type = string }
variable "logs_kms_key_id" {
  description = "KMS key for the alert topic. Created in the environment root because network and compute need it first."
  type        = string
}
variable "account_id" { type = string }
variable "alert_emails" {
  description = "Where a page goes. Empty in staging is acceptable; empty in production is not."
  type        = list(string)
  default     = []
}
variable "alb_suffix" { type = string }
variable "target_group_suffix" { type = string }
variable "db_identifier" { type = string }
variable "cache_replication_group_id" { type = string }
variable "log_group_name" { type = string }
variable "max_connections_alarm" { type = number }
variable "tags" {
  type    = map(string)
  default = {}
}
