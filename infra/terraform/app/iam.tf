# #56: two roles with different jobs. The execution role is what ECS itself
# uses to start the container; the task role is what the running application
# gets. Keeping them separate means the application cannot pull images or read
# the secrets ECS injected for it.

# Read the snapshot module's own outputs rather than restating its bucket,
# prefix and key here: duplicated literals drift, and the failure surfaces only
# when a task cannot read its database at start.
data "terraform_remote_state" "snapshot" {
  backend = "s3"

  config = {
    bucket = var.snapshot_state_bucket
    key    = var.snapshot_state_key
    region = var.snapshot_state_region
  }
}

locals {
  snapshot_bucket   = data.terraform_remote_state.snapshot.outputs.snapshot_bucket
  deployment_prefix = data.terraform_remote_state.snapshot.outputs.deployment_prefix
  kms_key_arn       = data.terraform_remote_state.snapshot.outputs.kms_key_arn
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ECS reads the secret at task start and injects it as an environment variable,
# so this permission belongs to the execution role, not the application.
data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.openai.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${var.name}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# Read-only, and only the one prefix that holds deployable snapshots. No write,
# no delete, no listing of any other bucket -- the application must not be able
# to alter the artifact it verifies.
data "aws_iam_policy_document" "task_snapshot" {
  statement {
    sid       = "ReadDeploymentSnapshots"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["arn:aws:s3:::${local.snapshot_bucket}/${local.deployment_prefix}*"]
  }

  statement {
    sid       = "LocateSnapshots"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.snapshot_bucket}"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.deployment_prefix}*"]
    }
  }

  statement {
    sid       = "DecryptSnapshots"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [local.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "task_snapshot" {
  name   = "${var.name}-task-snapshot"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task_snapshot.json
}

# The value is deliberately NOT set here: Terraform state would then carry the
# key. Set it once with:
#   aws secretsmanager put-secret-value --secret-id <name> --secret-string sk-...
resource "aws_secretsmanager_secret" "openai" {
  name        = "${var.name}/openai-api-key"
  description = "OPENAI_API_KEY for the Odisha PR&DW chatbot router"
}
