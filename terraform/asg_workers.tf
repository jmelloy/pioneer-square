# Worker fleet — Auto Scaling Group of ARM64 (Graviton2) t3g.medium EC2
# instances, each running the `worker` container long-running (mirrors the
# docker-compose `worker` service), replacing a fixed always-on pool that
# would otherwise have to be provisioned as on-demand ECS Fargate tasks. See
# terraform/README.md "Worker fleet (ASG)" for the design and the tradeoff
# against the on-demand `ecs:RunTask` dispatch path that remains in ecs.tf.

# -----------------------------------------------------------------------------
# AMI — latest Amazon Linux 2023 arm64 (Graviton), owned by Amazon.
# -----------------------------------------------------------------------------

data "aws_ami" "worker_arm" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# -----------------------------------------------------------------------------
# IAM — EC2 instance role. Narrower than the ECS task role (iam.tf): no
# worker-dispatch or Bedrock permissions, since these instances only ever run
# the worker image, not the backend/foreman.
# -----------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "asg_worker" {
  name               = "${local.name_prefix}-asg-worker"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = {
    Name = "${local.name_prefix}-asg-worker"
  }
}

# Session Manager access for shell/debugging without a bastion or SSH key pair.
resource "aws_iam_role_policy_attachment" "asg_worker_ssm" {
  role       = aws_iam_role.asg_worker.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# --- ECR: pull the worker image at boot (and on instance refresh). ---
data "aws_iam_policy_document" "asg_worker_ecr" {
  statement {
    sid       = "ECRAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken does not support resource-level scoping.
  }

  statement {
    sid    = "ECRPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [aws_ecr_repository.service["worker"].arn]
  }
}

resource "aws_iam_role_policy" "asg_worker_ecr" {
  name   = "${local.name_prefix}-asg-worker-ecr"
  role   = aws_iam_role.asg_worker.id
  policy = data.aws_iam_policy_document.asg_worker_ecr.json
}

# --- S3: session-log/asset bucket, same scope as the ECS task role (iam.tf). ---
data "aws_iam_policy_document" "asg_worker_s3" {
  statement {
    sid       = "ListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.assets.arn]
  }

  statement {
    sid    = "ReadWriteObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.assets.arn}/*"]
  }
}

resource "aws_iam_role_policy" "asg_worker_s3" {
  name   = "${local.name_prefix}-asg-worker-s3"
  role   = aws_iam_role.asg_worker.id
  policy = data.aws_iam_policy_document.asg_worker_s3.json
}

# --- CloudWatch Logs: the `docker run --log-driver awslogs` container logs. ---
data "aws_iam_policy_document" "asg_worker_logs" {
  statement {
    sid    = "WorkerContainerLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.worker_asg.arn}:*"]
  }
}

resource "aws_iam_role_policy" "asg_worker_logs" {
  name   = "${local.name_prefix}-asg-worker-logs"
  role   = aws_iam_role.asg_worker.id
  policy = data.aws_iam_policy_document.asg_worker_logs.json
}

resource "aws_iam_instance_profile" "asg_worker" {
  name = "${local.name_prefix}-asg-worker"
  role = aws_iam_role.asg_worker.name
}

resource "aws_cloudwatch_log_group" "worker_asg" {
  name              = "/ec2/${local.name_prefix}/worker-asg"
  retention_in_days = var.log_retention_days

  tags = {
    Name    = "${local.name_prefix}-worker-asg-logs"
    Service = "worker-asg"
  }
}

# -----------------------------------------------------------------------------
# Networking — outbound-only; workers connect out to the backend over the
# ALB/NAT, never accept inbound traffic.
# -----------------------------------------------------------------------------

resource "aws_security_group" "asg_worker" {
  name        = "${local.name_prefix}-asg-worker-sg"
  description = "Pioneer Square worker ASG instances — outbound only, no inbound listeners"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "All outbound (ECR, S3, backend ALB, GitHub, CloudWatch, etc.)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-asg-worker-sg"
  }
}

# -----------------------------------------------------------------------------
# Launch template + Auto Scaling Group
# -----------------------------------------------------------------------------

locals {
  # Registry hostname only (e.g. <account>.dkr.ecr.<region>.amazonaws.com),
  # split off the repository_url for `docker login`.
  worker_ecr_registry = split("/", aws_ecr_repository.service["worker"].repository_url)[0]

  worker_backend_url = "${local.has_certificate ? "https" : "http"}://${var.domain_name != "" ? var.domain_name : aws_lb.main.dns_name}"

  # Falls back to the repo this stack deploys from when var.worker_repos is unset.
  worker_repos = var.worker_repos != "" ? var.worker_repos : var.github_repository
}

resource "aws_launch_template" "worker" {
  name_prefix = "${local.name_prefix}-worker-"
  description = "Pioneer Square worker — t3g.medium ARM64, runs the worker container long-running"

  image_id      = data.aws_ami.worker_arm.id
  instance_type = var.worker_instance_type

  iam_instance_profile {
    name = aws_iam_instance_profile.asg_worker.name
  }

  vpc_security_group_ids = [aws_security_group.asg_worker.id]

  # http_put_response_hop_limit = 2 lets the worker container (one network
  # hop beyond the host) reach the IMDS to pick up the instance role's
  # credentials for its own AWS calls (S3 session-log sync).
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_type           = "gp3"
      volume_size           = var.worker_root_volume_gib
      delete_on_termination = true
      encrypted             = true
    }
  }

  user_data = base64encode(templatefile("${path.module}/templates/worker_user_data.sh.tpl", {
    region           = local.region
    ecr_registry     = local.worker_ecr_registry
    ecr_repo_url     = aws_ecr_repository.service["worker"].repository_url
    image_tag        = var.container_image_tag
    backend_url      = local.worker_backend_url
    guild_id         = var.guild_id
    repos            = local.worker_repos
    max_agents       = var.worker_max_agents
    worker_log_level = var.log_level
    s3_bucket        = aws_s3_bucket.assets.bucket
    s3_prefix        = "worker-sessions"
    log_group        = aws_cloudwatch_log_group.worker_asg.name
  }))

  tag_specifications {
    resource_type = "instance"

    tags = {
      Name    = "${local.name_prefix}-worker"
      Service = "worker-asg"
    }
  }

  tag_specifications {
    resource_type = "volume"

    tags = {
      Name = "${local.name_prefix}-worker"
    }
  }

  tags = {
    Name = "${local.name_prefix}-worker-lt"
  }
}

resource "aws_autoscaling_group" "worker" {
  name                = "${local.name_prefix}-worker-asg"
  vpc_zone_identifier = aws_subnet.private[*].id

  min_size         = var.worker_asg_min_size
  max_size         = var.worker_asg_max_size
  desired_capacity = var.worker_asg_desired_capacity

  health_check_type         = "EC2"
  health_check_grace_period = 300

  launch_template {
    id      = aws_launch_template.worker.id
    version = "$Latest"
  }

  # Intentionally no instance_refresh: changing container_image_tag creates a
  # new launch template version for replacement/scale-out instances, but it
  # must not terminate a busy long-running worker during an ordinary deploy.
  # Existing ASG workers keep running their current image until explicitly
  # terminated or scaled in.

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-worker"
    propagate_at_launch = true
  }

  tag {
    key                 = "Service"
    value               = "worker-asg"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_policy" "worker_cpu" {
  name                   = "${local.name_prefix}-worker-cpu-target-tracking"
  autoscaling_group_name = aws_autoscaling_group.worker.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ASGAverageCPUUtilization"
    }

    target_value = var.worker_target_cpu_utilization
    # Do not let CPU target tracking terminate an instance that may be running
    # a long-lived coding task. Scale-in remains an explicit operator action.
    disable_scale_in = true
  }
}
