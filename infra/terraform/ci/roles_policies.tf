# ---------------------------------------------------------------------------
# What the PLAN role may do: read the world, read the two state objects, and
# nothing else.
#
# Deliberately NOT the AWS-managed ReadOnlyAccess policy. That grants
# s3:GetObject across the account, which on this account includes
# dpic-prdw-snapshots -- the proprietary warehouse -- and dpic-dvc-cache.
# A role assumed from `pull_request`, which any contributor can trigger, must
# not be able to read those.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "plan" {
  statement {
    sid    = "DescribeWhatTheAppModuleManages"
    effect = "Allow"
    actions = [
      "ec2:Describe*",
      "elasticloadbalancing:Describe*",
      "ecs:Describe*",
      "ecs:List*",
      "ecr:Describe*",
      "ecr:GetLifecyclePolicy",
      "ecr:GetRepositoryPolicy",
      "ecr:ListTagsForResource",
      # NOT cloudfront:Get*. That wildcard includes GetFunction, and
      # auth.tf's own comment records that GetFunction reveals the basic-auth
      # credential compiled into the function body. Enumerated instead, so no
      # future Get* action is admitted by accident.
      #
      # GetFunction ITSELF is still required: the provider refreshes
      # aws_cloudfront_function by reading its code, and a plan that cannot
      # refresh it fails. Removing it would break every infrastructure PR
      # without closing the exposure, because the same credential is in the
      # Terraform state this role must also read. Both locations are #193.
      "cloudfront:GetDistribution",
      "cloudfront:GetDistributionConfig",
      "cloudfront:GetCachePolicy",
      "cloudfront:GetOriginRequestPolicy",
      "cloudfront:GetResponseHeadersPolicy",
      "cloudfront:GetFunction",
      "cloudfront:DescribeFunction",
      "cloudfront:List*",
      "logs:Describe*",
      "logs:ListTagsForResource",
      # NOT iam:Get*/List* on "*". This role is assumed from a pull request,
      # and account-wide IAM read lets any PR enumerate every role and policy
      # here -- including janasunani-ci-deploy's exact trust policy and
      # permissions. The same reasoning that ruled out ReadOnlyAccess above
      # applies to IAM and was missed the first time.
      #
      # iam:ListRoles and iam:ListPolicies USED TO SIT HERE, directly under
      # that comment and contradicting it: ListRoles returns each role's
      # AssumeRolePolicyDocument, so `aws iam list-roles` dumps the account's
      # entire trust graph -- exactly the thing the comment says is excluded.
      # Verified by running it. Reachable by any contributor with push, whose
      # PR runs from its own head, into a log that is world-readable because
      # this repository is public. Terraform refreshes NAMED roles with
      # GetRole, which ReadTheAppModulesOwnRolesInDetail below already grants,
      # so nothing legitimate needed the enumeration.
      "kms:DescribeKey",
      "kms:GetKeyPolicy",
      "kms:ListAliases",
      "cloudtrail:Describe*",
      "cloudtrail:Get*",
      "cloudtrail:ListTags",
      "cloudwatch:Describe*",
      "application-autoscaling:Describe*",
      "servicequotas:Get*",
      "sts:GetCallerIdentity",
      # The secret's METADATA only. GetSecretValue is denied below -- a plan
      # does not need the OpenAI key, and this role is reachable from any
      # contributor's pull request.
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:ListSecrets",
    ]
    resources = ["*"]
  }

  # Reading the app module's own roles in detail is legitimate -- a plan has
  # to diff them. Reading everyone else's is not.
  statement {
    sid    = "ReadTheAppModulesOwnRolesInDetail"
    effect = "Allow"
    actions = [
      "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies", "iam:ListRoleTags",
    ]
    resources = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${var.app_name_prefix}*"]
  }

  statement {
    sid       = "ReadTheBoundaryPolicyItself"
    effect    = "Allow"
    actions   = ["iam:GetPolicy", "iam:GetPolicyVersion"]
    resources = [local.boundary_arn]
  }

  # Both state objects, because app/versions.tf reads
  # data.terraform_remote_state.snapshot -- a plan of the app module fails
  # without the snapshot module's state.
  statement {
    sid     = "ReadBothTerraformStates"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "arn:${data.aws_partition.current.partition}:s3:::${local.state_bucket}/${local.app_state}",
      "arn:${data.aws_partition.current.partition}:s3:::${local.state_bucket}/${local.snapshot_state}",
    ]
  }

  statement {
    sid       = "ListTheStateBucketOnly"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:${data.aws_partition.current.partition}:s3:::${local.state_bucket}"]
  }

  # Terraform REFRESHES every resource before it can diff, so a plan needs a
  # read for each one the app module manages -- not only for the ones a change
  # touches. These three were missing, and a plan would have failed on the
  # budget's refresh before reporting anything. The apply policy got the same
  # treatment one round earlier; the plan policy was not re-checked against it,
  # which is how they diverged. Found by Codex on #187.
  statement {
    sid    = "RefreshWhatTheAppModuleManages"
    effect = "Allow"
    actions = [
      "budgets:ViewBudget",
      "budgets:DescribeBudget",
      "budgets:DescribeBudgets",
      "sns:GetTopicAttributes",
      "sns:GetSubscriptionAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:ListTagsForResource",
      "s3:GetBucketPolicy",
      "s3:GetBucketVersioning",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketTagging",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:GetBucketLogging",
      "s3:GetBucketOwnershipControls",
      "s3:GetBucketAcl",
    ]
    resources = ["*"]
  }

  # The plan role must never write state, never take the lock, and never read
  # a secret value. `plan -lock=false` is what the workflow runs; this makes
  # the flag redundant rather than load-bearing, so forgetting it cannot block
  # an apply on main.
  statement {
    sid    = "NeverWriteStateOrReadSecrets"
    effect = "Deny"
    actions = [
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "secretsmanager:GetSecretValue",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "NeverTouchThisModulesOwnIdentity"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = concat(local.self_role_arns, [local.boundary_arn])
  }
}

# ---------------------------------------------------------------------------
# What the APPLY role may do.
#
# The first draft granted ec2:*, secretsmanager:*, kms:*, s3:PutBucket* and
# more on Resource "*", scoped by action only. Review found that this reaches
# straight out of the app module and into the rest of the account: it could
# read any secret, schedule any KMS key for deletion, and -- via
# s3:PutBucketPolicy, which s3:PutBucket* includes -- rewrite the bucket policy
# of janasunani-documents-main or dpic-prdw-snapshots to make their contents
# world-readable. None of that is an escape from the permissions boundary; the
# boundary only ever gated the IAM actions, and the non-IAM ones were never
# scoped at all.
#
# Everything infra/terraform/app creates is named from its var.name, so the
# scoping below hangs off that prefix. Where a resource genuinely cannot be
# named in advance -- a VPC, a subnet, a security group, all of which get
# generated ids -- the Allow stays broad and a matching Deny removes the
# destructive verbs the app module never needs.
locals {
  acct        = data.aws_caller_identity.current.account_id
  part        = data.aws_partition.current.partition
  app         = var.app_name_prefix
  app_roles   = "arn:${local.part}:iam::${local.acct}:role/${var.app_name_prefix}*"
  app_secrets = "arn:${local.part}:secretsmanager:*:${local.acct}:secret:${var.app_name_prefix}/*"
  app_topics  = "arn:${local.part}:sns:*:${local.acct}:${var.app_name_prefix}*"
  # The one budget alarms.tf creates, not every budget in the account.
  app_budgets = "arn:${local.part}:budgets::${local.acct}:budget/${var.app_name_prefix}*"
  app_buckets = [
    "arn:${local.part}:s3:::${var.app_name_prefix}-audit-${local.acct}",
    "arn:${local.part}:s3:::${var.app_name_prefix}-audit-${local.acct}/*",
  ]
}

data "aws_iam_policy_document" "apply" {
  # Resources whose identifiers are generated at create time and so cannot be
  # named here. The Deny below is what bounds these.
  statement {
    sid    = "ManageUnnameableNetworkAndEdgeResources"
    effect = "Allow"
    actions = [
      "ec2:*",
      "elasticloadbalancing:*",
      "cloudfront:*",
      "cloudwatch:*",
      # application-autoscaling deliberately absent: infra/terraform/app
      # manages no scalable target and service.tf keeps a fixed desired_count
      # on purpose (#54). Granting it account-wide bought nothing and allowed
      # RegisterScalableTarget and PutScalingPolicy against sibling workloads.
      "tag:*",
    ]
    resources = ["*"]
  }

  # Security groups, routes and listeners belong to whoever created them, and
  # ec2:*/elasticloadbalancing:* on "*" let a compromised apply revoke another
  # project's ingress or repoint its routes. Scoped by the tag the provider
  # stamps on everything this project creates; an untagged resource matches the
  # Deny too, because a missing key makes StringNotEquals true.
  statement {
    sid    = "NeverMutateAnotherProjectsNetworkOrEdge"
    effect = "Deny"
    actions = [
      "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
      "ec2:DeleteSecurityGroup", "ec2:ModifySecurityGroupRules",
      "ec2:DeleteRoute", "ec2:ReplaceRoute", "ec2:DeleteRouteTable",
      "ec2:DisassociateRouteTable", "ec2:DeleteSubnet", "ec2:DeleteVpc",
      "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
      "elasticloadbalancing:ModifyListener", "elasticloadbalancing:ModifyRule",
      "cloudfront:UpdateFunction", "cloudfront:PublishFunction",
      "cloudfront:DeleteFunction",
    ]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["odisha-prdw"]
    }
  }

  # The Deny above keys on a MUTABLE tag, and the Allow grants ec2:*,
  # elasticloadbalancing:* and tag:*. Without this an attacker stamps
  # Project=odisha-prdw onto another project's security group, route table or
  # load balancer, the StringNotEquals above stops matching, and every revoke
  # or modify in that list becomes permitted. Same bypass already closed for
  # CloudFront; Codex found the EC2/ELB half in round 3.
  #
  # TWO conditions, ANDed, and the second one is load-bearing. Denying on
  # StringNotEquals alone would also deny tagging an UNTAGGED resource,
  # because a missing key makes StringNotEquals true -- and that is how the
  # provider tags anything it creates without tag-on-create support, so the
  # apply would fail on its own new resources. Null=false narrows this to
  # resources that already carry a Project tag belonging to someone else.
  #
  # Residual, stated rather than hidden: a resource with NO Project tag can
  # still be captured and then mutated. Closing that needs certainty about
  # whether the provider ever calls CreateTags against a fresh untagged
  # resource, which only a real apply establishes. Tracked in #193.
  statement {
    sid    = "NeverRetagAnotherProjectsNetworkOrEdge"
    effect = "Deny"
    actions = [
      "ec2:CreateTags", "ec2:DeleteTags",
      "elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags",
      "tag:TagResources", "tag:UntagResources",
    ]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["odisha-prdw"]
    }
    condition {
      test     = "Null"
      variable = "aws:ResourceTag/Project"
      values   = ["false"]
    }
  }

  # cloudwatch:* on "*" was the only broad grant in this policy with no
  # matching Deny -- ec2 has three, elasticloadbalancing two, cloudfront two,
  # ecs/ecr three. Verified before fixing: cloudwatch:DeleteAlarms against a
  # janasunani alarm returned `allowed`. That is the permission an attacker
  # uses FIRST, because it removes the alerting that would reveal every other
  # action in this policy being used. alarms.tf creates only metric alarms and
  # default_tags stamps them, so the same tag handle works here.
  #
  # TagResource/UntagResource are inside this statement for the same reason
  # they are inside the CloudFront one: a Deny keyed on a mutable tag is only
  # as strong as the tagging permission in front of it.
  statement {
    sid    = "NeverTouchAnotherProjectsAlarms"
    effect = "Deny"
    actions = [
      "cloudwatch:DeleteAlarms", "cloudwatch:PutMetricAlarm",
      "cloudwatch:SetAlarmState", "cloudwatch:DisableAlarmActions",
      "cloudwatch:EnableAlarmActions", "cloudwatch:PutDashboard",
      "cloudwatch:DeleteDashboards", "cloudwatch:TagResource",
      "cloudwatch:UntagResource",
    ]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["odisha-prdw"]
    }
  }

  # The app module runs Fargate and creates no EC2 instance, no key pair and
  # no image. Without this, ec2:* on "*" could terminate janasunani-cpu-box.
  statement {
    sid    = "NeverTouchEC2Instances"
    effect = "Deny"
    actions = [
      "ec2:RunInstances", "ec2:TerminateInstances", "ec2:StopInstances",
      "ec2:StartInstances", "ec2:RebootInstances", "ec2:CreateImage",
      "ec2:ModifyInstanceAttribute", "ec2:CreateKeyPair", "ec2:DeleteKeyPair",
      "ec2:CreateSnapshot", "ec2:DeleteSnapshot", "ec2:DeleteVolume",
      "ec2:AttachVolume", "ec2:DetachVolume", "ec2:PurchaseReservedInstancesOffering",
      "ec2:AcceptVpcPeeringConnection", "ec2:CreateVpcPeeringConnection",
    ]
    resources = ["*"]
  }

  # Review's residual finding: the "unnameable" justification does not hold for
  # ELB. Load balancer, target group and listener ARNs embed the name Terraform
  # chose -- arn:...:loadbalancer/app/prdw-chatbot/<random> -- so the same
  # wildcard trick used for ECR and secrets applies here too.
  statement {
    sid    = "NeverDeleteAnotherProjectsLoadBalancer"
    effect = "Deny"
    actions = [
      "elasticloadbalancing:DeleteLoadBalancer",
      "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:DeleteListener",
      "elasticloadbalancing:DeleteRule",
      "elasticloadbalancing:ModifyLoadBalancerAttributes",
      "elasticloadbalancing:ModifyTargetGroupAttributes",
      "elasticloadbalancing:SetSecurityGroups",
      "elasticloadbalancing:SetSubnets",
    ]
    not_resources = [
      "arn:${local.part}:elasticloadbalancing:*:${local.acct}:loadbalancer/app/${local.app}/*",
      "arn:${local.part}:elasticloadbalancing:*:${local.acct}:targetgroup/${local.app}/*",
      "arn:${local.part}:elasticloadbalancing:*:${local.acct}:listener/app/${local.app}/*",
      "arn:${local.part}:elasticloadbalancing:*:${local.acct}:listener-rule/app/${local.app}/*",
    ]
  }

  # CloudFront distribution ARNs are fully random with no name segment, so
  # ARN-scoping cannot work. default_tags stamps Project = odisha-prdw on
  # everything this project creates, so the tag is the only handle available.
  # A distribution with NO Project tag also matches this Deny, because an
  # absent condition key makes StringNotEquals true -- the safe direction:
  # unknown distributions are protected rather than exposed.
  statement {
    sid    = "NeverTouchAnotherProjectsDistribution"
    effect = "Deny"
    actions = [
      "cloudfront:DeleteDistribution",
      "cloudfront:UpdateDistribution",
      "cloudfront:CreateInvalidation",
      # TagResource and UntagResource belong in this Deny, not outside it.
      # The guard above keys on a MUTABLE tag, and the broad cloudfront:*
      # Allow permits tagging any distribution -- so without these two an
      # attacker tags another project's distribution Project=odisha-prdw,
      # the StringNotEquals stops matching, and update/delete become
      # permitted. Found by Codex on #187.
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
    ]
    resources = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["odisha-prdw"]
    }
  }

  # Both log groups the app module creates are deterministically named, so
  # logs:* on "*" was the same unnecessary breadth as the secrets grant.
  statement {
    sid     = "TheApplicationsOwnLogGroups"
    effect  = "Allow"
    actions = ["logs:*"]
    resources = [
      "arn:${local.part}:logs:*:${local.acct}:log-group:/ecs/${local.app}",
      "arn:${local.part}:logs:*:${local.acct}:log-group:/ecs/${local.app}:*",
      "arn:${local.part}:logs:*:${local.acct}:log-group:/aws/vpc/${local.app}",
      "arn:${local.part}:logs:*:${local.acct}:log-group:/aws/vpc/${local.app}:*",
    ]
  }

  # Enumeration cannot be scoped to an ARN.
  statement {
    sid       = "EnumerateLogGroupsToPlanThem"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups", "logs:ListTagsForResource"]
    resources = ["*"]
  }

  statement {
    sid       = "ManageTheApplicationsOwnServices"
    effect    = "Allow"
    actions   = ["ecs:*", "ecr:*"]
    resources = ["*"]
  }

  # ecs:* on "*" permitted stopping, scaling or deleting any workload in the
  # account, and ecr:* permitted PUSHING to every repository -- the earlier
  # ECR deny only removed deletion and policy calls, so an attacker could
  # still upload an image to grievance-database-backups-main. Both are now
  # bounded to resources this project owns.
  statement {
    sid    = "NeverMutateAnotherProjectsWorkload"
    effect = "Deny"
    actions = [
      "ecs:UpdateService", "ecs:DeleteService", "ecs:StopTask", "ecs:RunTask",
      "ecs:DeleteCluster", "ecs:DeregisterTaskDefinition", "ecs:UpdateServicePrimaryTaskSet",
      "ecs:DeleteTaskSet", "ecs:UpdateCluster",
      # ExecuteCommand is a SHELL inside a running task, so on a sibling
      # workload it yields that task's role credentials and environment --
      # strictly worse than the stop/update calls above it. It acts on a task
      # ARN, so the not_resources list below already bounds it correctly.
      # Found by Codex on #187 round 3.
      "ecs:ExecuteCommand",
    ]
    not_resources = [
      "arn:${local.part}:ecs:*:${local.acct}:cluster/${local.app}",
      "arn:${local.part}:ecs:*:${local.acct}:service/${local.app}/*",
      "arn:${local.part}:ecs:*:${local.acct}:task/${local.app}/*",
      "arn:${local.part}:ecs:*:${local.acct}:task-definition/${local.app}:*",
      "arn:${local.part}:ecs:*:${local.acct}:task-set/${local.app}/*",
    ]
  }

  statement {
    sid           = "NeverPushToAnotherProjectsRegistry"
    effect        = "Deny"
    actions       = ["ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:TagResource"]
    not_resources = ["arn:${local.part}:ecr:*:${local.acct}:repository/${local.app}*"]
  }

  # ECR is account-wide above because Terraform's describe calls need it, so
  # the destructive verbs are removed from every repository except the app's.
  statement {
    sid    = "NeverDeleteAnotherProjectsImages"
    effect = "Deny"
    actions = [
      "ecr:DeleteRepository", "ecr:BatchDeleteImage", "ecr:PutLifecyclePolicy",
      "ecr:DeleteLifecyclePolicy", "ecr:SetRepositoryPolicy", "ecr:DeleteRepositoryPolicy",
    ]
    not_resources = ["arn:${local.part}:ecr:*:${local.acct}:repository/${local.app}*"]
  }

  # One secret, named. Not secretsmanager:* on "*".
  statement {
    sid       = "TheApplicationSecretOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:*"]
    resources = [local.app_secrets]
  }

  statement {
    sid       = "ListSecretsToPlanThem"
    effect    = "Allow"
    actions   = ["secretsmanager:ListSecrets"]
    resources = ["*"]
  }

  # The app module creates no KMS key. It needs to describe the ones it uses
  # and nothing more; the destructive verbs are denied outright rather than
  # scoped, because there is no key here they should ever apply to.
  statement {
    sid       = "DescribeKeysOnly"
    effect    = "Allow"
    actions   = ["kms:DescribeKey", "kms:GetKeyPolicy", "kms:ListAliases", "kms:ListKeys"]
    resources = ["*"]
  }

  statement {
    sid    = "NeverDestroyOrRepolicyAKey"
    effect = "Deny"
    actions = [
      "kms:ScheduleKeyDeletion", "kms:DisableKey", "kms:PutKeyPolicy",
      "kms:DeleteAlias", "kms:DisableKeyRotation", "kms:CreateGrant", "kms:RetireGrant",
    ]
    resources = ["*"]
  }

  # SNS and Budgets, which the first draft omitted entirely -- alarms.tf
  # creates a topic, a subscription and a budget, so an apply would have
  # failed AccessDenied partway with the network already built.
  statement {
    sid       = "TheAlertsTopic"
    effect    = "Allow"
    actions   = ["sns:*"]
    resources = [local.app_topics]
  }

  statement {
    sid       = "ListTopicsToPlanThem"
    effect    = "Allow"
    actions   = ["sns:ListTopics", "sns:ListSubscriptions"]
    resources = ["*"]
  }

  statement {
    sid       = "TheAccountBudget"
    effect    = "Allow"
    actions   = ["budgets:*"]
    resources = [local.app_budgets]
  }

  # CloudTrail plus the one bucket audit.tf creates.
  statement {
    sid       = "TheAuditTrail"
    effect    = "Allow"
    actions   = ["cloudtrail:*"]
    resources = ["*"]
  }

  # StopLogging, DeleteTrail and PutEventSelectors against another workload's
  # trail would let a compromised apply disable the audit evidence before
  # using the rest of this role. The app's trail is named from var.name, so it
  # can be excluded by ARN.
  statement {
    sid    = "NeverSilenceAnotherProjectsTrail"
    effect = "Deny"
    actions = [
      "cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "cloudtrail:UpdateTrail",
      "cloudtrail:PutEventSelectors", "cloudtrail:RemoveTags",
    ]
    not_resources = ["arn:${local.part}:cloudtrail:*:${local.acct}:trail/${local.app}*"]
  }

  statement {
    sid       = "TheAuditBucketOnly"
    effect    = "Allow"
    actions   = ["s3:*"]
    resources = local.app_buckets
  }

  # The specific escalation review found: s3:PutBucket* includes
  # PutBucketPolicy, and a same-account resource policy grants read without
  # any matching identity policy. Denied everywhere except the app's own
  # bucket, so this role cannot open dpic-prdw-snapshots to the world.
  statement {
    sid    = "NeverRepolicyAnotherProjectsBucket"
    effect = "Deny"
    actions = [
      "s3:PutBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketAcl",
      "s3:PutBucketPublicAccessBlock", "s3:DeleteBucket", "s3:PutBucketVersioning",
    ]
    not_resources = local.app_buckets
  }

  statement {
    sid    = "ReadAndWriteTheAppStateOnly"
    effect = "Allow"
    actions = [
      "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject",
    ]
    resources = [
      # The lockfile is `<key>.tflock` -- verified by polling the bucket during
      # a live plan, not assumed. A wrong name here fails every apply on the
      # lock rather than on anything legible.
      "arn:${local.part}:s3:::${local.state_bucket}/${local.app_state}",
      "arn:${local.part}:s3:::${local.state_bucket}/${local.app_state}.tflock",
    ]
  }

  statement {
    sid       = "ReadTheSnapshotStateNeverWriteIt"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["arn:${local.part}:s3:::${local.state_bucket}/${local.snapshot_state}"]
  }

  statement {
    sid       = "ListTheStateBucketOnly"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:${local.part}:s3:::${local.state_bucket}"]
  }

  # IAM, in two statements because the boundary context key only exists for
  # some calls.
  #
  # `iam:PermissionsBoundary` is populated ONLY for requests that themselves
  # set a boundary -- CreateRole, CreateUser, PutRolePermissionsBoundary. The
  # first draft put PutRolePolicy, AttachRolePolicy and UpdateAssumeRolePolicy
  # in the gated statement too, where the condition can never be true, so
  # those calls would have been implicitly denied and the apply would have
  # failed on the first role it tried to configure.
  statement {
    sid    = "CreateAppRolesCarryingTheBoundary"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:PutRolePermissionsBoundary",
    ]
    resources = [local.app_roles]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.boundary_arn]
    }
  }

  # Bounded by the resource ARN instead: only roles named for the app module,
  # never an arbitrary role in the account. The first draft allowed these on
  # "*", which let it delete janasunani-ci-deploy.
  statement {
    sid    = "ConfigureAppRolesOnly"
    effect = "Allow"
    actions = [
      "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:DeleteRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:TagRole",
      "iam:UntagRole", "iam:DeleteRole", "iam:GetRole", "iam:GetRolePolicy",
      "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListRoleTags",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = [local.app_roles]
  }

  # Reads that Terraform performs while refreshing, which cannot be scoped to
  # a single ARN because they enumerate.
  statement {
    sid       = "ReadIamToRefreshState"
    effect    = "Allow"
    actions   = ["iam:ListRoles", "iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicies"]
    resources = ["*"]
  }

  statement {
    sid       = "ServiceLinkedRolesOnly"
    effect    = "Allow"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
  }

  # PassRole was ungated on "*" in the first draft, which is precisely the
  # escalation the module header warns about: it could pass an existing
  # powerful role to ECS and execute as it. Both the destination service and
  # the role are named now.
  statement {
    sid       = "PassOnlyAppRolesAndOnlyToTheirServices"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [local.app_roles]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com", "vpc-flow-logs.amazonaws.com"]
    }
  }

  statement {
    sid    = "NeverEscapeTheBoundary"
    effect = "Deny"
    actions = [
      "iam:DeleteRolePermissionsBoundary",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion",
      "iam:SetDefaultPolicyVersion",
      "iam:DeletePolicy",
      "iam:CreateUser",
      "iam:CreateAccessKey",
      "iam:CreateLoginProfile",
      "iam:UpdateLoginProfile",
      "organizations:*",
      "account:*",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "NeverTouchThisModulesOwnIdentity"
    effect    = "Deny"
    actions   = ["iam:*"]
    resources = concat(local.self_role_arns, [local.boundary_arn])
  }
}

resource "aws_iam_role" "plan" {
  name               = "${var.github_repository}-ci-tf-plan"
  description        = "GitHub Actions on pull requests: terraform plan, read-only, no state writes, no secret values."
  assume_role_policy = data.aws_iam_policy_document.plan_trust.json
}

resource "aws_iam_role_policy" "plan" {
  name   = "tf-plan"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan.json
}

resource "aws_iam_role" "apply" {
  name               = "${var.github_repository}-ci-tf-apply"
  description        = "GitHub Actions in the production environment: terraform apply for infra/terraform/app."
  assume_role_policy = data.aws_iam_policy_document.apply_trust.json

  # An apply that waits for steady state can run for the 20m timeout set in
  # service.tf; the session has to outlive it or credentials expire mid-apply
  # and leave the S3 lock held.
  max_session_duration = 7200
}

resource "aws_iam_role_policy" "apply" {
  name   = "tf-apply"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.apply.json
}
