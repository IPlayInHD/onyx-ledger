# =============================================================================
# SERVICES — one image, five commands
# =============================================================================

locals {
  # Which queues each worker drains, and which database identity it drains them
  # as. Taken from `workers/celery_app.py`'s task_routes — if a queue is added
  # there and not here, its tasks sit unconsumed, so the two must be read
  # together.
  workers = {
    "worker-app" = {
      queues      = "analysis,documents,ingestion,notify,maintenance,tkms_parse,tkms_extract,tkms_validate,tkms_compare,tkms_index,ioe"
      db_secret   = "api"
      cpu         = var.worker_app_cpu
      memory      = var.worker_app_memory
      count       = var.worker_app_count
      concurrency = var.worker_app_concurrency
    }
    "worker-freshness" = {
      queues      = "ioe_freshness,ioe_integrity"
      db_secret   = "freshness"
      cpu         = var.worker_freshness_cpu
      memory      = var.worker_freshness_memory
      count       = var.worker_freshness_count
      concurrency = var.worker_freshness_concurrency
    }
    "worker-privacy" = {
      queues      = "privacy"
      db_secret   = "privacy"
      cpu         = var.worker_privacy_cpu
      memory      = var.worker_privacy_memory
      count       = var.worker_privacy_count
      concurrency = var.worker_privacy_concurrency # deletion is serialised on purpose
    }
  }

  # WHY THE POOL IS CAPPED PER ROLE.
  #
  # Every uvicorn worker and every Celery child builds its own SQLAlchemy
  # engine, so the connections an environment can open is
  # (pool + overflow) x processes x tasks — not (pool + overflow). At the
  # application defaults (10 + 20) that multiplies out to 423 potential
  # backends against a db.t4g.small's 225, and the failure mode is the database
  # refusing connections during a burst.
  #
  # These are ENVIRONMENT variables, not a code change: `ONYX_DB_POOL_SIZE` and
  # `ONYX_DB_MAX_OVERFLOW` are ordinary pydantic settings. modules/capacity
  # computes the resulting ceiling and refuses to plan if it exceeds what the
  # chosen instance class allows.
  #
  # Measured: the API held 23 backends across two uvicorn workers while serving
  # 4 000 authenticated requests at 534 rps — about 12 per process against a cap
  # of 20. The cap is headroom over a load far above launch volume, not a
  # squeeze on the working set.
  api_pool_env = [
    { name = "ONYX_DB_POOL_SIZE", value = tostring(var.api_pool_size) },
    { name = "ONYX_DB_MAX_OVERFLOW", value = tostring(var.api_pool_overflow) },
  ]
  worker_pool_env = [
    { name = "ONYX_DB_POOL_SIZE", value = tostring(var.worker_pool_size) },
    { name = "ONYX_DB_MAX_OVERFLOW", value = tostring(var.worker_pool_overflow) },
  ]

  # Secrets injected into every task that talks to the application database.
  # `ONYX_PRIVACY_DATABASE_URL` and `ONYX_FRESHNESS_DATABASE_URL` are read by
  # `privacy_session.py` for the privileged keyholes; a task that is not the
  # privacy or freshness worker is not given them.
  base_secrets = [
    { name = "ONYX_JWT_SECRET", valueFrom = var.jwt_secret_arn },
    { name = "ONYX_ADMISSION_IDENTITY_SECRET", valueFrom = var.admission_secret_arn },
    { name = "ONYX_REDIS_URL", valueFrom = var.redis_url_arn },
  ]
}

# ------------------------------------------------------------------- API -----
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-onyx-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task["api"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64" # Graviton: same work, cheaper
  }

  container_definitions = jsonencode([{
    name      = "api"
    image     = var.image_uri
    essential = true

    portMappings = [{ containerPort = var.api_port, protocol = "tcp" }]

    command = [
      "uvicorn", "app.main:app",
      "--host", "0.0.0.0",
      "--port", tostring(var.api_port),
      "--workers", tostring(var.api_uvicorn_workers),
      # Graceful termination: finish in-flight requests before exiting, so a
      # deployment does not sever a customer's analysis mid-response.
      "--timeout-graceful-shutdown", "25",
    ]

    environment = concat(local.common_environment, local.api_pool_env)
    secrets = concat(local.base_secrets, [
      { name = "ONYX_DATABASE_URL", valueFrom = var.db_secret_arns["api"] },
    ])

    # The container's own check, distinct from the load balancer's. This one is
    # about the process; the target group's is about serving traffic.
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${var.api_port}/healthz', timeout=3).status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }

    stopTimeout = 30
  }])

  tags = local.tags
}

resource "aws_ecs_service" "api" {
  name            = "api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.tasks_security_group_id]
    assign_public_ip = false # private subnets; egress is via NAT
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_port
  }

  health_check_grace_period_seconds = 60
  enable_execute_command            = var.enable_execute_command

  # Roll forward only if the new tasks stay healthy; roll back automatically if
  # they do not. A deployment that half-lands is worse than one that does not.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  # The image digest moves with each deployment, applied by the pipeline rather
  # than by Terraform — so a `terraform apply` never silently reverts the
  # running revision to whatever the variable said last.
  lifecycle { ignore_changes = [task_definition, desired_count] }

  tags = local.tags
}

# THE HARD CEILING.
#
# `max_capacity` is what autoscaling may reach. The precondition is what stops a
# profile from ever asking for more than the estate is willing to pay for: a
# mistyped maximum fails the plan instead of becoming a bill. §11 asks for a
# capped autoscaling maximum; this is that cap, expressed where it cannot be
# skipped rather than in a comment.
resource "aws_appautoscaling_target" "api" {
  max_capacity       = var.api_max_count
  min_capacity       = var.api_count
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"

  lifecycle {
    precondition {
      condition     = var.api_max_count <= var.api_absolute_max_count
      error_message = "api_max_count ${var.api_max_count} exceeds the absolute ceiling ${var.api_absolute_max_count}."
    }
    precondition {
      condition     = var.api_count <= var.api_max_count
      error_message = "api_count ${var.api_count} exceeds api_max_count ${var.api_max_count}."
    }
  }
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${var.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification { predefined_metric_type = "ECSServiceAverageCPUUtilization" }
    target_value       = 65
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}

# --------------------------------------------------------------- workers -----
resource "aws_ecs_task_definition" "worker" {
  for_each                 = local.workers
  family                   = "${var.name}-onyx-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task[each.key].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = each.key
    image     = var.image_uri
    essential = true

    command = [
      "celery", "-A", "workers.celery_app", "worker",
      "--queues", each.value.queues,
      "--concurrency", tostring(each.value.concurrency),
      "--loglevel", "INFO",
    ]

    environment = concat(local.common_environment, local.worker_pool_env)
    secrets = concat(local.base_secrets, [
      { name = "ONYX_DATABASE_URL", valueFrom = var.db_secret_arns[each.value.db_secret] },
      ], each.key == "worker-privacy" ? [
      { name = "ONYX_PRIVACY_DATABASE_URL", valueFrom = var.db_secret_arns["privacy"] },
      ] : [], each.key == "worker-freshness" ? [
      { name = "ONYX_FRESHNESS_DATABASE_URL", valueFrom = var.db_secret_arns["freshness"] },
    ] : [])

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = each.key
      }
    }

    # Long enough for `acks_late` to finish the task in hand rather than
    # re-queueing work that was nearly done.
    stopTimeout = 120
  }])

  tags = merge(local.tags, { Workload = each.key })
}

resource "aws_ecs_service" "worker" {
  for_each        = local.workers
  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker[each.key].arn
  desired_count   = each.value.count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.tasks_security_group_id]
    assign_public_ip = false
  }

  enable_execute_command = var.enable_execute_command

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle { ignore_changes = [task_definition, desired_count] }

  tags = merge(local.tags, { Workload = each.key })
}

# ------------------------------------------------------------------ beat -----
# EXACTLY ONE TASK. Two beats means every scheduled job fires twice, and the
# integrity verifier firing twice is a doubled replay load against sealed
# artefacts. `deployment_minimum_healthy_percent = 0` with a maximum of 100 is
# what makes a deployment stop the old one BEFORE starting the new one.
resource "aws_ecs_task_definition" "beat" {
  family                   = "${var.name}-onyx-beat"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.beat_cpu
  memory                   = var.beat_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task["beat"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "beat"
    image     = var.image_uri
    essential = true
    command   = ["celery", "-A", "workers.celery_app", "beat", "--loglevel", "INFO"]

    environment = concat(local.common_environment, [
      { name = "ONYX_DB_POOL_SIZE", value = "2" },
      { name = "ONYX_DB_MAX_OVERFLOW", value = "2" },
    ])
    secrets = local.base_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "beat"
      }
    }
  }])

  tags = local.tags
}

resource "aws_ecs_service" "beat" {
  name            = "beat"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.beat.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.tasks_security_group_id]
    assign_public_ip = false
  }

  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0

  lifecycle { ignore_changes = [task_definition] }

  tags = local.tags
}

# ------------------------------------------------------------- migration -----
# Not a service. The pipeline calls `run-task` with this definition and waits
# for exit code 0 before it will update anything else. §15: migration failure
# stops the deployment, and nothing rolls a destructive change back on its own.
resource "aws_ecs_task_definition" "migration" {
  family                   = "${var.name}-onyx-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.migration_cpu
  memory                   = var.migration_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task["migration"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "migration"
    image     = var.image_uri
    essential = true
    command   = ["python", "-m", "alembic", "upgrade", "head"]

    environment = concat(local.common_environment, local.worker_pool_env)
    # The MIGRATOR's credentials, and only here. No serving task is given them.
    secrets = [
      { name = "ONYX_DATABASE_URL_SYNC", valueFrom = var.db_secret_arns["migrator"] },
      { name = "ONYX_DATABASE_URL", valueFrom = var.db_secret_arns["api"] },
      { name = "ONYX_JWT_SECRET", valueFrom = var.jwt_secret_arn },
      { name = "ONYX_ADMISSION_IDENTITY_SECRET", valueFrom = var.admission_secret_arn },
      { name = "ONYX_REDIS_URL", valueFrom = var.redis_url_arn },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.tasks.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "migration"
      }
    }
  }])

  tags = local.tags
}
