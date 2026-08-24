output "profile" { value = var.profile }

output "single_nat_gateway" { value = local.p.single_nat_gateway }

output "db_instance_class" { value = local.p.db_instance_class }
output "db_multi_az" { value = local.p.db_multi_az }
output "db_allocated_storage" { value = local.p.db_allocated_storage }
output "db_max_allocated_storage" { value = local.p.db_max_allocated_storage }
output "db_backup_retention_days" { value = local.p.db_backup_retention_days }

output "cache_node_type" { value = local.p.cache_node_type }
output "cache_node_count" { value = local.p.cache_node_count }

output "api_cpu" { value = local.p.api_cpu }
output "api_memory" { value = local.p.api_memory }
output "api_count" { value = local.p.api_count }
output "api_max_count" { value = local.p.api_max_count }
output "api_uvicorn_workers" { value = local.p.api_uvicorn_workers }
output "api_pool_size" { value = local.p.api_pool_size }
output "api_pool_overflow" { value = local.p.api_pool_overflow }

output "worker_app_cpu" { value = local.p.worker_app_cpu }
output "worker_app_memory" { value = local.p.worker_app_memory }
output "worker_app_count" { value = local.p.worker_app_count }
output "worker_app_concurrency" { value = local.p.worker_app_concurrency }

output "worker_freshness_cpu" { value = local.p.worker_freshness_cpu }
output "worker_freshness_memory" { value = local.p.worker_freshness_memory }
output "worker_freshness_count" { value = local.p.worker_freshness_count }
output "worker_freshness_concurrency" { value = local.p.worker_freshness_concurrency }

output "worker_privacy_cpu" { value = local.p.worker_privacy_cpu }
output "worker_privacy_memory" { value = local.p.worker_privacy_memory }
output "worker_privacy_count" { value = local.p.worker_privacy_count }
output "worker_privacy_concurrency" { value = local.p.worker_privacy_concurrency }

output "worker_pool_size" { value = local.p.worker_pool_size }
output "worker_pool_overflow" { value = local.p.worker_pool_overflow }

output "beat_cpu" { value = local.p.beat_cpu }
output "beat_memory" { value = local.p.beat_memory }

output "migration_cpu" { value = local.p.migration_cpu }
output "migration_memory" { value = local.p.migration_memory }

output "log_retention_days" { value = local.p.log_retention_days }
output "monthly_budget_usd" { value = local.p.monthly_budget_usd }

output "connection_ceiling" {
  description = "Arithmetic maximum PostgreSQL backends this profile can open."
  value       = local.connection_ceiling
}

output "connection_room" {
  description = "What the chosen instance class allows, after headroom."
  value       = local.connection_room
}
