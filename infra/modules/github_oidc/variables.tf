variable "name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "create_provider" {
  description = "One OIDC provider per account. The first environment creates it; the second reuses it."
  type        = bool
  default     = false
}
variable "thumbprints" {
  description = "GitHub's OIDC certificate thumbprints."
  type        = list(string)
  default     = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd"]
}
variable "trusted_subjects" {
  description = "Exact `sub` claims allowed to assume the role. Never a wildcard over repositories."
  type        = list(string)
}
variable "ecr_repository_arn" { type = string }
variable "cluster_arn" { type = string }
variable "cluster_name" { type = string }
variable "passable_role_arns" { type = list(string) }
variable "frontend_bucket_arn" { type = string }
variable "distribution_arn" { type = string }
variable "tags" {
  type    = map(string)
  default = {}
}
