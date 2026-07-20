# RDS Postgres — replaces the docker-compose `postgres` service. Fargate
# tasks are ephemeral/stateless, so the database moves to a managed instance
# rather than a container with a mounted volume.
#
# Note: the docker-compose postgres service also enables pg_stat_statements
# and auto_explain via shared_preload_libraries (see docker-compose.yml).
# RDS Postgres supports pg_stat_statements via the DB parameter group below;
# auto_explain is not available on RDS and is dropped in this migration.

resource "aws_db_subnet_group" "main" {
  name       = "${local.name_prefix}-db-subnets"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${local.name_prefix}-db-subnets"
  }
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name_prefix}-pg16"
  family = "postgres16"

  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "pg_stat_statements.max"
    value = "10000"
  }

  parameter {
    name  = "pg_stat_statements.track"
    value = "all"
  }

  parameter {
    name  = "track_io_timing"
    value = "1"
  }
}

resource "aws_db_instance" "postgres" {
  identifier = "${local.name_prefix}-db"

  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name

  multi_az                   = var.db_multi_az
  backup_retention_period    = var.db_backup_retention_days
  deletion_protection        = var.db_deletion_protection
  skip_final_snapshot        = var.db_skip_final_snapshot
  final_snapshot_identifier  = var.db_skip_final_snapshot ? null : "${local.name_prefix}-db-final"
  auto_minor_version_upgrade = true
  publicly_accessible        = false
  copy_tags_to_snapshot      = true

  # Password is managed via var.db_password / SSM (see ssm.tf); rotate through
  # those instead of re-applying with a new literal here.
  lifecycle {
    ignore_changes = [password]
  }

  tags = {
    Name = "${local.name_prefix}-db"
  }
}
