variable "profile" {
  description = <<-DOC
    Which capacity profile this environment runs at.

    `lean_launch`       — pre-revenue and early-revenue. One of everything,
                          measured sizes, single-AZ database, one NAT.
    `high_availability` — multi-AZ database, NAT per AZ, replicated cache,
                          more than one of every task.

    Changing this value is the ONLY supported way to move between the two. It
    is a deliberate, reviewable, single-line change — not a redesign, and not a
    pile of individually-tuned numbers spread across an environment root.
  DOC
  type        = string

  validation {
    condition     = contains(["lean_launch", "high_availability"], var.profile)
    error_message = "profile must be lean_launch or high_availability."
  }
}
