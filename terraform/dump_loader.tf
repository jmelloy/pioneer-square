# Throwaway "dump loader" instance for bulk-loading SQL dumps into RDS, which
# lives in private subnets with no other path in. Off by default; everything
# here is count-gated on one flag.
#
# Usage:
#   1. Start:  terraform apply -var dump_loader_enabled=true
#   2. Upload: aws s3 cp dump.sql s3://<assets-bucket>/tmp/dump.sql
#   3. Load (runs on the instance via SSM RunCommand, no SSH/tunnel needed):
#        terraform output -raw dump_loader_load_command | bash
#      then poll:  aws ssm get-command-invocation --command-id <id> \
#                    --instance-id $(terraform output -raw dump_loader_instance_id)
#   4. Stop:   terraform apply   (flag defaults back to false; also delete the
#              s3://.../tmp/ object)
#
# ponytail: t4g.nano + psql streaming from S3. Use unlimited CPU credits (set
# below) — a standard-mode nano throttles to ~5% CPU and a multi-GB load
# crawls for hours.

variable "dump_loader_enabled" {
  description = "Create the temporary EC2 instance (plus IAM role and SG) used to load SQL dumps into RDS. Keep false except while actively loading."
  type        = bool
  default     = false
}

data "aws_ssm_parameter" "al2023_arm64" {
  count = var.dump_loader_enabled ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_iam_role" "dump_loader" {
  count = var.dump_loader_enabled ? 1 : 0
  name  = "${local.name_prefix}-dump-loader"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dump_loader_ssm" {
  count      = var.dump_loader_enabled ? 1 : 0
  role       = aws_iam_role.dump_loader[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "dump_loader" {
  count = var.dump_loader_enabled ? 1 : 0
  name  = "dump-loader"
  role  = aws_iam_role.dump_loader[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.assets.arn}/tmp/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.secret["db_password"].arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "dump_loader" {
  count = var.dump_loader_enabled ? 1 : 0
  name  = "${local.name_prefix}-dump-loader"
  role  = aws_iam_role.dump_loader[0].name
}

# No ingress at all — SSM RunCommand is outbound-only from the instance.
# vpc.tf adds this SG to the RDS SG's allowed sources when it exists.
resource "aws_security_group" "dump_loader" {
  count       = var.dump_loader_enabled ? 1 : 0
  name        = "${local.name_prefix}-dump-loader"
  description = "Temporary dump loader - egress only"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-dump-loader"
  }
}

resource "aws_instance" "dump_loader" {
  count = var.dump_loader_enabled ? 1 : 0

  ami                         = data.aws_ssm_parameter.al2023_arm64[0].value
  instance_type               = "t4g.nano"
  subnet_id                   = aws_subnet.public[0].id
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.dump_loader[0].id]
  iam_instance_profile        = aws_iam_instance_profile.dump_loader[0].name

  credit_specification {
    cpu_credits = "unlimited"
  }

  user_data = <<-EOF
    #!/bin/bash
    dnf install -y -q postgresql17
  EOF

  tags = {
    Name = "${local.name_prefix}-dump-loader"
  }
}

output "dump_loader_instance_id" {
  value = var.dump_loader_enabled ? aws_instance.dump_loader[0].id : null
}

output "dump_loader_load_command" {
  description = "SSM RunCommand that streams s3://<assets>/tmp/dump.sql into the database in one transaction."
  value = var.dump_loader_enabled ? join(" ", [
    "aws ssm send-command",
    "--instance-ids ${aws_instance.dump_loader[0].id}",
    "--document-name AWS-RunShellScript",
    "--timeout-seconds 600",
    "--parameters '${jsonencode({
      executionTimeout = ["21600"]
      commands = [
        "export PGPASSWORD=$(aws ssm get-parameter --name ${aws_ssm_parameter.secret["db_password"].name} --with-decryption --query Parameter.Value --output text --region ${local.region})",
        "aws s3 cp s3://${aws_s3_bucket.assets.bucket}/tmp/dump.sql - --region ${local.region} | psql -h ${aws_db_instance.postgres.address} -U ${var.db_username} -d ${var.db_name} -q -1 -v ON_ERROR_STOP=1 2>/tmp/load.err; echo psql_exit=$?",
        "tail -5 /tmp/load.err",
      ]
    })}'",
    "--query Command.CommandId --output text",
  ]) : null
}
