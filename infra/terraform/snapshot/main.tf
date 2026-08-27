# Private, versioned, KMS-encrypted home for deployable DuckDB snapshots.
#
# The deployed database is an immutable file, not a service: there is no
# database endpoint, security group or port here. The security perimeter for
# the data is this bucket's policy, the customer-managed key, and the SHA-256
# the application verifies before it opens the file.
#
# Snapshot objects are never mutated in place. A new artifact is a new object
# version; rollback selects a prior version. Versioning is therefore load
# bearing, not merely a safety net.

data "aws_caller_identity" "current" {}

resource "aws_kms_key" "snapshots" {
  description             = "Encrypts Odisha PR&DW deployable DuckDB snapshots"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "snapshots" {
  name          = "alias/dpic-prdw-snapshots"
  target_key_id = aws_kms_key.snapshots.key_id
}

resource "aws_s3_bucket" "snapshots" {
  bucket = var.snapshot_bucket_name
}

resource "aws_s3_bucket_versioning" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.snapshots.arn
      sse_algorithm     = "aws:kms"
    }
    # One data key per request would be charged per part of a ~1 GB multipart
    # upload and per task download.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshots" {
  bucket     = aws_s3_bucket.snapshots.id
  depends_on = [aws_s3_bucket_versioning.snapshots]

  rule {
    id     = "retain-rollback-window"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    # A failed ~1 GB multipart upload otherwise bills storage indefinitely.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  policy = data.aws_iam_policy_document.snapshots.json

  depends_on = [aws_s3_bucket_public_access_block.snapshots]
}

data "aws_iam_policy_document" "snapshots" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:*"]
    resources = [aws_s3_bucket.snapshots.arn, "${aws_s3_bucket.snapshots.arn}/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyUnencryptedObjectUploads"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.snapshots.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}
