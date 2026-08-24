variable "name" { type = string }
variable "database_endpoint" { type = string }
variable "database_port" { type = number }
variable "cache_endpoint" { type = string }
variable "cache_port" { type = number }
variable "cache_auth_token" {
  type      = string
  sensitive = true
}
variable "recovery_window_days" {
  description = "Deleted-secret grace period. 0 in staging so a rebuild is not blocked by a name still in the bin."
  type        = number
  default     = 30
}
variable "tags" {
  type    = map(string)
  default = {}
}
