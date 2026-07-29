# SSM Parameter Store — holds every sensitive docker-compose env var
# (DB password, OAuth/API credentials, bot tokens) as SecureString
# parameters. ECS task definitions reference these by ARN in their
# `secrets` block (see ecs.tf) rather than embedding values in plain
# `environment` entries.
#
# Terraform sets the *initial* value from the corresponding sensitive
# variable (see variables.tf — defaults are "CHANGE_ME_IN_SSM" placeholders).
# Each parameter ignores future changes to `value` so that rotating a secret
# via `aws ssm put-parameter` or the console doesn't get clobbered — or
# drift-flagged — by the next `terraform apply`. See README.md "Secrets".

locals {
  ssm_path_prefix = "/${var.project_name}/${var.environment}"

  ssm_secrets = {
    db_password              = var.db_password
    github_client_id         = var.github_client_id
    github_client_secret     = var.github_client_secret
    github_token             = var.github_token
    github_app_private_key   = var.github_app_private_key
    anthropic_api_key        = var.anthropic_api_key
    claude_code_oauth_token  = var.claude_code_oauth_token
    openai_api_key           = var.openai_api_key
    pioneer_foreman_key      = var.pioneer_foreman_key
    pioneer_ci_key           = var.pioneer_ci_key
    discord_bot_token        = var.discord_bot_token
    aws_bearer_token_bedrock = var.aws_bearer_token_bedrock
    # Falls back to an unguessable value, not the shared placeholder — see the
    # variable "debug_token" comment for why this secret must fail closed.
    debug_token = coalesce(var.debug_token, random_password.debug_token.result)
  }
}

resource "random_password" "debug_token" {
  length  = 48
  special = false
}

resource "aws_ssm_parameter" "secret" {
  for_each = local.ssm_secrets

  name = "${local.ssm_path_prefix}/${each.key}"
  type = "SecureString"
  # SSM rejects empty values, and ecs.tf references every key in this map, so
  # optional secrets left "" (discord_bot_token, aws_bearer_token_bedrock) are
  # seeded with the same placeholder as the required ones.
  value = coalesce(each.value, "CHANGE_ME_IN_SSM")

  tags = {
    Name = "${local.name_prefix}-${replace(each.key, "_", "-")}"
  }

  lifecycle {
    ignore_changes = [value]
  }
}

# DATABASE_URL is derived from the RDS endpoint + the same password stored
# above, so the backend/worker containers get a single connection string
# instead of assembling one from parts at runtime.
resource "aws_ssm_parameter" "database_url" {
  name  = "${local.ssm_path_prefix}/database_url"
  type  = "SecureString"
  value = "postgresql+asyncpg://${var.db_username}:${var.db_password}@${aws_db_instance.postgres.endpoint}/${var.db_name}"

  tags = {
    Name = "${local.name_prefix}-database-url"
  }

  lifecycle {
    ignore_changes = [value]
  }
}
