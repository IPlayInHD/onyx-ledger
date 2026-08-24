variable "name" { type = string }
variable "data_subnet_ids" { type = list(string) }
variable "security_group_id" { type = string }
variable "engine_version" {
  type    = string
  default = "16.4"
}
variable "instance_class" { type = string }
variable "allocated_storage" {
  type    = number
  default = 20
}
variable "max_allocated_storage" {
  description = "Storage autoscaling ceiling. Running out of disk is an outage."
  type        = number
  default     = 200
}
variable "master_username" {
  type    = string
  default = "onyx_root"
}
variable "multi_az" { type = bool }
variable "backup_retention_days" {
  description = "Operational backup window. NOT a customer-data retention policy — see PD-3."
  type        = number
}
variable "deletion_protection" {
  description = <<-DOC
    Whether the instance refuses `terraform destroy`.

    DEFAULTS TO TRUE. An environment that forgets to state its intent gets the
    protected value; only an environment that deliberately says `false` — and
    staging, which is ephemeral by design, is the one that does — can be torn
    down. Making the safe value the default is also what lets this module be
    scanned in isolation without a false finding.
  DOC
  type        = bool
  default     = true
}
variable "tags" {
  type    = map(string)
  default = {}
}
