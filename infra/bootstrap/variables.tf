variable "region" {
  type    = string
  default = "ca-central-1"
}
variable "state_bucket_name" {
  description = "Globally unique. Suggest onyx-tfstate-<account-id>."
  type        = string
}
variable "lock_table_name" {
  type    = string
  default = "onyx-terraform-locks"
}
