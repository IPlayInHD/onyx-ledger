# =============================================================================
# CAPACITY PROFILES — every sizing decision, in one place, with its evidence
# =============================================================================
# This module creates NOTHING. It is a lookup table with arithmetic, so that
# "how big is this environment" is a single reviewable value rather than two
# dozen numbers scattered across two environment roots.
#
# WHAT A PROFILE MAY CHANGE:  capacity and redundancy.
# WHAT A PROFILE MAY NEVER CHANGE:  which services exist, which database
#   identity each one runs as, whether the database is public, whether the data
#   subnet has a default route, whether RLS is forced, or whether production
#   fails closed. Those are properties of the architecture and are asserted by
#   tests/security/test_capacity_profiles.py in BOTH profiles.
#
# SIZES ARE MEASURED, NOT GUESSED. The numbers below come from running the real
# API and the real Celery workers under a representative workload and reading
# RSS, CPU and PostgreSQL connection counts. The measurement and its refutations
# are written up in docs/operations/cost-model.md; the short version is:
#
#   API task, 4 000 authenticated requests at 534 rps
#       peak RSS 329.6 MB · peak 1.90 vCPU · peak 23 backends
#   worker-app, 400 real tasks
#       concurrency 4  peak RSS 843.2 MB
#       concurrency 2  peak RSS 507.7 MB · peak 1.50 vCPU
#       concurrency 1  peak RSS 316.5 MB · peak 0.82 vCPU
#   beat, idle scheduler
#       peak RSS 98.6 MB · 0.00 vCPU · 0 backends
#
# The "workers fit in 0.5 GB" hypothesis is REFUTED by that table: at
# concurrency 2 a worker peaked at 99% of a 512 MiB limit running the CHEAPEST
# scheduled task in the product. Fargate enforces the memory limit by killing
# the task, and a task killed mid-erasure is not a saving. So workers get
# 1 GiB in both profiles and only their vCPU differs.

locals {
  # RDS default `max_connections` is LEAST({DBInstanceClassMemory/9531392}, 5000).
  # These are that formula evaluated at each class's memory, which is what the
  # published per-class figures come to.
  db_max_connections = {
    "db.t4g.micro"  = 112
    "db.t4g.small"  = 225
    "db.t4g.medium" = 450
    "db.t4g.large"  = 901
    "db.m7g.large"  = 901
  }

  profiles = {
    # -----------------------------------------------------------------------
    lean_launch = {
      # --- network ---
      single_nat_gateway = true

      # --- database ---
      db_instance_class        = "db.t4g.small"
      db_multi_az              = false
      db_allocated_storage     = 20
      db_max_allocated_storage = 100
      db_backup_retention_days = 7

      # --- cache ---
      cache_node_type  = "cache.t4g.micro"
      cache_node_count = 1

      # --- api ---
      api_cpu             = 512  # 0.5 vCPU
      api_memory          = 1024 # measured peak 329.6 MB
      api_count           = 1
      api_max_count       = 2
      api_uvicorn_workers = 2
      api_pool_size       = 10
      api_pool_overflow   = 10

      # --- workers ---
      # worker-app serves the queues a CUSTOMER waits on (analysis, documents,
      # ingestion). It keeps 0.5 vCPU so a user-visible analysis does not get
      # six times slower to save $6.49 a month. The two background workers do
      # not have a person waiting on them and run at 0.25.
      worker_app_cpu         = 512
      worker_app_memory      = 1024
      worker_app_count       = 1
      worker_app_concurrency = 2

      worker_freshness_cpu         = 256
      worker_freshness_memory      = 1024
      worker_freshness_count       = 1
      worker_freshness_concurrency = 2

      worker_privacy_cpu         = 256
      worker_privacy_memory      = 1024
      worker_privacy_count       = 1
      worker_privacy_concurrency = 1

      worker_pool_size     = 5
      worker_pool_overflow = 5

      # --- beat ---
      beat_cpu    = 256
      beat_memory = 512 # measured peak 98.6 MB

      # --- migration (transient, runs once per deploy) ---
      migration_cpu    = 512
      migration_memory = 1024

      # --- observability ---
      log_retention_days = 30

      # --- cost guardrails ---
      monthly_budget_usd     = 250
      db_connection_headroom = 0.85
    }

    # -----------------------------------------------------------------------
    # PRESERVED, NOT DELETED. This is the profile to move to when the scale-up
    # triggers in docs/operations/cost-model.md fire. It is exercised by
    # `terraform validate` in the production root's test fixtures and asserted
    # to still exist by tests/security/test_capacity_profiles.py.
    high_availability = {
      single_nat_gateway = false

      db_instance_class        = "db.m7g.large"
      db_multi_az              = true
      db_allocated_storage     = 100
      db_max_allocated_storage = 500
      db_backup_retention_days = 35

      cache_node_type  = "cache.t4g.small"
      cache_node_count = 2

      api_cpu             = 1024
      api_memory          = 2048
      api_count           = 2
      api_max_count       = 6
      api_uvicorn_workers = 2
      api_pool_size       = 10
      api_pool_overflow   = 20

      worker_app_cpu         = 1024
      worker_app_memory      = 2048
      worker_app_count       = 2
      worker_app_concurrency = 4

      worker_freshness_cpu         = 1024
      worker_freshness_memory      = 2048
      worker_freshness_count       = 2
      worker_freshness_concurrency = 2

      worker_privacy_cpu         = 1024
      worker_privacy_memory      = 2048
      worker_privacy_count       = 2
      worker_privacy_concurrency = 1

      worker_pool_size     = 8
      worker_pool_overflow = 8

      beat_cpu    = 512
      beat_memory = 1024

      migration_cpu    = 1024
      migration_memory = 2048

      log_retention_days = 90

      monthly_budget_usd     = 1200
      db_connection_headroom = 0.85
    }
  }

  p = local.profiles[var.profile]

  # ---------------------------------------------------------------------------
  # THE CONNECTION CEILING
  # ---------------------------------------------------------------------------
  # Not a guess at usage — the ARITHMETIC MAXIMUM this configuration can open.
  # Every uvicorn worker and every Celery child process builds its own engine,
  # so the ceiling is (pool + overflow) x processes x tasks.
  #
  # WHY THIS IS COMPUTED RATHER THAN TRUSTED. The defaults (10 + 20 per process)
  # multiply out to 423 potential backends against a db.t4g.small's 225. Nothing
  # in Terraform or in the application would have said so; the database would
  # simply have started refusing connections under load, which reads as an
  # application outage rather than as a sizing mistake.
  api_conn      = (local.p.api_pool_size + local.p.api_pool_overflow) * local.p.api_uvicorn_workers * local.p.api_max_count
  worker_conn   = (local.p.worker_pool_size + local.p.worker_pool_overflow)
  app_conn      = local.worker_conn * (local.p.worker_app_concurrency + 1) * local.p.worker_app_count
  fresh_conn    = local.worker_conn * (local.p.worker_freshness_concurrency + 1) * local.p.worker_freshness_count
  privacy_conn  = local.worker_conn * (local.p.worker_privacy_concurrency + 1) * local.p.worker_privacy_count
  beat_conn     = 4
  migrate_conn  = local.worker_conn
  reserved_conn = 3 # rds_superuser reserved connections

  connection_ceiling = (
    local.api_conn + local.app_conn + local.fresh_conn +
    local.privacy_conn + local.beat_conn + local.migrate_conn + local.reserved_conn
  )

  db_max_conn     = local.db_max_connections[local.p.db_instance_class]
  connection_room = floor(local.db_max_conn * local.p.db_connection_headroom)
}

# The guard. A profile that could exhaust its own database refuses to plan.
resource "terraform_data" "connection_ceiling_guard" {
  input = local.connection_ceiling

  lifecycle {
    precondition {
      condition = local.connection_ceiling <= local.connection_room
      error_message = format(
        "profile %s can open up to %d PostgreSQL backends but %s allows only %d (%d%% of %d). Lower a pool size, a concurrency, or a task maximum — or move to a larger instance class.",
        var.profile, local.connection_ceiling, local.p.db_instance_class,
        local.connection_room, local.p.db_connection_headroom * 100, local.db_max_conn,
      )
    }
  }
}
