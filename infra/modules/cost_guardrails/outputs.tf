output "budget_name" { value = aws_budgets_budget.monthly.name }
output "anomaly_monitor_arn" { value = aws_ce_anomaly_monitor.services.arn }
