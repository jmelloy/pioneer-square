# Pioneer Square — Terraform (AWS)

Migrates the `docker-compose.yml` stack to AWS: VPC/ALB/ECS/ECR/RDS/S3/SSM, with ECS
services and one-off tasks placed on an EC2 Auto Scaling Group capacity provider. IAM
policies grant the runtime roles S3 and Amazon Bedrock access.

## Architecture at a glance

| docker-compose service | ECS equivalent |
| --- | --- |
| `postgres` | RDS Postgres (`rds.tf`) — the DB stays managed and outside ECS capacity |
| `backend` | ECS service `*-backend`, fronted by the ALB (`ecs.tf`, `alb.tf`), placed on the ASG capacity provider |
| `foreman` | ECS service `*-foreman`, `desired_count = 0` by default (compose's `profiles: [foreman]`), placed on the ASG capacity provider |
| `worker` | ECS task definition for on-demand workers placed on the ASG capacity provider |
| `pgweb` (Metabase, `profiles: [tools]`) | Metabase ECS service (`metabase.tf`), fronted by the ALB and placed on the ASG capacity provider |
| `postgres-test` (`profiles: [test]`) | Not migrated; test-only |
| (backend's inline `alembic upgrade head`) | One-off `*-migrate` task definition, run at deploy time — see "Database migrations" below |

### ECS capacity provider

All ECS services and one-off ECS tasks now run on an EC2 Auto Scaling Group capacity
provider (`ecs_capacity.tf`) instead of Fargate:

| Piece | Resource | Notes |
| --- | --- | --- |
| AMI | `data.aws_ssm_parameter.ecs_optimized_ami` | Latest ECS-optimized Amazon Linux 2023 AMI. |
| Capacity | `aws_autoscaling_group.ecs_capacity` + `aws_ecs_capacity_provider.asg` | ECS managed scaling adjusts the ASG between `ecs_capacity_min_size` and `ecs_capacity_max_size` as tasks are placed. |
| Bootstrap | `templates/ecs_capacity_user_data.sh.tpl` | Points the ECS agent at `aws_ecs_cluster.main`; no application container is started directly by user data. |
| Identity | `aws_iam_role.ecs_capacity` | EC2 trust policy with `AmazonEC2ContainerServiceforEC2Role` for ECS agent registration and `AmazonSSMManagedInstanceCore` for Session Manager access. |
| Networking | `aws_security_group.ecs_capacity` | Egress-only host SG. Service traffic uses each task ENI's existing service/task security group (`ecs_tasks`, plus Metabase's ALB SG). |

The ECS cluster's default capacity provider strategy points at this ASG, and the
backend/foreman/metabase services also set it explicitly. The deploy migration task and
backend-spawned worker tasks use the same capacity provider with `ecs:RunTask`
`capacityProviderStrategy`.

Sizing is controlled by `ecs_capacity_instance_type`, `ecs_capacity_min_size`,
`ecs_capacity_desired_capacity`, `ecs_capacity_max_size`, `ecs_capacity_target_capacity`,
and `ecs_capacity_root_volume_gib`. Keep `ecs_capacity_min_size >= 1` for the backend; ECS
managed scaling can add instances for extra service tasks, migrations, Metabase, and
on-demand workers.

### On-demand worker dispatch (ECS on ASG capacity)

In docker-compose, the `backend` container spawns one `worker` container per spawn request
via the Docker socket. In AWS, the backend dispatches workers with `ecs:RunTask` instead:
`backend/worker_runtime.py` switches to ECS mode when `ECS_CLUSTER_NAME` and
`ECS_WORKER_TASK_DEFINITION` are set. Terraform also injects
`ECS_WORKER_CAPACITY_PROVIDER`, `ECS_WORKER_SUBNETS`, and `ECS_WORKER_SECURITY_GROUPS`, so
spawned worker tasks run on the shared ASG-backed ECS capacity provider with their own
awsvpc task ENI.

Spawned worker tasks connect back to the backend through the ALB (`WORKER_BACKEND_URL`) and
carry per-spawn configuration as container env overrides. Credentials are fetched per guild
from the backend after startup rather than baked into the task definition. The backend's
idle reaper (`backend/worker_lifecycle.py`, `PIONEER_WORKER_IDLE_TIMEOUT`, default 30 min)
stops spawned workers after inactivity.

### Database migrations

In docker-compose the backend container runs `alembic upgrade head` inline before
`pioneer serve`. On ECS that would couple schema upgrades to backend startup, forcing the
old task down before the new one could safely come up. Instead, `ecs.tf` registers a
dedicated `*-migrate` task definition (backend image, `alembic upgrade head` only,
`DATABASE_URL` as its single secret) with no service, and the backend container command is
a plain `pioneer serve`.

At deploy time, `deploy.yml`'s `migrate` job registers the migrate task definition at the
new image tag (a `-target`ed apply that also ensures the ECS capacity provider exists but
leaves the services untouched), runs it via `ecs:RunTask`, streams its CloudWatch output into
the job log, and fails the deploy if the
task exits non-zero — the services only roll after the schema is at `head`. Because the
backend no longer migrates at startup, ECS's default rolling deployment (min healthy 100% /
max 200%) brings the new backend up alongside the old one and tears the old one down only
once the new one passes health checks.

Running a migration out-of-band (without deploying) is the same call the workflow makes:

```bash
aws ecs run-task --cluster <cluster_name> \
  --task-definition <name_prefix>-migrate \
  --capacity-provider-strategy "capacityProvider=<ecs_capacity_provider_name>,weight=1" \
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
   as a one-off ECS task on the ASG capacity provider, and fails the deploy if `alembic upgrade head` exits non-zero
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
