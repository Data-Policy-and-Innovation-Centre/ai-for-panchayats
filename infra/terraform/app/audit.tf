# #59: the audit half of the security architecture -- who touched the AWS
# account, and what talked to the network the tasks live on.
#
# SCOPE NOTE. CloudTrail is account-wide and region-global, so it does not
# really belong to one application module. It lives here because this is the
# only root module that applies against this account today. If an account
# baseline module is ever created, move the trail and its bucket there; the
# flow log is genuinely VPC-scoped and stays.

data "aws_caller_identity" "current" {}

locals {
  # Constructed rather than referenced. The bucket policy must name the trail
  # in its aws:SourceArn condition, and the trail cannot be created until the
  # policy exists -- referencing aws_cloudtrail.main.arn here is a cycle.
  # Trail ARNs are deterministic, so writing it out breaks the loop.
  trail_arn = "arn:aws:cloudtrail:${var.region}:${data.aws_caller_identity.current.account_id}:trail/${var.name}"
}

# --------------------------------------------------------------------------
# Where the trail writes
# --------------------------------------------------------------------------

resource "aws_s3_bucket" "audit" {
  count = var.enable_cloudtrail ? 1 : 0

  # Bucket names are globally unique, and the account id is the conventional
  # disambiguator. It is read at apply time, never written into a tracked file.
  bucket = "${var.name}-audit-${data.aws_caller_identity.current.account_id}"

  # An audit trail that a routine `terraform destroy` can erase is not an
  # audit trail. Removing this block is the deliberate act of deciding the
  # history is no longer needed -- the same two-step the snapshot bucket uses.
  #
  # Note the consequence: once this has been applied, setting
  # enable_cloudtrail = false fails at PLAN time, because turning the flag off
  # asks Terraform to destroy a bucket it is forbidden to destroy. That is the
  # intended behaviour and not a bug, but it does mean the flag is one-way in
  # place. Deleting the trail after the fact is: delete this lifecycle block,
  # apply, then set the flag.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket                  = aws_s3_bucket.audit[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-S3, not the snapshot CMK. KMS would add a per-request charge on every
# log file for no gain here: these objects are already unreadable without an
# IAM grant, and pointing CloudTrail at a customer key means a key policy that
# must stay in step with the trail or delivery silently stops.
resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.audit[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.audit[0].id

  rule {
    id     = "expire"
    status = "Enabled"

    filter {}

    expiration {
      days = var.audit_retention_days
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "audit_bucket" {
  count = var.enable_cloudtrail ? 1 : 0

  # aws:SourceArn on both statements. Without it any account could name this
  # bucket as its own trail's destination and write into it -- the confused
  # deputy CloudTrail's own documentation warns about.
  statement {
    sid       = "AWSCloudTrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.audit[0].arn]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }

  statement {
    sid       = "AWSCloudTrailWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit[0].arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }

  # Matches the snapshot bucket's posture: reads and writes over plain HTTP
  # are refused outright rather than merely discouraged.
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit[0].arn, "${aws_s3_bucket.audit[0].arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  count = var.enable_cloudtrail ? 1 : 0

  bucket = aws_s3_bucket.audit[0].id
  policy = data.aws_iam_policy_document.audit_bucket[0].json
}

# --------------------------------------------------------------------------
# The trail
# --------------------------------------------------------------------------

# CloudTrail Event history is on by default and free, but it holds 90 days,
# cannot be queried across regions, and -- the part that matters -- records no
# data events at all, so it would never show an overwrite of the snapshot.
resource "aws_cloudtrail" "main" {
  count = var.enable_cloudtrail ? 1 : 0

  name           = var.name
  s3_bucket_name = aws_s3_bucket.audit[0].id

  # An attacker's first move is rarely in the region you are watching.
  is_multi_region_trail         = true
  include_global_service_events = true

  # Hashes each log file and signs the digest, so a deleted or edited log file
  # is detectable after the fact rather than merely unlikely.
  enable_log_file_validation = true

  advanced_event_selector {
    name = "Management events"

    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }

  # The control that makes artifact substitution visible. Management events
  # would show someone changing the bucket policy; only data events show the
  # PutObject that replaced the database, and which principal issued the Get
  # that a task then ran. Volume here is a few events per deployment, so the
  # per-100k-event charge rounds to nothing.
  advanced_event_selector {
    name = "Snapshot bucket data events"

    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }

    field_selector {
      field  = "resources.type"
      equals = ["AWS::S3::Object"]
    }

    field_selector {
      field       = "resources.ARN"
      starts_with = ["arn:aws:s3:::${local.snapshot_bucket}/"]
    }
  }

  depends_on = [aws_s3_bucket_policy.audit]
}

# --------------------------------------------------------------------------
# VPC flow logs
# --------------------------------------------------------------------------

# The tasks run in public subnets with public IPs (see network.tf), so "who
# reached what" is a question that can actually be asked here, and the answer
# is not otherwise recorded anywhere: security groups drop unauthorised
# packets silently, and the load balancer only logs what it accepted.
resource "aws_cloudwatch_log_group" "flow" {
  count = var.enable_flow_logs ? 1 : 0

  name              = "/aws/vpc/${var.name}"
  retention_in_days = var.flow_log_retention_days
}

data "aws_iam_policy_document" "flow_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# AWS's reference policy for this role also lists logs:DescribeLogGroups. It is
# omitted because delivery does not use it, and a permission that is not needed
# is a permission that cannot be misused: verified against the applied flow log,
# which reports DeliverLogsStatus=SUCCESS with records arriving. Should delivery
# ever stop with a permissions error, that is the first thing to add back --
# flow log delivery fails silently, leaving plan and apply clean.
data "aws_iam_policy_document" "flow_write" {
  count = var.enable_flow_logs ? 1 : 0

  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow[0].arn}:*"]
  }
}

resource "aws_iam_role" "flow" {
  count = var.enable_flow_logs ? 1 : 0

  name               = "${var.name}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
}

resource "aws_iam_role_policy" "flow" {
  count = var.enable_flow_logs ? 1 : 0

  name   = "${var.name}-flow-logs"
  role   = aws_iam_role.flow[0].id
  policy = data.aws_iam_policy_document.flow_write[0].json
}

resource "aws_flow_log" "main" {
  count = var.enable_flow_logs ? 1 : 0

  vpc_id       = aws_vpc.main.id
  traffic_type = "ALL"

  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow[0].arn
  iam_role_arn         = aws_iam_role.flow[0].arn

  # 600s, the maximum. One minute would multiply the record count -- and the
  # per-GB ingestion charge -- for finer timing than any question asked here
  # needs. ACCEPT/REJECT and the peer address are unaffected by aggregation.
  max_aggregation_interval = 600
}
