# =============================================================================
# BILL-SHOCK GUARDRAILS
# =============================================================================
# WHAT THIS DOES: tells a person, early, in several independent ways, that the
# spend is not what was expected.
#
# WHAT THIS DELIBERATELY DOES NOT DO: act. There is no automated scale-to-zero,
# no service stop, no snapshot-and-delete. A budget overrun on a tax product is
# a reason to look, not a reason to take a customer's filing offline — and an
# automated destructive action wired to a billing signal is a self-inflicted
# outage waiting for a pricing change. §11 says this explicitly and the absence
# is the design.
#
# The three signals are independent on purpose:
#   * ACTUAL thresholds catch a step change after it has landed.
#   * The FORECAST threshold catches a trend before month end.
#   * Cost Anomaly Detection catches a shape change that no threshold would
#     have noticed, because it compares against the account's own history.

locals { tags = merge(var.tags, { Module = "cost_guardrails" }) }

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-onyx-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$onyx-ledger"]
  }

  # LOW FIRST. A notice at half the budget is the one that arrives while there
  # is still a cheap decision to make; a notice at 100% arrives after the money
  # is spent.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.alert_emails
  }

  # The one that arrives before the money is spent.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.alert_emails
  }
}

resource "aws_ce_anomaly_monitor" "services" {
  name              = "${var.name}-onyx-services"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
  tags              = local.tags
}

resource "aws_ce_anomaly_subscription" "services" {
  name      = "${var.name}-onyx-anomalies"
  frequency = "DAILY"

  monitor_arn_list = [aws_ce_anomaly_monitor.services.arn]

  dynamic "subscriber" {
    for_each = var.alert_emails
    content {
      type    = "EMAIL"
      address = subscriber.value
    }
  }

  # Absolute dollars, not a percentage: at a $200 estate a 20% anomaly is $40
  # and worth a look, while at a $20 estate a 20% anomaly is $4 and is noise.
  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = [tostring(var.anomaly_threshold_usd)]
    }
  }

  tags = local.tags
}
