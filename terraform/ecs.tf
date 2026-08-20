# ECS cluster, task definitions mirroring each docker-compose service, and the
# long-running services (backend, foreman). ECS tasks are placed on the EC2 Auto
# Scaling Group capacity provider in ecs_capacity.tf rather than Fargate. The
# worker task definition is registered but not run as a service: backend-spawned
# workers use ecs:RunTask against the same ASG-backed capacity provider.

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name = "${local.name_prefix}-cluster"
  }
}

resource "aws_cloudwatch_log_group" "service" {
  for_each = toset(local.ecr_services)

  name              = "/ecs/${local.name_prefix}/${each.key}"
  retention_in_days = var.log_retention_days

  tags = {
    Name    = "${local.name_prefix}-${each.key}-logs"
    Service = each.key
  }
}

# -----------------------------------------------------------------------------
# Backend — HTTP API + SPA, the only service fronted by the ALB.
# -----------------------------------------------------------------------------

resource "aws_ecs_task_definition" "backend" {
  family                   = "${local.name_prefix}-backend"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = var.backend_cpu
  memory                   = var.backend_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "backend"
      image     = "${aws_ecr_repository.service["backend"].repository_url}:${var.container_image_tag}"
      essential = true

      portMappings = [
        {
          containerPort = var.backend_container_port
          protocol      = "tcp"
        }
      ]

      # Migrations run in the separate one-off migrate task below (before the
      # services roll), so backend startup never gates on `alembic upgrade` and
      # a new task can come up alongside the old one during a deploy.
      command = ["pioneer", "serve"]

      environment = [
        { name = "GITHUB_REDIRECT_URI", value = var.frontend_url != "" ? "${var.frontend_url}/auth/github/callback" : "" },
        { name = "FRONTEND_URL", value = var.frontend_url },
        # GitHub App identity — the backend mints per-guild installation tokens so
        # comments/commits attribute to the bot. Private key rides in `secrets`.
        # Per-guild installation ids are stored in the DB, not here.
        { name = "GITHUB_APP_ID", value = var.github_app_id },
        { name = "GITHUB_APP_SLUG", value = var.github_app_slug },
        { name = "GITHUB_APP_INSTALLATION_ID", value = var.github_app_installation_id },
        # Spawned workers run as separate ECS tasks, so "localhost" would never
        # reach the backend — they connect back through the ALB.
        { name = "WORKER_BACKEND_URL", value = "${local.has_certificate ? "https" : "http"}://${var.domain_name != "" ? var.domain_name : aws_lb.main.dns_name}" },
        { name = "LOG_LEVEL", value = var.log_level },
        { name = "PIONEER_S3_BUCKET", value = aws_s3_bucket.assets.bucket },
        { name = "PIONEER_S3_PREFIX", value = "worker-sessions" },
        { name = "PIONEER_ASG_LIFECYCLE_TOKEN", value = random_password.asg_lifecycle_token.result },
        { name = "AWS_DEFAULT_REGION", value = local.region },
        # Worker dispatch via ECS RunTask (backend/worker_runtime.py): the
        # first two select ECS mode; the capacity provider keeps spawned
        # workers on the same ASG-backed ECS cluster as every other task; the
        # subnet/SG pair is the awsvpc network configuration each spawned
        # worker task launches with.
        { name = "ECS_CLUSTER_NAME", value = aws_ecs_cluster.main.name },
        { name = "ECS_WORKER_TASK_DEFINITION", value = "${local.name_prefix}-worker" },
        { name = "ECS_WORKER_CAPACITY_PROVIDER", value = aws_ecs_capacity_provider.asg.name },
        { name = "ECS_WORKER_SUBNETS", value = join(",", aws_subnet.private[*].id) },
        { name = "ECS_WORKER_SECURITY_GROUPS", value = aws_security_group.ecs_tasks.id },
        # The embedded foreman (backend/foreman/runner.py) makes LLM calls from
        # this process whenever no standalone foreman proxy is connected for a
        # guild — it needs the same provider config as the foreman task below.
        { name = "FOREMAN_MODEL", value = var.foreman_model },
        { name = "FOREMAN_PROVIDER", value = var.foreman_provider },
        { name = "FOREMAN_BEDROCK_MODEL", value = var.foreman_bedrock_model },
        # Discord integration — mirrors the docker-compose backend service.
        # DISCORD_BOT_TOKEN rides in `secrets` below.
        { name = "DISCORD_CHANNEL_ID", value = var.discord_channel_id },
        { name = "DISCORD_APPLICATION_ID", value = var.discord_application_id },
        { name = "DISCORD_PUBLIC_KEY", value = var.discord_public_key },
        { name = "DISCORD_ALLOWED_ROLE_IDS", value = var.discord_allowed_role_ids },
        { name = "DISCORD_PIONEER_GUILD_SLUG", value = var.discord_pioneer_guild_slug },
        { name = "DISCORD_STREAM_TASKS", value = var.discord_stream_tasks },
        { name = "DISCORD_GATEWAY_ENABLED", value = var.discord_gateway_enabled },
        { name = "DISCORD_DEV_GUILD_ID", value = var.discord_dev_guild_id },
        { name = "DISCORD_PR_DEBOUNCE_SECONDS", value = var.discord_pr_debounce_seconds },
        # Coalesces bursty GitHub webhook events (CI check_run/check_suite/status
        # especially) before each batch triggers a foreman run — a major foreman
        # token-cost lever. Default 30s in code; raised here to cut CI-driven runs.
        { name = "WEBHOOK_DEBOUNCE_SECONDS", value = var.webhook_debounce_seconds },
      ]

      secrets = [
        for key, param in {
          DATABASE_URL           = aws_ssm_parameter.database_url
          GITHUB_CLIENT_ID       = aws_ssm_parameter.secret["github_client_id"]
          GITHUB_CLIENT_SECRET   = aws_ssm_parameter.secret["github_client_secret"]
          GITHUB_TOKEN           = aws_ssm_parameter.secret["github_token"]
          GITHUB_APP_PRIVATE_KEY = aws_ssm_parameter.secret["github_app_private_key"]
          ANTHROPIC_API_KEY      = aws_ssm_parameter.secret["anthropic_api_key"]
          PIONEER_FOREMAN_KEY    = aws_ssm_parameter.secret["pioneer_foreman_key"]
          DISCORD_BOT_TOKEN      = aws_ssm_parameter.secret["discord_bot_token"]
          DEBUG_TOKEN            = aws_ssm_parameter.secret["debug_token"]
        } : { name = key, valueFrom = param.arn }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service["backend"].name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "backend"
        }
      }
    }
  ])

  tags = {
    Name    = "${local.name_prefix}-backend"
    Service = "backend"
  }
}

resource "aws_ecs_service" "backend" {
  name            = "${local.name_prefix}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.backend_desired_count

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.asg.name
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend.arn
    container_name   = "backend"
    container_port   = var.backend_container_port
  }

  depends_on = [aws_lb_listener.http, aws_ecs_cluster_capacity_providers.main]

  tags = {
    Name    = "${local.name_prefix}-backend"
    Service = "backend"
  }
}

# -----------------------------------------------------------------------------
# Migrate — one-off release task (no service). deploy.yml registers this at
# the new image tag and runs it to completion via ecs:RunTask BEFORE the full
# terraform apply rolls the services: the schema is upgraded first, then the
# new backend comes up next to the old one and ECS tears the old one down
# only once the new one is healthy.
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/ecs/${local.name_prefix}/migrate"
  retention_in_days = var.log_retention_days

  tags = {
    Name    = "${local.name_prefix}-migrate-logs"
    Service = "migrate"
  }
}

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name_prefix}-migrate"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "migrate"
      image     = "${aws_ecr_repository.service["backend"].repository_url}:${var.container_image_tag}"
      essential = true

      command = ["sh", "-c", "cd backend && alembic upgrade head"]

      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_ssm_parameter.database_url.arn }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.migrate.name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "migrate"
        }
      }
    }
  ])

  tags = {
    Name    = "${local.name_prefix}-migrate"
    Service = "migrate"
  }
}

# -----------------------------------------------------------------------------
# Foreman — background AI loop (WebSocket client to the backend), no
# inbound networking. Matches the `profiles: ["foreman"]` compose service:
# desired_count defaults to 0 until var.guild_id is configured.
# -----------------------------------------------------------------------------

resource "aws_ecs_task_definition" "foreman" {
  family                   = "${local.name_prefix}-foreman"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = var.foreman_cpu
  memory                   = var.foreman_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "foreman"
      image     = "${aws_ecr_repository.service["foreman"].repository_url}:${var.container_image_tag}"
      essential = true
      command   = ["pioneer", "foreman"]

      environment = [
        # wss:// when an HTTPS listener exists: the HTTP listener 301-redirects
        # everything to HTTPS in that case (see alb.tf), and a plain ws://
        # upgrade request cannot follow an HTTP redirect.
        { name = "BACKEND_WS_URL", value = "${local.has_certificate ? "wss" : "ws"}://${aws_lb.main.dns_name}" },
        { name = "GUILD_ID", value = var.guild_id },
        { name = "LOG_LEVEL", value = var.log_level },
        { name = "FOREMAN_MODEL", value = var.foreman_model },
        { name = "FOREMAN_PROVIDER", value = var.foreman_provider },
        { name = "FOREMAN_BEDROCK_MODEL", value = var.foreman_bedrock_model },
        { name = "AWS_DEFAULT_REGION", value = local.region },
      ]

      secrets = [
        for key, param in {
          ANTHROPIC_API_KEY        = aws_ssm_parameter.secret["anthropic_api_key"]
          OPENAI_API_KEY           = aws_ssm_parameter.secret["openai_api_key"]
          AWS_BEARER_TOKEN_BEDROCK = aws_ssm_parameter.secret["aws_bearer_token_bedrock"]
        } : { name = key, valueFrom = param.arn }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service["foreman"].name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "foreman"
        }
      }
    }
  ])

  tags = {
    Name    = "${local.name_prefix}-foreman"
    Service = "foreman"
  }
}

resource "aws_ecs_service" "foreman" {
  name            = "${local.name_prefix}-foreman"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.foreman.arn
  desired_count   = var.foreman_desired_count

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.asg.name
    weight            = 1
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  depends_on = [aws_ecs_cluster_capacity_providers.main]

  tags = {
    Name    = "${local.name_prefix}-foreman"
    Service = "foreman"
  }
}

# -----------------------------------------------------------------------------
# Worker — registered task definition only (no aws_ecs_service). Launched
# on demand by the backend via ecs:RunTask against this family, replacing
# the docker.from_env()/containers.run() calls in backend/foreman/tools.py and
# backend/routes/workers.py. RunTask uses the same ASG-backed ECS capacity
# provider as backend/foreman/metabase/migrate.
# -----------------------------------------------------------------------------

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["EC2"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  # Cloned repos + git worktrees live on the ECS capacity instance's root EBS
  # volume (var.ecs_capacity_root_volume_gib). Fargate-only ephemeral_storage
  # is intentionally not set for this EC2 task definition.

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = "${aws_ecr_repository.service["worker"].repository_url}:${var.container_image_tag}"
      essential = true

      environment = [
        { name = "WORKER_LOG_LEVEL", value = var.log_level },
        { name = "PIONEER_S3_BUCKET", value = aws_s3_bucket.assets.bucket },
        { name = "PIONEER_S3_PREFIX", value = "worker-sessions" },
        { name = "AWS_DEFAULT_REGION", value = local.region },
      ]

      # No `secrets` block here on purpose. Worker credentials (GitHub token,
      # Claude OAuth token, provider API keys) are per-guild, so the worker
      # fetches them from the backend on startup over its authenticated
      # connection — /auth/github/token, /auth/claude/credentials, and the
      # per-guild /guilds/{id}/foreman/env-vars (see the worker's
      # _fetch_github_token_if_needed / _check_claude_auth / _fetch_guild_env_vars).
      #
      # Pinning them here as deploy-wide SSM secrets would be actively harmful:
      # ECS won't let a RunTask container override replace a value a secret
      # already binds (aws/containers-roadmap#1269), and the worker skips its
      # per-guild fetch whenever the variable is already set in its environment
      # — so a baked-in secret would shadow the correct per-guild credential.

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.service["worker"].name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])

  tags = {
    Name    = "${local.name_prefix}-worker"
    Service = "worker"
  }
}
