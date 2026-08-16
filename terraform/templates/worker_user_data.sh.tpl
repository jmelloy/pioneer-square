#!/bin/bash
# Bootstraps a t3g.medium (ARM64) Auto Scaling Group instance into a
# long-running Pioneer Square worker, mirroring the docker-compose `worker`
# service (see terraform/README.md "Worker fleet (ASG)"). Rendered by
# terraform/asg_workers.tf via templatefile(); $${...} placeholders below are
# substituted by Terraform, plain $VAR references are evaluated by bash at
# boot time.
set -euo pipefail
exec > >(tee -a /var/log/pioneer-worker-init.log) 2>&1

echo "== Pioneer Square worker bootstrap: $(date -u -Iseconds) =="

dnf install -y docker
systemctl enable --now docker

REGION="${region}"

TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ecr_registry}"

IMAGE="${ecr_repo_url}:${image_tag}"
docker pull "$IMAGE"

cat > /etc/pioneer-worker.env <<EOF
PIONEER_BACKEND_URL=${backend_url}
PIONEER_GUILD_ID=${guild_id}
PIONEER_REPOS=${repos}
PIONEER_MAX_AGENTS=${max_agents}
PIONEER_WORKER_NAME=$INSTANCE_ID
PIONEER_WORKER_LOG_LEVEL=${worker_log_level}
PIONEER_S3_BUCKET=${s3_bucket}
PIONEER_S3_PREFIX=${s3_prefix}
AWS_DEFAULT_REGION=$REGION
EOF

docker rm -f pioneer-worker >/dev/null 2>&1 || true

docker run -d \
  --name pioneer-worker \
  --restart unless-stopped \
  --env-file /etc/pioneer-worker.env \
  --log-driver awslogs \
  --log-opt awslogs-region="$REGION" \
  --log-opt awslogs-group="${log_group}" \
  --log-opt awslogs-create-group=true \
  --log-opt awslogs-stream="$INSTANCE_ID" \
  "$IMAGE"

echo "== Pioneer Square worker bootstrap complete: container $(docker inspect -f '{{.Id}}' pioneer-worker) =="
