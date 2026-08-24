output "distribution_domain_name" { value = module.edge.distribution_domain_name }
output "frontend_bucket" { value = module.edge.frontend_bucket }
output "ecr_repository_url" { value = module.compute.ecr_repository_url }
output "cluster_name" { value = module.compute.cluster_name }
output "migration_task_family" { value = module.compute.migration_task_family }
output "deploy_role_arn" { value = module.github_oidc.deploy_role_arn }
output "database_endpoint" {
  value     = module.database.endpoint
  sensitive = true
}
