# -----------------------------------------------------------------------------
# Core
# -----------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used to prefix/tag all resources."
  type        = string
  default     = "pioneer-square"
}

variable "environment" {
  # NOTE: label is "staging" but this is the ONLY live deployment and it serves
  # the single real org — treat it as production. Not renamed to "prod" because
  # that reshuffles every resource name/ARN (a destroy+recreate).
  description = "Deployment environment name (e.g. staging, prod). Used in resource names and tags."
  type        = string
  default     = "staging"
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to span (public + private subnet pair each)."
  type        = number
  default     = 2
}

variable "single_nat_gateway" {
  description = "Use a single shared NAT Gateway instead of one per AZ, to cut cost in non-prod environments."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# ALB / TLS
# -----------------------------------------------------------------------------

variable "domain_name" {
  description = "Public hostname the ALB serves (e.g. pioneer-square.example.com). Used only for output/reference; DNS record creation is out of scope."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = <<-EOT
    ARN of an existing ACM certificate for the ALB's HTTPS listener.
    Leave empty to have Terraform provision one via aws_acm_certificate for
    `var.domain_name` (validation still requires manual/external DNS records
    unless `var.route53_zone_id` is set). Must be non-empty for the HTTPS
    listener to be created.
  EOT
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Optional Route53 hosted zone ID for ACM DNS validation records. Leave empty to skip automatic validation."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# ECS — shared
# -----------------------------------------------------------------------------

variable "container_image_tag" {
  # No default on purpose: deploy.yml always passes -var container_image_tag=<sha>.
  # A "latest" default was a footgun — nothing ever pushes that tag, so a bare
  # `terraform apply` (without the var) would point every task def at a
  # nonexistent image and break the running services. Required means a bare
  # apply fails loudly instead.
  description = "Image tag (commit SHA) deployed for all services. deploy.yml passes -var container_image_tag=<sha> on every push. Required — a bare `terraform apply` must specify a tag that exists in ECR."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for ECS task logs."
  type        = number
  default     = 30
}

# -----------------------------------------------------------------------------
# ECS EC2 capacity provider — Auto Scaling Group backing all ECS tasks
# -----------------------------------------------------------------------------

variable "ecs_capacity_instance_type" {
  description = "EC2 instance type for the ECS capacity provider Auto Scaling Group. All ECS services and one-off tasks are placed here instead of Fargate."
  type        = string
  default     = "t3.medium"
}

variable "ecs_capacity_min_size" {
  description = "Minimum ECS capacity provider instance count. Keep at least 1 so the backend service has somewhere to run."
  type        = number
  default     = 1
}

variable "ecs_capacity_max_size" {
  description = "Maximum ECS capacity provider instance count. ECS managed scaling raises capacity as task demand grows."
  type        = number
  default     = 4
}

variable "ecs_capacity_desired_capacity" {
  description = "Initial desired ECS capacity provider instance count. ECS managed scaling may adjust it within min/max."
  type        = number
  default     = 1
}

variable "ecs_capacity_target_capacity" {
  description = "ECS managed scaling target capacity percentage for the ASG capacity provider. 100 packs tasks tightly; lower values keep headroom."
  type        = number
  default     = 100
}

variable "ecs_capacity_root_volume_gib" {
  description = "Root EBS volume size (GiB) for ECS capacity provider instances. Container writable layers and on-demand worker worktrees live here."
  type        = number
  default     = 80
}

# -----------------------------------------------------------------------------
# ECS — backend service (HTTP API + SPA, exposed via ALB)
# -----------------------------------------------------------------------------

variable "backend_container_port" {
  description = "Port the backend container listens on (matches `EXPOSE 8000` / `pioneer serve` in the Dockerfile)."
  type        = number
  default     = 8000
}

variable "backend_cpu" {
  description = "ECS task CPU units for the backend service."
  type        = number
  default     = 256
}

variable "backend_memory" {
  description = "ECS task memory (MiB) for the backend service."
  type        = number
  default     = 384
}

variable "backend_desired_count" {
  description = "Desired task count for the backend ECS service."
  type        = number
  default     = 1
}

# -----------------------------------------------------------------------------
# ECS — foreman service (background AI loop, no inbound HTTP; profiles:[foreman] in compose)
# -----------------------------------------------------------------------------

variable "foreman_cpu" {
  description = "ECS task CPU units for the foreman service."
  type        = number
  default     = 256
}

variable "foreman_memory" {
  description = "ECS task memory (MiB) for the foreman service."
  type        = number
  default     = 512
}

variable "foreman_desired_count" {
  description = "Desired task count for the foreman ECS service. Set to 0 to disable (no GUILD_ID configured)."
  type        = number
  default     = 0
}

variable "foreman_model" {
  description = "Claude model ID used by the foreman when FOREMAN_PROVIDER=anthropic."
  type        = string
  default     = "claude-sonnet-4-6"
}

variable "foreman_provider" {
  description = "Foreman LLM provider: anthropic, openai, or bedrock."
  type        = string
  default     = "anthropic"
}

variable "foreman_bedrock_model" {
  description = "Cross-region Bedrock inference profile ARN for the foreman when foreman_provider=bedrock (account-scoped, no safe default)."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# ECS — worker task definition
# -----------------------------------------------------------------------------
# The worker is NOT deployed as a long-running ECS service: in docker-compose
# the backend spawns one worker container per task via the Docker socket
# (backend/foreman/tools.py, backend/routes/workers.py). In AWS, this task
# definition is launched on demand via the ECS RunTask API (ecs:RunTask — see
# iam.tf) and placed on the ASG-backed ECS capacity provider.

variable "worker_cpu" {
  description = "ECS task CPU units for on-demand worker tasks."
  type        = number
  default     = 768
}

variable "worker_memory" {
  description = "ECS task memory (MiB) for on-demand worker tasks."
  type        = number
  default     = 1536
}

variable "worker_ephemeral_storage_gib" {
  description = "Deprecated for EC2-backed ECS worker tasks. Worker repos and worktrees now live on the ECS capacity instance root volume."
  type        = number
  default     = 40
}

# -----------------------------------------------------------------------------
# RDS (replaces the docker-compose `postgres` service)
# -----------------------------------------------------------------------------

variable "db_name" {
  description = "Postgres database name."
  type        = string
  default     = "pioneer_square"
}

variable "db_username" {
  description = "Postgres master username."
  type        = string
  default     = "pioneer"
}

variable "db_engine_version" {
  description = "Postgres engine version."
  type        = string
  default     = "18.4"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GiB."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "RDS storage autoscaling ceiling in GiB."
  type        = number
  default     = 100
}

variable "db_multi_az" {
  description = "Enable RDS Multi-AZ (recommended for prod)."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "RDS automated backup retention period in days."
  type        = number
  default     = 7
}

variable "db_deletion_protection" {
  description = "Enable RDS deletion protection."
  type        = bool
  default     = false
}

variable "db_skip_final_snapshot" {
  description = "Skip the final RDS snapshot on destroy (set false for prod)."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# S3
# -----------------------------------------------------------------------------

variable "s3_bucket_name" {
  description = "Optional explicit name for the assets/uploads/session-log bucket. Leave empty to derive a unique name from project/environment/account ID."
  type        = string
  default     = ""
}

variable "s3_force_destroy" {
  description = "Allow `terraform destroy` to delete the S3 bucket even if it still contains objects. Keep false in prod."
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# GitHub OIDC (for the deploy.yml / terraform.yml Actions workflows)
# -----------------------------------------------------------------------------

variable "github_oidc_provider_arn" {
  description = <<-EOT
    ARN of an existing `token.actions.githubusercontent.com` IAM OIDC
    provider in this account. Leave empty to have Terraform create one
    (only one such provider can exist per account — if your account already
    has one from another stack, set this instead of creating a duplicate).
  EOT
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "GitHub repository allowed to assume the CI deploy role, as `org/repo` (e.g. jmelloy/pioneer-square)."
  type        = string
  default     = "jmelloy/pioneer-square"
}

variable "github_oidc_allowed_refs" {
  description = "Git ref patterns (in GitHub OIDC subject-claim form) allowed to assume the CI role."
  type        = list(string)
  default = [
    "ref:refs/heads/main",
    "ref:refs/heads/release/*",
  ]
}

# -----------------------------------------------------------------------------
# Secrets (values). Defaults are placeholders — see README "Secrets" section.
# Terraform writes these into SSM as the *initial* value only; the parameters
# are created with lifecycle.ignore_changes on value so rotating them via the
# console/CLI afterward doesn't cause drift on the next `terraform apply`.
# -----------------------------------------------------------------------------

variable "db_password" {
  description = "Initial Postgres master password (rotate via SSM/RDS afterward, not by re-applying)."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "github_client_id" {
  description = "GitHub OAuth App client ID (backend auth flow)."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "github_client_secret" {
  description = "GitHub OAuth App client secret (backend auth flow)."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "github_token" {
  description = "GitHub token used by the backend/worker to clone private repos and open PRs."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "github_app_id" {
  description = "GitHub App id — the backend mints installation tokens so comments/commits attribute to the App bot. Non-secret."
  type        = string
  default     = "4394230"
}

variable "github_app_slug" {
  description = "GitHub App bot username (public page github.com/apps/pioneer-square + '[bot]') used for attribution. Empty disables App identity."
  type        = string
  default     = "pioneer-square[bot]"
}

variable "github_app_installation_id" {
  description = "Default GitHub App installation id used when a guild has none set (all jmelloy/* guilds share one). Per-guild rows override. Non-secret."
  type        = string
  default     = "149025614"
}

variable "github_app_private_key" {
  description = "GitHub App private key (PEM). Stored in SSM; empty leaves the App disabled and falls back to GITHUB_TOKEN."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "anthropic_api_key" {
  description = "Anthropic API key for the backend foreman (Claude SDK)."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "claude_code_oauth_token" {
  description = "Claude Code OAuth token forwarded to worker tasks (from `claude setup-token`)."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "openai_api_key" {
  description = "OpenAI API key, used when FOREMAN_PROVIDER=openai (Ollama/vLLM/LM Studio compatible endpoints)."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "pioneer_foreman_key" {
  description = "Shared secret the foreman uses to authenticate to the backend."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "debug_token" {
  # The backend mounts /debug/query whenever DEBUG_TOKEN is non-empty (main.py),
  # with no placeholder-stripping — so unlike the other secrets we can't seed the
  # shared "CHANGE_ME_IN_SSM" placeholder here or the routes would go live behind
  # a publicly-known token. Left empty, ssm.tf seeds an unguessable random value
  # (routes mounted but unreachable); set a real token to actually use them.
  description = "Bearer token gating the backend /debug/query routes. Leave empty to keep them unreachable; set a real value to enable."
  type        = string
  sensitive   = true
  default     = ""
}

variable "pioneer_ci_key" {
  description = "Shared secret for GitHub Actions CI-completion notifications to /guilds/{guild_id}/foreman/ci-notify."
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_IN_SSM"
}

variable "discord_bot_token" {
  description = "Discord bot token for notifications, slash commands, and the Gateway websocket. Empty disables Discord integration."
  type        = string
  sensitive   = true
  default     = ""
}

variable "aws_bearer_token_bedrock" {
  description = "Optional Bedrock bearer token for the foreman, used instead of IAM credentials when set."
  type        = string
  sensitive   = true
  default     = ""
}

# -----------------------------------------------------------------------------
# Non-secret application configuration
# -----------------------------------------------------------------------------

variable "frontend_url" {
  description = "Externally-reachable base URL of the app (GitHub OAuth redirect + webhook base). E.g. https://pioneer-square.example.com."
  type        = string
  default     = ""
}

variable "guild_id" {
  description = "6-char guild ID the foreman instance manages. Required for the foreman service to do anything useful."
  type        = string
  default     = ""
}

variable "log_level" {
  description = "Backend/foreman log level."
  type        = string
  default     = "INFO"
}

# Discord integration (see docs/discord.md). All optional — empty disables the
# corresponding feature, matching the docker-compose defaults.

variable "discord_channel_id" {
  description = "Discord channel ID for flat-channel notification embeds. Required together with discord_bot_token for notifications."
  type        = string
  default     = ""
}

variable "discord_application_id" {
  description = "Discord application ID — slash-command registration and the bot's own user ID for @mention detection."
  type        = string
  default     = ""
}

variable "discord_public_key" {
  description = "Discord application public key for interaction signature verification."
  type        = string
  default     = ""
}

variable "discord_allowed_role_ids" {
  description = "Comma-separated Discord role IDs authorized to control the bot."
  type        = string
  default     = ""
}

variable "discord_pioneer_guild_slug" {
  description = "Pioneer Square guild slug the Discord integration is bound to."
  type        = string
  default     = ""
}

variable "discord_stream_tasks" {
  description = "Mirror live task terminal output into per-task Discord threads (high volume)."
  type        = string
  default     = "false"
}

variable "discord_gateway_enabled" {
  description = "Run the persistent Discord Gateway websocket for inbound messages (requires the Message Content Intent). No-op while discord_bot_token is unset."
  type        = string
  default     = "true"
}

variable "discord_dev_guild_id" {
  description = "Discord guild (server) ID for instant slash-command registration during development. Empty registers commands globally."
  type        = string
  default     = ""
}

variable "discord_pr_debounce_seconds" {
  description = "Seconds to debounce PR-event Discord notifications."
  type        = string
  default     = ""
}

variable "webhook_debounce_seconds" {
  description = "Seconds to coalesce bursty GitHub webhook events before each batch triggers a foreman run. Code default is 30; raised to cut CI-driven foreman token spend."
  type        = string
  default     = "120"
}
