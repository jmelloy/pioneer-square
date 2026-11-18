# S3 bucket for uploaded assets, worker session-log sync (PIONEER_S3_BUCKET /
# PIONEER_S3_PREFIX in docker-compose.yml), and general application storage.

resource "aws_s3_bucket" "assets" {
  bucket        = local.s3_bucket_name
  force_destroy = var.s3_force_destroy

  tags = {
    Name = local.s3_bucket_name
  }
}

resource "aws_s3_bucket_versioning" "assets" {
  bucket = aws_s3_bucket.assets.id

  versioning_configuration {
    status = "Suspended" # worker-session logs don't need versioning
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket = aws_s3_bucket.assets.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 15
    }
  }
}

# Deny any non-TLS access to the bucket. ECS task access is granted via the
# IAM policy in iam.tf, not via this bucket policy — this only tightens the
# transport requirement.
resource "aws_s3_bucket_policy" "assets" {
  bucket = aws_s3_bucket.assets.id
  policy = data.aws_iam_policy_document.assets_bucket_policy.json
}

data "aws_iam_policy_document" "assets_bucket_policy" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.assets.arn,
      "${aws_s3_bucket.assets.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

locals {
  s3_bucket_name = var.s3_bucket_name != "" ? var.s3_bucket_name : "${local.name_prefix}-assets-${local.account_id}"
}

# Private S3 bucket for Foreman API audit payloads (backend/api_audit_store.py).
# api_request_log keeps metadata in Postgres; gzip JSON request/response bodies live here.
# Objects are immutable-by-convention (no versioning, no delete permission for the app) and
# expire automatically.

locals {
  audit_bucket_name = var.audit_bucket_name != "" ? var.audit_bucket_name : "${local.name_prefix}-api-audit-${local.account_id}"
  audit_prefix      = "foreman-api-audit"
}

resource "aws_s3_bucket" "audit" {
  bucket        = local.audit_bucket_name
  force_destroy = var.s3_force_destroy

  tags = {
    Name = local.audit_bucket_name
  }
}

resource "aws_s3_bucket_ownership_controls" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    object_ownership = "BucketOwnerEnforced" # disables ACLs
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket = aws_s3_bucket.audit.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id

  versioning_configuration {
    status = "Suspended" # keys are unique per payload; versions would defeat expiry
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    id     = "expire-audit-payloads"
    status = "Enabled"

    filter {
      prefix = "${local.audit_prefix}/"
    }

    expiration {
      days = var.audit_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "audit_bucket_policy" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.audit.arn,
      "${aws_s3_bucket.audit.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyOldTLS"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.audit.arn,
      "${aws_s3_bucket.audit.arn}/*",
    ]

    condition {
      test     = "NumericLessThan"
      variable = "s3:TlsVersion"
      values   = ["1.2"]
    }
  }

  statement {
    sid    = "DenyUnencryptedUploads"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["AES256"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = data.aws_iam_policy_document.audit_bucket_policy.json

  depends_on = [aws_s3_bucket_public_access_block.audit]
}

# Least privilege for the shared ECS task role: write + read of audit objects only.
# No DeleteObject (expiry is lifecycle-managed). ListBucket is scoped to the prefix so a
# missing/expired key returns 404 rather than 403, which the reader maps to "expired".
data "aws_iam_policy_document" "ecs_task_audit" {
  statement {
    sid       = "ListAuditPrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.audit.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.audit_prefix}/*"]
    }
  }

  statement {
    sid       = "WriteReadAuditObjects"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.audit.arn}/${local.audit_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "ecs_task_audit" {
  name   = "${local.name_prefix}-ecs-task-audit"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task_audit.json
}
