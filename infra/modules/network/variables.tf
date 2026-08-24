variable "name" {
  description = "Environment name, used as the prefix on every resource."
  type        = string
}

variable "region" {
  description = "AWS region. Used to name the S3 gateway endpoint service."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR for the VPC. /16 leaves room for the three /20 tiers."
  type        = string
  default     = "10.20.0.0/16"
}

variable "single_nat_gateway" {
  description = "One NAT gateway instead of one per AZ. Staging only."
  type        = bool
  default     = false
}

variable "api_port" {
  description = "Container port the API listens on."
  type        = number
  default     = 8000
}

variable "logs_kms_key_arn" {
  description = "KMS key for log group encryption."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource in the module."
  type        = map(string)
  default     = {}
}
