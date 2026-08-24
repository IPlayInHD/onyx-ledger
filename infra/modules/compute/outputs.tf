output "cluster_name" { value = aws_ecs_cluster.this.name }
output "cluster_arn" { value = aws_ecs_cluster.this.arn }
output "alb_dns_name" { value = aws_lb.this.dns_name }
output "alb_zone_id" { value = aws_lb.this.zone_id }
output "alb_arn" { value = aws_lb.this.arn }
output "ecr_repository_url" { value = aws_ecr_repository.this.repository_url }
output "ecr_repository_arn" { value = aws_ecr_repository.this.arn }
output "api_service_name" { value = aws_ecs_service.api.name }
output "migration_task_family" { value = aws_ecs_task_definition.migration.family }
output "log_group_name" { value = aws_cloudwatch_log_group.tasks.name }
output "task_role_arns" { value = { for k, r in aws_iam_role.task : k => r.arn } }

# The execution role must be passable too: every task definition names it, and
# a deploy that cannot pass it cannot register one.
output "passable_role_arns" {
  value = concat([for r in aws_iam_role.task : r.arn], [aws_iam_role.execution.arn])
}
output "target_group_arn_suffix" { value = aws_lb_target_group.api.arn_suffix }
output "alb_arn_suffix" { value = aws_lb.this.arn_suffix }
