# ECS EC2 capacity provider — all ECS services and one-off tasks run on this
# Auto Scaling Group instead of Fargate. This ASG is ECS cluster capacity for
# backend/foreman/metabase/migrate and on-demand worker RunTask launches.

# Latest ECS-optimized Amazon Linux 2023 AMI for x86_64 instances.
data "aws_ssm_parameter" "ecs_optimized_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

resource "aws_iam_role" "ecs_capacity" {
  name               = "${local.name_prefix}-ecs-capacity"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = {
    Name = "${local.name_prefix}-ecs-capacity"
  }
}

resource "aws_iam_role_policy_attachment" "ecs_capacity_ecs" {
  role       = aws_iam_role.ecs_capacity.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_role_policy_attachment" "ecs_capacity_ssm" {
  role       = aws_iam_role.ecs_capacity.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ecs_capacity" {
  name = "${local.name_prefix}-ecs-capacity"
  role = aws_iam_role.ecs_capacity.name
}

resource "aws_security_group" "ecs_capacity" {
  name        = "${local.name_prefix}-ecs-capacity-sg"
  description = "ECS EC2 capacity instances - outbound only; task ENIs carry service security groups"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "All outbound (ECR, SSM, CloudWatch, RDS via task ENIs, internet via NAT)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-ecs-capacity-sg"
  }
}

resource "aws_launch_template" "ecs_capacity" {
  name_prefix = "${local.name_prefix}-ecs-capacity-"
  description = "Pioneer Square ECS cluster capacity"

  image_id      = data.aws_ssm_parameter.ecs_optimized_ami.value
  instance_type = var.ecs_capacity_instance_type

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_capacity.name
  }

  vpc_security_group_ids = [aws_security_group.ecs_capacity.id]

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  block_device_mappings {
    device_name = "/dev/xvda"

    ebs {
      volume_type           = "gp3"
      volume_size           = var.ecs_capacity_root_volume_gib
      delete_on_termination = true
      encrypted             = true
    }
  }

  user_data = base64encode(templatefile("${path.module}/templates/ecs_capacity_user_data.sh.tpl", {
    cluster_name = aws_ecs_cluster.main.name
  }))

  tag_specifications {
    resource_type = "instance"

    tags = {
      Name    = "${local.name_prefix}-ecs-capacity"
      Service = "ecs-capacity"
    }
  }

  tag_specifications {
    resource_type = "volume"

    tags = {
      Name    = "${local.name_prefix}-ecs-capacity"
      Service = "ecs-capacity"
    }
  }

  tags = {
    Name = "${local.name_prefix}-ecs-capacity-lt"
  }
}

resource "aws_autoscaling_group" "ecs_capacity" {
  name = "${local.name_prefix}-ecs-capacity-asg"
  vpc_zone_identifier = [
    for subnet in aws_subnet.private : subnet.id
  ]

  min_size         = var.ecs_capacity_min_size
  max_size         = var.ecs_capacity_max_size
  desired_capacity = var.ecs_capacity_desired_capacity

  health_check_type         = "EC2"
  health_check_grace_period = 300

  protect_from_scale_in = true

  launch_template {
    id      = aws_launch_template.ecs_capacity.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-ecs-capacity"
    propagate_at_launch = true
  }

  tag {
    key                 = "Service"
    value               = "ecs-capacity"
    propagate_at_launch = true
  }

  # ECS managed scaling owns desired_capacity, and ECS adds the AmazonECSManaged
  # tag when the ASG is attached to a capacity provider. Do not let the targeted
  # migrate/bootstrap apply fight managed scaling before running the migration.
  lifecycle {
    ignore_changes = [
      desired_capacity,
      tag,
    ]
  }
}

resource "aws_ecs_capacity_provider" "asg" {
  name = "${local.name_prefix}-asg"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.ecs_capacity.arn
    managed_termination_protection = "ENABLED"

    managed_scaling {
      status                    = "ENABLED"
      target_capacity           = var.ecs_capacity_target_capacity
      minimum_scaling_step_size = 1
      maximum_scaling_step_size = 2
      instance_warmup_period    = 300
    }
  }

  tags = {
    Name = "${local.name_prefix}-asg-capacity-provider"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = [aws_ecs_capacity_provider.asg.name]

  default_capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.asg.name
    weight            = 1
    base              = 0
  }
}
