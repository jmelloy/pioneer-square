# Metabase — on-demand BI tool, fronted by the existing ALB on a subdomain.
#
# "Scaled to nothing" by default: metabase_desired_count = 0, so there is no
# running task (and no Fargate cost) at rest. Start it on demand by setting the
# count to 1 (see the `metabase-up`/`metabase-down` note in README / the CLI
# one-liners), stop it by setting it back to 0. The ALB rule, target group,
# cert, and log group are all free while idle.
#
# It reuses the existing RDS instance (a separate `metabase` database created
# out-of-band) for its own metadata, so there is no second database to run.
#
# ponytail: no request-driven wake-from-zero — ECS/Fargate can't natively
# scale UP from 0 tasks (nothing exists to trip a metric), so start/stop is a
# manual count toggle. Add a scheduled scale-down (nightly -> 0) or an
# EventBridge "wake" lambda only if the manual toggle proves annoying.

variable "metabase_desired_count" {
  description = "Metabase ECS desired count. 0 = off (scaled to nothing), 1 = running. This is the on/off knob."
  type        = number
  default     = 0
}

variable "metabase_image" {
  # ponytail: floating tag. A restart could pull a newer Metabase — fine for an
  # on-demand tool. Pin to a specific vX.Y.Z if a surprise upgrade ever bites.
  description = "Metabase container image."
  type        = string
  default     = "metabase/metabase:latest"
}

locals {
  metabase_fqdn = "metabase.${var.domain_name}"
}

# --- TLS: its own cert (a SAN on the prod cert would force that single cert to
# re-validate and risk the main site). DNS-validated manually on Linode. ---
resource "aws_acm_certificate" "metabase" {
  domain_name       = local.metabase_fqdn
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${local.name_prefix}-metabase-cert" }
}

# Attach to the existing HTTPS listener as an extra cert (SNI picks it by host).
# https listener always exists in this deployment (domain_name is set).
resource "aws_lb_listener_certificate" "metabase" {
  listener_arn    = aws_lb_listener.https[0].arn
  certificate_arn = aws_acm_certificate.metabase.arn
}

# --- SG: ALB -> Metabase on 3000. The task ALSO joins the ecs_tasks SG (below)
# so RDS, which already trusts ecs_tasks, needs no edit. ---
resource "aws_security_group" "metabase" {
  name        = "${local.name_prefix}-metabase-sg"
  description = "Allow inbound from the ALB to the Metabase task on 3000"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Metabase Jetty port from ALB"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-metabase-sg" }
}

resource "aws_lb_target_group" "metabase" {
  name        = "${local.name_prefix}-metabase"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/api/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }

  deregistration_delay = 30

  tags = { Name = "${local.name_prefix}-metabase-tg" }
}

resource "aws_lb_listener_rule" "metabase" {
  listener_arn = aws_lb_listener.https[0].arn

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.metabase.arn
  }

  condition {
    host_header {
      values = [local.metabase_fqdn]
    }
  }

  tags = { Name = "${local.name_prefix}-metabase-rule" }
}

resource "aws_cloudwatch_log_group" "metabase" {
  name              = "/ecs/${local.name_prefix}/metabase"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name_prefix}-metabase-logs" }
}

resource "aws_ecs_task_definition" "metabase" {
  family                   = "${local.name_prefix}-metabase"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024 # ponytail: 1 vCPU / 2GB is Metabase's comfortable floor; bump if it OOMs.
  memory                   = 2048
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "metabase"
      image     = var.metabase_image
      essential = true

      portMappings = [{ containerPort = 3000, protocol = "tcp" }]

      environment = [
        { name = "MB_DB_TYPE", value = "postgres" },
        { name = "MB_DB_DBNAME", value = "metabase" },
        { name = "MB_DB_HOST", value = aws_db_instance.postgres.address },
        { name = "MB_DB_PORT", value = "5432" },
        { name = "MB_DB_USER", value = var.db_username },
        { name = "MB_SITE_URL", value = "https://${local.metabase_fqdn}" },
      ]

      secrets = [
        { name = "MB_DB_PASS", valueFrom = aws_ssm_parameter.secret["db_password"].arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.metabase.name
          "awslogs-region"        = local.region
          "awslogs-stream-prefix" = "metabase"
        }
      }
    }
  ])

  tags = { Name = "${local.name_prefix}-metabase" }
}

resource "aws_ecs_service" "metabase" {
  name            = "${local.name_prefix}-metabase"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.metabase.arn
  desired_count   = var.metabase_desired_count
  launch_type     = "FARGATE"

  # Metabase takes ~1-2 min to boot before /api/health passes.
  health_check_grace_period_seconds = 180

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.metabase.id, aws_security_group.ecs_tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.metabase.arn
    container_name   = "metabase"
    container_port   = 3000
  }

  # desired_count is the on/off toggle, flipped 0<->1 via `aws ecs
  # update-service` on demand. Ignore it here so a later `terraform apply`
  # doesn't yank a running Metabase back to 0 (or vice versa).
  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = { Name = "${local.name_prefix}-metabase" }
}

output "metabase_url" {
  description = "Metabase URL once DNS + cert are in place and the service is scaled to 1."
  value       = "https://${local.metabase_fqdn}"
}

output "metabase_cert_validation" {
  description = "DNS record to add on Linode to validate the Metabase cert."
  value       = [for o in aws_acm_certificate.metabase.domain_validation_options : { name = o.resource_record_name, type = o.resource_record_type, value = o.resource_record_value }]
}
