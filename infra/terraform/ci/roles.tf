# The two roles that run Terraform: one that may only look, one that may act.
#
# Applying infra/terraform/app touches a VPC, subnets, security groups, an ALB,
# ECR, an ECS cluster and service, log groups, a Secrets Manager secret, a
# CloudFront distribution -- and IAM roles, including iam:PassRole. CreateRole
# plus AttachRolePolicy plus PassRole is privilege escalation unless bounded,
# so "least privilege" here is really BOUNDED ADMIN and is planned as one.

locals {
  # Distinct subjects, both exact. `:pull_request` is the sub GitHub mints for
  # every pull-request-family event regardless of target branch, so it cannot
  # collide with the push role's `ref:refs/heads/main`.
  plan_subject  = "repo:${var.github_owner}/${var.github_repository}:pull_request"
  apply_subject = "repo:${var.github_owner}/${var.github_repository}:environment:production"

  state_bucket   = "dpic-prdw-tfstate"
  app_state      = "prdw/app/terraform.tfstate"
  snapshot_state = "prdw/snapshot/terraform.tfstate"

  # This module's OWN resources. Neither role may touch them: a role that can
  # rewrite the trust policy granting it access is not bounded by anything.
  # Listed as ARNs built from the names rather than from the resources
  # themselves, because referring to aws_iam_role.apply inside the policy
  # attached to aws_iam_role.apply is a dependency cycle.
  self_role_arns = [
    "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.github_repository}-ci-ecr-push",
    "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.github_repository}-ci-tf-plan",
    "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.github_repository}-ci-tf-apply",
  ]
  boundary_arn = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/${var.github_repository}-ci-apply-boundary"
}

# ---------------------------------------------------------------------------
# The permissions boundary.
#
# This is the ceiling on any role the apply role creates. It is NOT the apply
# role's own permissions -- it is the maximum any role it mints can ever have,
# enforced by IAM at the moment that role tries to act.
#
# It deliberately omits iam:* entirely, so a role created by the apply role can
# never itself create roles. That is what stops the escalation chain at depth
# one rather than letting it recurse.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "apply_boundary" {
  # Registry and log actions stay on "*": ECR auth is registry-level, and a
  # task's log stream ARN is not known when the boundary is written.
  statement {
    sid    = "RegistryAndLogs"
    effect = "Allow"
    actions = [
      "ecr:GetAuthorizationToken",
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
      "kms:Decrypt",
      "kms:DescribeKey",
    ]
    resources = ["*"]
  }

  # The data reads, NAMED. These were on "*" until Codex raised it on #187 and
  # #186 was filed for it. A boundary is a ceiling, so "*" here meant a role
  # the apply role creates could carry s3:GetObject over dpic-prdw-snapshots,
  # dpic-dvc-cache and janasunani-documents-main, and GetSecretValue over every
  # secret in the account -- bypassing the carefully scoped grants on the apply
  # policy itself by the simple route of minting a role and running a task as
  # it.
  #
  # The tradeoff, recorded because it is a real one: a boundary is meant to
  # bound roles that do not exist yet, and naming today's two resources means
  # editing this when a third appears. That is the correct direction to fail --
  # a new resource gets an explicit decision instead of silent access.
  statement {
    sid    = "OnlyTheSnapshotAndTheApplicationSecret"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
      # Present because prdw-chatbot-task's inline policy already grants it and
      # a boundary is an INTERSECTION: attaching this boundary without it would
      # silently strip GetBucketLocation from the running task, and boto3 uses
      # it for region resolution. Verified against the live role before the
      # boundary was made mandatory, rather than discovered in production.
      "s3:GetBucketLocation",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::dpic-prdw-snapshots",
      "arn:${data.aws_partition.current.partition}:s3:::dpic-prdw-snapshots/*",
    ]
  }

  statement {
    sid       = "OnlyTheApplicationSecret"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:${data.aws_partition.current.partition}:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:${var.app_name_prefix}/*"]
  }

  # Named as a hard Deny rather than merely left out. A boundary is evaluated
  # as an intersection, so omission would already exclude these -- but writing
  # them down means a later edit that widens the Allow above cannot
  # accidentally re-admit them.
  statement {
    sid       = "NeverIdentityOrTerraformState"
    effect    = "Deny"
    actions   = ["iam:*", "sts:AssumeRole", "organizations:*", "account:*"]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "apply_boundary" {
  name        = "${var.github_repository}-ci-apply-boundary"
  description = "Ceiling for any role the CI apply role creates. Not that role's own permissions."
  policy      = data.aws_iam_policy_document.apply_boundary.json
}

# ---------------------------------------------------------------------------
# Trust policies.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "plan_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.plan_subject]
    }
  }
}

data "aws_iam_policy_document" "apply_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # `environment:production` is minted only for a job that declares
    # `environment: production`. Deployment to that environment is restricted
    # to the main branch by the environment's own branch policy, so the branch
    # restriction is enforced by GitHub rather than duplicated here.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.apply_subject]
    }
  }
}
