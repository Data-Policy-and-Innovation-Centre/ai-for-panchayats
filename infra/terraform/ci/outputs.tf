output "push_role_arn" {
  description = "Role for the image build/push workflow to assume. Consumed by .github/workflows as a repository variable, not a secret -- a role ARN is not a credential, and treating it as one hides it from review."
  value       = aws_iam_role.push.arn
}

output "push_role_subject" {
  description = "The exact OIDC `sub` this role trusts. Printed so a workflow that cannot assume the role can be compared against it directly."
  value       = local.push_subject
}

output "account_id" {
  description = "Resolved at apply time so no tracked file has to contain it."
  value       = data.aws_caller_identity.current.account_id
}
