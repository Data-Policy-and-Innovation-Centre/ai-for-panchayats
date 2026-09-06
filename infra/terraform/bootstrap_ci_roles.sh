#!/usr/bin/env bash
# Apply infra/terraform/ci with human credentials.
#
# This step is manual once, and necessarily so: the roles this module creates
# are the roles CI assumes, so there is no CI role available to create the
# first CI role. Every later change to this module can go through the normal
# review path; only the first apply needs a person.
#
# Re-running is safe. Terraform converges, and the preflight checks below
# refuse to act rather than guessing when something is not as expected.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="$SCRIPT_DIR/ci"

# Must match the backend block in ci/versions.tf. A Terraform backend cannot
# read variables, so these are duplicated here on purpose and the script fails
# loudly rather than applying against a bucket Terraform will never use.
STATE_BUCKET="dpic-prdw-tfstate"
STATE_KEY="prdw/ci/terraform.tfstate"
REGION="${AWS_REGION:-ap-south-1}"
OIDC_URL="token.actions.githubusercontent.com"

say() { printf '[bootstrap-ci] %s\n' "$*"; }
die() { printf '[bootstrap-ci] %s\n' "$*" >&2; exit 1; }

for tool in aws terraform; do
  command -v "$tool" >/dev/null 2>&1 || die "required tool not found: $tool"
done

# 1. Who is applying.
#
# The point of the check is the chicken-and-egg: if someone has already wired
# CI and then runs this from a workflow, the CI role would be modifying the
# trust policy that grants it access -- which is exactly the escalation the
# separate state key exists to prevent.
caller_arn="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" \
  || die "no usable AWS credentials; run this with your own, not from CI"
say "applying as $caller_arn"
case "$caller_arn" in
  *:assumed-role/*-ci-*)
    die "this is a CI role. Bootstrap must run with human credentials -- a CI role editing its own trust policy is the escalation this module is arranged to prevent."
    ;;
esac

# 2. The state bucket, which bootstrap_state_bucket.sh creates.
if ! aws s3api head-bucket --bucket "$STATE_BUCKET" --region "$REGION" >/dev/null 2>&1; then
  die "state bucket s3://$STATE_BUCKET is missing or unreadable. Run bootstrap_state_bucket.sh first."
fi
say "state bucket s3://$STATE_BUCKET reachable; this module uses key $STATE_KEY"

# 3. The OIDC provider, which this module READS rather than creates.
#
# It is an account-level singleton shared with other projects' CI roles, so
# this module deliberately does not manage it -- see the comment on the data
# source in ci/main.tf. If it is genuinely absent, create it once by hand; do
# not add a resource block for it, or `terraform destroy` here will take every
# other repository's deployment pipeline down with it.
account_id="$(aws sts get-caller-identity --query Account --output text)"
provider_arn="arn:aws:iam::${account_id}:oidc-provider/${OIDC_URL}"
if ! aws iam get-open-id-connect-provider \
      --open-id-connect-provider-arn "$provider_arn" >/dev/null 2>&1; then
  cat >&2 <<MSG
[bootstrap-ci] The GitHub OIDC provider does not exist in this account.

Create it once, by hand, then re-run:

  aws iam create-open-id-connect-provider \\
    --url https://${OIDC_URL} \\
    --client-id-list sts.amazonaws.com

Do NOT add an aws_iam_openid_connect_provider resource to infra/terraform/ci.
The provider is shared account-wide; a destroy of this module would then
remove other projects' ability to authenticate.
MSG
  exit 1
fi
say "OIDC provider present (read as a data source, not managed here)"

# 4. Converge.
terraform -chdir="$MODULE" init -input=false
terraform -chdir="$MODULE" apply -input=false "$@"

# 5. Hand the results to the workflows that need them.
#
# The role ARN is published as a repository VARIABLE rather than a secret. It
# is not a credential -- possession of it grants nothing without a token whose
# `sub` matches -- and putting it in a secret only hides it from the review
# that should be checking it.
push_role="$(terraform -chdir="$MODULE" output -raw push_role_arn)"
say "done. Wire the workflows with:"
printf '\n  gh variable set AWS_ECR_PUSH_ROLE_ARN --body %q\n\n' "$push_role"
say "trusted subject: $(terraform -chdir="$MODULE" output -raw push_role_subject)"
