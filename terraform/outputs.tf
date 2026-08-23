output "alb_dns_name" {
  description = "Public DNS name of the ALB. Point var.domain_name's CNAME/ALIAS record here."
  value       = aws_lb.main.dns_name
}

output "alb_zone_id" {
  description = "Route53 hosted zone ID of the ALB, for creating an ALIAS record."
  value       = aws_lb.main.zone_id
}

output "ecr_repository_urls" {
  description = "ECR repository URLs, keyed by service name (backend/foreman/worker)."
  value       = { for k, v in aws_ecr_repository.service : k => v.repository_url }
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket used for assets/uploads/worker session logs."
  value       = aws_s3_bucket.assets.bucket
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster. Tasks run on the ASG-backed capacity provider."
  value       = aws_ecs_cluster.main.name
}

output "ecs_capacity_provider_name" {
  description = "ASG-backed ECS capacity provider used by services and one-off RunTask launches."
  value       = aws_ecs_capacity_provider.asg.name
}

output "ecs_service_names" {
  description = "Names of the long-running ECS services."
  value = {
    backend  = aws_ecs_service.backend.name
    foreman  = aws_ecs_service.foreman.name
    metabase = aws_ecs_service.metabase.name
  }
}

output "ecs_worker_task_definition_family" {
  description = "Task definition family for on-demand worker tasks, launched via ecs:RunTask."
  value       = aws_ecs_task_definition.worker.family
}

output "ecs_migrate_task_definition_family" {
  description = "Task definition family for the one-off alembic migration release task, run via ecs:RunTask by deploy.yml before rolling the services."
  value       = aws_ecs_task_definition.migrate.family
}

output "ecs_migrate_log_group" {
  description = "CloudWatch log group of the migrate task, used by deploy.yml to surface migration output."
  value       = aws_cloudwatch_log_group.migrate.name
}

output "private_subnet_ids" {
  description = "Private subnet IDs, the awsvpc network configuration for one-off ECS tasks (migrate)."
  value       = aws_subnet.private[*].id
}

output "ecs_tasks_security_group_id" {
  description = "Security group shared by ECS tasks, used when launching one-off tasks (migrate)."
  value       = aws_security_group.ecs_tasks.id
}

output "rds_endpoint" {
  description = "RDS Postgres connection endpoint (host:port)."
  value       = aws_db_instance.postgres.endpoint
}

output "github_actions_deploy_role_arn" {
  description = "IAM role ARN for GitHub Actions to assume via OIDC (used as AWS_DEPLOY_ROLE_ARN in the workflows)."
  value       = aws_iam_role.github_actions_deploy.arn
}

output "ecs_task_role_arn" {
  description = "IAM role ARN assumed by application code inside ECS tasks (S3 + Bedrock + worker-dispatch policies)."
  value       = aws_iam_role.ecs_task.arn
}

output "ecs_capacity_asg_name" {
  description = "Name of the Auto Scaling Group that backs the ECS capacity provider."
  value       = aws_autoscaling_group.ecs_capacity.name
}

