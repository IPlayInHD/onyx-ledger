# =============================================================================
# OBSERVABILITY — a small number of alarms that mean something
# =============================================================================
# The failure mode this avoids is a hundred alarms nobody reads. Each one below
# corresponds to a thing that is actually broken and that a person would act on
# at two in the morning; anything merely interesting belongs on a dashboard.

locals { tags = merge(var.tags, { Module = "observability" }) }

resource "aws_sns_topic" "alerts" {
  name              = "${var.name}-onyx-alerts"
  kms_master_key_id = var.logs_kms_key_id
  tags              = local.tags
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = toset(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# ------------------------------------------------------------------- API -----
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.name}-onyx-api-5xx"
  alarm_description   = "The API is returning server errors. Something is broken, not merely slow."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = var.alb_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "api_unhealthy" {
  alarm_name          = "${var.name}-onyx-api-unhealthy"
  alarm_description   = "No healthy API task. The product is down."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  dimensions          = { LoadBalancer = var.alb_suffix, TargetGroup = var.target_group_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "api_latency" {
  alarm_name          = "${var.name}-onyx-api-latency"
  alarm_description   = "p95 response time above two seconds for ten minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  threshold           = 2
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  dimensions          = { LoadBalancer = var.alb_suffix }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

# -------------------------------------------------------------- database -----
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${var.name}-onyx-rds-cpu"
  alarm_description   = "Sustained database CPU. Usually a query, occasionally a leak."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_storage" {
  alarm_name          = "${var.name}-onyx-rds-storage"
  alarm_description   = "Under 10 GB free. Autoscaling should have handled it; find out why it did not."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10737418240
  comparison_operator = "LessThanThreshold"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  alarm_name          = "${var.name}-onyx-rds-connections"
  alarm_description   = "Connection count near the instance ceiling."
  namespace           = "AWS/RDS"
  metric_name         = "DatabaseConnections"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.max_connections_alarm
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { DBInstanceIdentifier = var.db_identifier }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

# ----------------------------------------------------------------- cache -----
resource "aws_cloudwatch_metric_alarm" "cache_cpu" {
  alarm_name          = "${var.name}-onyx-valkey-cpu"
  alarm_description   = "Broker CPU sustained high; queues will back up next."
  namespace           = "AWS/ElastiCache"
  metric_name         = "EngineCPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 75
  comparison_operator = "GreaterThanThreshold"
  dimensions          = { ReplicationGroupId = var.cache_replication_group_id }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

# --------------------------------------------------------------- workers -----
# A scheduled integrity verification that stops failing loudly and starts
# failing silently is the worst case for a product whose claim is that sealed
# results can be replayed. The application logs a known string on failure; this
# turns that string into a page.
resource "aws_cloudwatch_log_metric_filter" "worker_failures" {
  name           = "${var.name}-worker-task-failures"
  log_group_name = var.log_group_name
  pattern        = "{ $.event = \"task_failed\" }"

  metric_transformation {
    name      = "WorkerTaskFailures"
    namespace = "Onyx/${var.name}"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_failures" {
  alarm_name          = "${var.name}-onyx-worker-failures"
  alarm_description   = "Celery tasks are failing. Check which queue before restarting anything."
  namespace           = "Onyx/${var.name}"
  metric_name         = "WorkerTaskFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_log_metric_filter" "email_failures" {
  name           = "${var.name}-email-send-failures"
  log_group_name = var.log_group_name
  pattern        = "{ $.event = \"email_send_failed\" }"

  metric_transformation {
    name      = "EmailSendFailures"
    namespace = "Onyx/${var.name}"
    value     = "1"
    unit      = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "email_failures" {
  alarm_name          = "${var.name}-onyx-email-failures"
  alarm_description   = "Recovery mail is not going out. Customers cannot get back in."
  namespace           = "Onyx/${var.name}"
  metric_name         = "EmailSendFailures"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 3
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = local.tags
}
