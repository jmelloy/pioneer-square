# IAM: ECS task execution role, ECS task role (S3 + Bedrock + worker
# dispatch), and the GitHub Actions OIDC provider + deploy role used by
# .github/workflows/deploy.yml.

# -----------------------------------------------------------------------------
# ECS task execution role — used by the ECS agent to pull images from ECR,
# write to CloudWatch Logs, and fetch SSM/Secrets Manager values referenced in
# a task definition's `secrets` block.
# -----------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "${local.name_prefix}-ecs-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_task_execution_secrets" {
  statement {
    sid    = "ReadSSMParameters"
    effect = "Allow"
    actions = [
      "ssm:GetParameters",
      "ssm:GetParameter",
    ]
    resources = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.ssm_path_prefix}/*"]
  }

  statement {
    sid       = "DecryptSSMSecureStrings"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:${local.partition}:kms:${local.region}:${local.account_id}:key/*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${local.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ecs_task_execution_secrets" {
  name   = "${local.name_prefix}-ecs-task-execution-secrets"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution_secrets.json
}

# -----------------------------------------------------------------------------
# ECS task role — assumed by the application code inside the container.
# Shared across backend/foreman/worker task definitions since they all need
# S3 access for session-log/asset storage, and Bedrock access for the
# foreman's LLM calls (FOREMAN_PROVIDER=bedrock) and any backend-side Bedrock
# use.
# -----------------------------------------------------------------------------

resource "aws_iam_role" "ecs_task" {
  name               = "${local.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

# --- S3: assets/uploads/session-log bucket access ---
data "aws_iam_policy_document" "ecs_task_s3" {
  statement {
    sid    = "ListBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
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

resource "aws_iam_role_policy" "ecs_task_s3" {
  name   = "${local.name_prefix}-ecs-task-s3"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_s3.json
}

# --- Bedrock: model invocation for the foreman AI (Nova/Claude via Bedrock) ---
# NOTE: granting this IAM policy is necessary but not sufficient — access to
# each individual foundation model family (Anthropic, Amazon Nova, etc.) must
# also be enabled per-account in the Bedrock console under "Model access"
# before InvokeModel calls to that family will succeed.
data "aws_iam_policy_document" "ecs_task_bedrock" {
  statement {
    sid    = "InvokeFoundationModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:${local.partition}:bedrock:*::foundation-model/*",
      # Cross-region inference profiles (e.g. FOREMAN_BEDROCK_MODEL =
      # arn:aws:bedrock:<region>:<account-id>:inference-profile/us.anthropic.*)
      "arn:${local.partition}:bedrock:*:${local.account_id}:inference-profile/*",
    ]
  }

  statement {
    sid       = "ListFoundationModels"
    effect    = "Allow"
    actions   = ["bedrock:ListFoundationModels"]
    resources = ["*"] # ListFoundationModels does not support resource-level scoping.
  }
}

resource "aws_iam_role_policy" "ecs_task_bedrock" {
  name   = "${local.name_prefix}-ecs-task-bedrock"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_bedrock.json
}

# --- Worker dispatch: lets the backend task launch on-demand worker tasks
# via ECS RunTask instead of the Docker socket it uses in docker-compose
# (see the worker_cpu/worker_memory comment in variables.tf). ---
data "aws_iam_policy_document" "ecs_task_worker_dispatch" {
  statement {
    sid    = "RunAndDescribeWorkerTasks"
    effect = "Allow"
    actions = [
      "ecs:RunTask",
      "ecs:StopTask",
      "ecs:DescribeTasks",
    ]
    resources = [
      aws_ecs_cluster.main.arn,
      # RunTask is authorized against the task definition family (any
      # revision) it launches — scoped to the worker family only.
      "arn:${local.partition}:ecs:${local.region}:${local.account_id}:task-definition/${local.name_prefix}-worker:*",
      # StopTask/DescribeTasks are authorized against the running task ARN,
      # scoped to tasks within this cluster.
      "arn:${local.partition}:ecs:${local.region}:${local.account_id}:task/${aws_ecs_cluster.main.name}/*",
    ]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }

  statement {
    sid       = "PassRolesToWorkerTask"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_task_execution.arn, aws_iam_role.ecs_task.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ecs_task_worker_dispatch" {
  name   = "${local.name_prefix}-ecs-task-worker-dispatch"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_worker_dispatch.json
}

# -----------------------------------------------------------------------------
# GitHub Actions OIDC — used by .github/workflows/deploy.yml (build/push +
# terraform apply) via aws-actions/configure-aws-credentials with
# role-to-assume, no static keys.
# -----------------------------------------------------------------------------

data "tls_certificate" "github_actions" {
  count = var.github_oidc_provider_arn == "" ? 1 : 0
  url   = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github_actions" {
  count = var.github_oidc_provider_arn == "" ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.github_actions[0].certificates[0].sha1_fingerprint]
}

locals {
  github_oidc_provider_arn = var.github_oidc_provider_arn != "" ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github_actions[0].arn
}

data "aws_iam_policy_document" "github_actions_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [for ref in var.github_oidc_allowed_refs : "repo:${var.github_repository}:${ref}"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = "${local.name_prefix}-github-actions-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume_role.json

  tags = {
    Name = "${local.name_prefix}-github-actions-deploy"
  }
}

# Deploy pipeline needs: push images to ECR, register task definitions and
# update ECS services, and pass the two task roles above when registering
# new task definition revisions. Scoped to this project's own resources
# where the API supports resource-level permissions.
data "aws_iam_policy_document" "github_actions_deploy" {
  statement {
    sid       = "ECRAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "ECRPushPull"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:PutImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
    ]
    resources = [for repo in aws_ecr_repository.service : repo.arn]
  }

  statement {
    sid    = "ECSDeploy"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:RegisterTaskDefinition",
      "ecs:UpdateService",
    ]
    resources = ["*"] # ecs:RegisterTaskDefinition does not support resource-level scoping.
  }

  statement {
    sid       = "PassTaskRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_task_execution.arn, aws_iam_role.ecs_task.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name   = "${local.name_prefix}-github-actions-deploy"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}

# deploy.yml's `apply` stage runs `terraform apply` against this stack, which
# needs broad read/write access across the resource types this module manages.
# Using a managed PowerUserAccess-style policy here is intentionally broader
# than the deploy-only policy above; tighten to your org's policies before
# running in a shared/production AWS account.
resource "aws_iam_role_policy_attachment" "github_actions_terraform_poweruser" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role_policy_attachment" "github_actions_terraform_iam_limited" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/IAMFullAccess"
}
