#!/bin/bash
set -euxo pipefail

cat >/etc/ecs/ecs.config <<EOF
ECS_CLUSTER=${cluster_name}
ECS_ENABLE_CONTAINER_METADATA=true
ECS_AVAILABLE_LOGGING_DRIVERS=["json-file","awslogs"]
ECS_ENABLE_TASK_IAM_ROLE=true
ECS_ENABLE_TASK_IAM_ROLE_NETWORK_HOST=true
EOF
