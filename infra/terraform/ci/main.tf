# CI identity: how GitHub Actions gets short-lived AWS credentials.
#
# Before this module, every push to ECR was a human running
# `aws ecr get-login-password`. The alternative -- an access key pasted into
# repository secrets -- is a long-lived credential in the settings of a public
# repository, which is the failure this exists to avoid.

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

# The OIDC provider is NOT declared as a resource, and that is the single most
# important line in this module (#87).
#
# The issue was written on the premise that no provider existed. One does: it
# was created 2026-08-25 and `janasunani-ci-deploy` -- a SIBLING repository's
# deploy role -- already trusts it. An IAM OIDC provider is an account-level
# singleton keyed by URL, so declaring it here would either fail on
# EntityAlreadyExists or, if imported, put a resource this module can destroy
# in the path of another project's entire deployment pipeline. `terraform
# destroy` on this module would then take janasunani's CI down with it.
#
# Reading it instead means this module can be created and destroyed freely and
# the shared provider outlives both. Its thumbprint and client ID list are
# whoever owns it to manage; we depend only on its ARN.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# Owned by infra/terraform/app. Read here so this module grants access to it
# without being able to change or delete it -- the "no resource in it is
# managed by infra/terraform/app" half of this issue's first criterion.
data "aws_ecr_repository" "app" {
  name = var.ecr_repository_name
}

locals {
  # The exact `sub` claim, assembled once. Written as a single string with no
  # wildcard anywhere: `repo:owner/name:ref:refs/heads/main` matches runs on
  # that branch and nothing else. A `repo:owner/name:*` would additionally
  # match every pull request, every other branch and every tag -- including a
  # PR opened from a fork against this repository.
  push_subject = "repo:${var.github_owner}/${var.github_repository}:ref:refs/heads/${var.deploy_branch}"
}

data "aws_iam_policy_document" "push_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    # Both conditions are required, and dropping either is a real hole.
    #
    # Without the `aud` check the role trusts tokens minted for a different
    # audience. Without the `sub` check -- or with StringLike and a wildcard --
    # it trusts every repository in every organisation that uses GitHub
    # Actions, because the provider itself is not repository-specific.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.push_subject]
    }
  }
}

# Two statements because ECR splits its permissions across two resource scopes
# and collapsing them would grant more than intended.
data "aws_iam_policy_document" "push" {
  # GetAuthorizationToken is a REGISTRY-level action: it has no repository
  # resource to scope to and is only valid on "*". It returns a token good for
  # the whole registry, which is why every other permission below is scoped
  # tightly -- the token is not the authorisation, the policy is.
  statement {
    sid       = "AuthenticateToRegistry"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Everything that touches image data, pinned to the one repository.
  #
  # The read actions are not an oversight: `docker push` reads before it
  # writes. BatchCheckLayerAvailability is how it skips layers the registry
  # already has, and BatchGetImage/GetDownloadUrlForLayer are how a build
  # resolves an existing tag. Without them a push still works but re-uploads
  # every layer each time.
  #
  # Deliberately absent: DeleteRepository, BatchDeleteImage, PutLifecyclePolicy
  # and SetRepositoryPolicy. Tags are immutable in this registry, so CI never
  # needs to remove anything, and a role that cannot delete cannot be used to
  # erase the image a running task is pinned to.
  statement {
    sid    = "PushToOneRepository"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
    ]
    resources = [data.aws_ecr_repository.app.arn]
  }
}

resource "aws_iam_role" "push" {
  name        = "${var.github_repository}-ci-ecr-push"
  description = "GitHub Actions on ${var.deploy_branch}: push images to ${var.ecr_repository_name} and nothing else."

  assume_role_policy = data.aws_iam_policy_document.push_trust.json

  # An hour is far longer than a build needs, but the credential is scoped to
  # one repository's image data, so the blast radius of the extra time is a
  # push nobody asked for -- against immutable tags, which reject it.
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "push" {
  name   = "ecr-push"
  role   = aws_iam_role.push.id
  policy = data.aws_iam_policy_document.push.json
}
