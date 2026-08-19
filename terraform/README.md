# Pioneer Square — Terraform (AWS)

Migrates the `docker-compose.yml` stack to AWS: VPC/ALB/ECS/ECR/RDS/S3/SSM, plus an EC2
Auto Scaling Group for baseline workers, with IAM policies granting the runtime roles S3
and Amazon Bedrock access.

## Architecture at a glance

| docker-compose service | ECS equivalent |
| --- | --- |
| `postgres` | RDS Postgres (`rds.tf`) — Fargate has no persistent volumes, so the DB moves to a managed instance |
| `backend` | ECS service `*-backend`, fronted by the ALB (`ecs.tf`, `alb.tf`) |
| `foreman` | ECS service `*-foreman`, `desired_count = 0` by default (compose's `profiles: [foreman]`) |
| `worker` | Auto Scaling Group of t3g.medium (ARM64) EC2 instances, **plus** an ECS task definition for elastic overflow — see "Worker fleet (ASG)" below |
| `pgweb` (Metabase, `profiles: [tools]`) | Not migrated; run locally against the RDS endpoint if needed |
| `postgres-test` (`profiles: [test]`) | Not migrated; test-only |
| (backend's inline `alembic upgrade head`) | One-off `*-migrate` task definition, run at deploy time — see "Database migrations" below |

### Worker fleet (ASG)

`asg_workers.tf` provisions the baseline worker pool as an EC2 Auto Scaling Group instead
of always-on ECS Fargate tasks: `t3g.medium` (ARM64/Graviton2) instances running Amazon
Linux 2023's `arm64` AMI, each running the same `worker` image as docker-compose
long-running via plain `docker run` (not ECS) — no per-task container churn.

| Piece | Resource | Notes |
| --- | --- | --- |
| AMI | `data.aws_ami.worker_arm` | Latest `al2023-ami-*-arm64`, refreshed on every `apply`. |
| Bootstrap | `templates/worker_user_data.sh.tpl` | Installs Docker, `docker login`s to ECR, pulls `<worker repo>:var.container_image_tag`, and `docker run -d --restart unless-stopped` with an env file (`PIONEER_BACKEND_URL`, `PIONEER_GUILD_ID`, `PIONEER_REPOS`, `PIONEER_MAX_AGENTS`, S3 session-log vars) and the `awslogs` log driver. Deploy builds publish a multi-arch image manifest so the same tag runs on both x86 Fargate and ARM64 ASG instances. |
| Identity | `aws_iam_role.asg_worker` | EC2 (not ECS) trust policy — scoped to ECR pull (worker repo only), the assets S3 bucket, its own CloudWatch log group, and `AmazonSSMManagedInstanceCore` for Session Manager shell access (no SSH key pair). |
| Networking | `aws_security_group.asg_worker` | Egress-only — the worker never accepts inbound connections, it dials out to the backend over the ALB. Instances launch into the private subnets, same as ECS tasks. |
| Scaling | `aws_autoscaling_policy.worker_cpu` | `TargetTrackingScaling` on `ASGAverageCPUUtilization`, target `var.worker_target_cpu_utilization` (default 70), with automatic scale-in disabled so active workers are not terminated by CPU dips. Scale-in is explicit/manual. |
| Rollout | Launch template only, no automatic `instance_refresh` | A new `container_image_tag` changes the launch template version for future scale-out/replacement instances, but Terraform does **not** recycle existing ASG instances during ordinary deploys. Existing workers keep running their current image until an operator terminates the EC2 instance or the ASG scales it in. |

Credentials work the same way as the on-demand path below: the worker fetches its
per-guild GitHub/Claude/provider tokens itself from the backend after connecting
(`/auth/github/token`, `/guilds/{id}/foreman/env-vars`) — nothing sensitive is baked into
the launch template or user data.

**Sizing.** `worker_asg_min_size`/`worker_asg_max_size`/`worker_asg_desired_capacity`
(default `1`/`4`/`1`) control the fleet, and `worker_max_agents` defaults to `2` per
`t3g.medium` instance. `worker_asg_min_size` is kept `>= 1` by default:
target tracking needs at least one running instance to compute `ASGAverageCPUUtilization`
against, so scaling out from zero isn't possible with CPU-only target tracking. Automatic
scale-in is disabled because EC2 ASG scale-in has no app-level knowledge of active coding
tasks; reduce desired capacity or terminate instances manually when you know they are safe
to replace. A queue-depth metric plus lifecycle drain hook would allow safer automatic
scale-in later — no such aggregate-count endpoint exists in the backend today (see
`backend/routes/tasks.py`), so this module ships CPU target tracking scale-out only, per
the issue's primary ask; queue-depth-based scaling is a documented follow-up, not
implemented here.

**Capacity.** A `t3g.medium` is intended as one small always-on worker host, not a combined
backend/foreman/multi-worker box. It has 2 vCPU / 4 GiB total, so run at most 1-2 concurrent
agent slots on it for typical repo/test workloads; use `t3g.large`/`t4g.large` or multiple
instances for 2-3 busy workers plus any LLM proxy/foreman process.

**Cost.** A single `t3g.medium` (2 vCPU / 4 GiB, ARM) runs roughly ~40% cheaper per
vCPU-hour than the equivalent on-demand x86 Fargate `worker` task definition
(`var.worker_cpu = 1024` / `var.worker_memory = 2048`, i.e. 1 vCPU / 2 GiB), while running
continuously rather than only while a task is in flight. Whether the ASG is cheaper than
the previous architecture in practice depends on how much of the day workers were actually
busy: an idle Fargate fleet costs $0, so a `min_size = 1` always-on ASG has a real cost
floor (roughly one `t3g.medium` running 24/7, ~$0.03/hr on-demand in `us-east-1` at time of
writing — check current pricing before relying on this) in exchange for zero cold-start
latency on the first task of a burst. Set `worker_asg_min_size = 0` and accept ECS-only
dispatch (disable/remove this module) if the workload is bursty enough that an always-on
floor isn't worth the latency win; the on-demand path below remains available either way.

### On-demand overflow dispatch (ECS Fargate)

In docker-compose, the `backend` container spawns one `worker` container per spawn request
via the Docker socket. Fargate tasks have no shared Docker socket, so on ECS the backend
dispatches workers with `ecs:RunTask` instead: `backend/worker_runtime.py` switches to ECS
mode automatically when `ECS_CLUSTER_NAME` and `ECS_WORKER_TASK_DEFINITION` are set (both
injected into the backend task definition by `ecs.tf`, alongside `ECS_WORKER_SUBNETS` and
`ECS_WORKER_SECURITY_GROUPS` for the worker task's awsvpc network interface). This module
registers the `worker` task definition and grants the backend's task role
`ecs:RunTask`/`ecs:StopTask`/`ecs:DescribeTasks` scoped to this cluster (see `iam.tf`).
With the ASG fleet above providing baseline capacity, this path now mainly covers the
foreman's `spawn_worker` tool and the operator "spawn worker" UI action reaching for repos
the ASG fleet doesn't already have checked out — kept rather than removed so that
capability doesn't regress.

Spawned worker tasks connect back to the backend through the ALB (`WORKER_BACKEND_URL`),
carry per-spawn configuration as container env overrides (static secrets like
`GITHUB_TOKEN` ride in the task definition's `secrets` block), and are stopped with
`ecs:StopTask` when force-killed. The backend's idle reaper
(`backend/worker_lifecycle.py`, `PIONEER_WORKER_IDLE_TIMEOUT`, default 30 min) shuts
spawned workers down after a period of inactivity so idle Fargate tasks don't accrue
cost indefinitely.

### Database migrations

In docker-compose the backend container runs `alembic upgrade head` inline before
`pioneer serve`. On ECS that would couple schema upgrades to backend startup, forcing the
old task down before the new one could safely come up. Instead, `ecs.tf` registers a
dedicated `*-migrate` task definition (backend image, `alembic upgrade head` only,
`DATABASE_URL` as its single secret) with no service, and the backend container command is
a plain `pioneer serve`.

At deploy time, `deploy.yml`'s `migrate` job registers the migrate task definition at the
new image tag (a `-target`ed apply that leaves the services untouched), runs it via
`ecs:RunTask`, streams its CloudWatch output into the job log, and fails the deploy if the
task exits non-zero — the services only roll after the schema is at `head`. Because the
backend no longer migrates at startup, ECS's default rolling deployment (min healthy 100% /
max 200%) brings the new backend up alongside the old one and tears the old one down only
once the new one passes health checks.

Running a migration out-of-band (without deploying) is the same call the workflow makes:

```bash
aws ecs run-task --cluster <cluster_name> \
  --task-definition <name_prefix>-migrate \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<private_subnet_ids>],securityGroups=[<ecs_tasks_security_group_id>],assignPublicIp=DISABLED}"
```

(all values available as Terraform outputs).

## Prerequisites

- Terraform >= 1.6 (native S3 state locking via `use_lockfile` needs >= 1.10 — see "Backend
  setup" for the fallback on older versions).
- AWS CLI configured with credentials that can create the resources below (or use the
  GitHub Actions OIDC role this module creates — see "CI/CD").
- An AWS account with **Bedrock model access enabled** for whichever model families the
  foreman will call (Anthropic Claude, Amazon Nova, etc.) — this is a per-account,
  per-model-family console setting (Bedrock console → **Model access**) and is **not**
  something Terraform/IAM can enable. The IAM policy in `iam.tf` only grants the API
  permissions; it does not grant model access itself.
- Docker, for building the three service images (`backend`, `foreman`, `worker`) — see the
  repo's root `Dockerfile` and its `--target` build args.

## Backend setup

Terraform state is stored in S3 (`main.tf`'s `backend "s3"` block). Backend blocks can't
reference variables, so bootstrap the bucket once, out of band, before the first `init`:

```bash
aws s3api create-bucket --bucket <your-tfstate-bucket> --region <your-region>
aws s3api put-bucket-versioning --bucket <your-tfstate-bucket> \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket <your-tfstate-bucket> \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

Then fill in `terraform/main.tf`'s `backend "s3"` block (`bucket`, `region`) — or pass them
at init time instead of editing the file:

```bash
terraform init \
  -backend-config="bucket=<your-tfstate-bucket>" \
  -backend-config="region=<your-region>"
```

`use_lockfile = true` uses Terraform's native S3 state locking (Terraform >= 1.10, no
DynamoDB table needed). On older Terraform, delete that line, create a DynamoDB table, and
uncomment `dynamodb_table` in the same block:

```bash
aws dynamodb create-table \
  --table-name <your-tfstate-bucket>-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST
```

## First-time apply

1. Copy the variables you need to override into a `terraform.tfvars` (everything has a
   default — see `variables.tf` — but the secret variables default to the placeholder
   `"CHANGE_ME_IN_SSM"` and should be set to real values before the first apply so the
   initial SSM parameter values aren't garbage):

   ```hcl
   aws_region   = "us-east-1"
   environment  = "staging"
   domain_name  = "pioneer-square.example.com"
   frontend_url = "https://pioneer-square.example.com"

   db_password             = "..."
   github_client_id        = "..."
   github_client_secret    = "..."
   github_token            = "..."
   anthropic_api_key       = "..."
   claude_code_oauth_token = "..."
   pioneer_foreman_key     = "..."
   pioneer_ci_key          = "..."
   discord_bot_token       = "..."   # optional, leave "" to disable Discord
   ```

   Never commit `terraform.tfvars` with real secrets in it — see "Secrets" below for the
   longer-term approach.

2. `terraform init` (see "Backend setup"), then `terraform plan -out plan.tfplan` and review
   it, then `terraform apply plan.tfplan`.

3. First apply creates ECR repos but no images exist yet, so the ECS services will fail to
   start (`CannotPullContainerError`) until you build and push at least one image per
   service:

   ```bash
   aws ecr get-login-password --region <region> | \
     docker login --username AWS --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com

   for target in backend foreman worker; do
     docker build --target "$target" -t "<account-id>.dkr.ecr.<region>.amazonaws.com/pioneer-square-staging/$target:latest" .
     docker push "<account-id>.dkr.ecr.<region>.amazonaws.com/pioneer-square-staging/$target:latest"
   done
   ```

   Then force a new deployment so the services pick up the images:

   ```bash
   aws ecs update-service --cluster pioneer-square-staging-cluster \
     --service pioneer-square-staging-backend --force-new-deployment
   ```

   The worker ASG's launch template renders `var.container_image_tag` (not `latest`) into
   its user data, so on a first apply — before any image has been pushed at that tag — its
   instances will boot, fail the `docker pull` in `worker_user_data.sh.tpl`, and simply have
   no worker container running (check `/var/log/pioneer-worker-init.log` on the instance via
   SSM Session Manager). Once an image exists at that tag, either wait for the next
   `terraform apply -var container_image_tag=<sha>` (which rolls a fresh launch template
   version and refreshes the instances) or terminate the instances manually to let the ASG
   replace them.

4. Point `var.domain_name`'s DNS record at the `alb_dns_name` output (or set
   `var.route53_zone_id` so Terraform manages the ACM validation + you manage the apex
   record yourself — this module does not create the domain's A/ALIAS record).

## Updating a service image

Manually:

```bash
docker build --target backend -t <ecr_repo_url>:<tag> .
docker push <ecr_repo_url>:<tag>
aws ecs update-service --cluster <cluster_name> --service <service_name> --force-new-deployment
```

Via CI: push to `main` (or a `release/**` branch) and `.github/workflows/deploy.yml` builds,
pushes, and updates the ECS service task definition with the new image digest automatically
— see "CI/CD" below.

The `backend_container_port`, `backend_cpu`/`memory`, etc. variables (see `variables.tf`)
resize tasks; `terraform apply` after changing them.

## Secrets

Every sensitive docker-compose env var is stored as an SSM `SecureString` parameter under
`/pioneer-square/<environment>/...` (`ssm.tf`), and referenced by ECS task definitions via
their `secrets` block — never as plaintext `environment` entries. Each parameter has
`lifecycle { ignore_changes = [value] }`, so:

- Terraform sets the **initial** value from the matching Terraform variable.
- Rotating a secret afterward should go through `aws ssm put-parameter --overwrite` (or the
  console) directly — **not** by changing the Terraform variable and re-applying, since
  Terraform will no longer touch `value` once the parameter exists.

```bash
aws ssm put-parameter --name /pioneer-square/staging/anthropic_api_key \
  --type SecureString --value "sk-ant-..." --overwrite
```

## Bedrock

`iam.tf` grants the ECS task role `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`,
and `bedrock:ListFoundationModels`, scoped to `arn:aws:bedrock:*::foundation-model/*` plus
this account's cross-region inference profiles
(`arn:aws:bedrock:*:<account-id>:inference-profile/*` — the ARN shape `FOREMAN_BEDROCK_MODEL`
expects). **This is necessary but not sufficient**: each model family (Anthropic, Amazon
Nova, etc.) also requires access to be enabled per-account in the Bedrock console under
**Model access** before `InvokeModel` calls to that family succeed.

## CI/CD

### `deploy.yml`

Triggered on push to `main` (or manually via `workflow_dispatch`). Three stages:

1. **build** (one job per service, backend/foreman/worker): assumes the
   `github_actions_deploy_role_arn` role via OIDC, logs in to ECR, and builds/pushes
   the image tagged with the commit SHA (also stamped into the image as `PIONEER_VERSION`).
2. **migrate**: registers the `*-migrate` task definition at the new image tag
   (`terraform apply -target=aws_ecs_task_definition.migrate`, services untouched), runs it
   as a one-off Fargate task, and fails the deploy if `alembic upgrade head` exits non-zero
   — see "Database migrations" above.
3. **apply**: `terraform init` + `terraform apply -auto-approve -var container_image_tag=<sha>`.
   Terraform is the single source of truth for the running task definitions — it renders
   env (subnets, security groups, config) **and** the image tag into one revision and rolls
   the services. There is no separate `amazon-ecs-deploy-task-definition` step and the
   services no longer set `ignore_changes = [task_definition]`; config changes in
   `ecs.tf` now reach the running services on the next push.

Single tfstate today, so this deploys `staging` only. To add `prod`, give it its own state
(workspace or backend key) and select it in the `apply` job before wiring a `release/**`
trigger. The `apply` job has no GitHub `environment:` approval gate because that would
change the OIDC subject claim to `environment:<name>`, which `var.github_oidc_allowed_refs`
doesn't permit — to add one, also add `"environment:terraform-apply"` to that list.

### Required GitHub configuration

**Repository secrets/variables** (Settings → Secrets and variables → Actions):

| Name | Purpose |
| --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | The `github_actions_deploy_role_arn` Terraform output — the OIDC role the deploy workflow assumes. |
| `AWS_REGION` | Region to operate in (matches `var.aws_region`). |

(The tfstate bucket/region are hardcoded in `main.tf`'s `backend "s3"` block, so `deploy.yml` runs a bare `terraform init` — no `TF_STATE_BUCKET` secret needed. If you fork to another account, edit that block.)

No `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are used — the workflow authenticates via
OIDC (`aws-actions/configure-aws-credentials` + `role-to-assume`), which is why `iam.tf`
creates an `aws_iam_openid_connect_provider` for `token.actions.githubusercontent.com` plus
a role trusting it, scoped to `var.github_repository` and `var.github_oidc_allowed_refs`.

If your account already has a `token.actions.githubusercontent.com` OIDC provider from
another stack (only one can exist per account), set `var.github_oidc_provider_arn` to its
ARN instead of letting this module create a duplicate.
