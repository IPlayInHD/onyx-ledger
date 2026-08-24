variable "name" { type = string }
variable "data_subnet_ids" { type = list(string) }
variable "security_group_id" { type = string }
variable "logs_kms_key_arn" { type = string }
variable "engine_version" {
  type    = string
  default = "7.2"
}
variable "node_type" { type = string }
variable "node_count" {
  description = "1 in staging. 2+ in production so a node loss fails over."
  type        = number
}
variable "snapshot_retention_days" {
  type    = number
  default = 1
}
variable "tags" {
  type    = map(string)
  default = {}
}
