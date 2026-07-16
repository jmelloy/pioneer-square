# Pioneer Square — AWS ECS Fargate infrastructure.
#
# Migrates the docker-compose stack (postgres, backend, foreman, worker) to
# ECS Fargate behind an ALB, with ECR for images, S3 for asset/log storage,
# RDS for the database, SSM Parameter Store for secrets, and IAM policies
# granting the task role Bedrock + S3 access.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # Remote state in S3. Bootstrap the bucket (and, on Terraform < 1.10, a
  # DynamoDB lock table named the same as the bucket with a "-locks" suffix)
  # out of band before running `terraform init` — see README.md "Backend
  # setup". Backend blocks cannot interpolate variables, so fill in the
  # placeholders below (or pass them via `terraform init -backend-config=...`).
  backend "s3" {
    bucket       = "REPLACE_WITH_YOUR_TFSTATE_BUCKET"
    key          = "pioneer-square/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # Terraform >= 1.10 native S3 locking.
    # dynamodb_table = "REPLACE_WITH_YOUR_TFSTATE_LOCKS_TABLE" # Terraform < 1.10.
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.common_tags
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.name

  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  name_prefix = "${var.project_name}-${var.environment}"

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
