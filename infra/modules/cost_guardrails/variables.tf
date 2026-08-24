variable "name" { type = string }

variable "monthly_budget_usd" {
  description = "The number this environment is expected to cost. Not a cap — nothing here stops spending; it stops SURPRISE."
  type        = number
}

variable "alert_emails" {
  description = "Where a budget or anomaly notice goes. Empty means no one is told, which is why the plan refuses it."
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "A budget nobody is told about is not a guardrail. Provide at least one address."
  }
}

variable "anomaly_threshold_usd" {
  description = "Absolute dollar impact above which Cost Anomaly Detection raises a notice."
  type        = number
  default     = 25
}

variable "tags" {
  type    = map(string)
  default = {}
}
