#!/usr/bin/env bash
# Create and harden the Terraform state bucket.
#
# Terraform cannot create the bucket that holds its own state, so this one-off
# step runs before `terraform init`. State locking uses S3 conditional writes
# (`use_lockfile`), which needs no DynamoDB table on Terraform >= 1.10.
#
# Re-running is safe: the script enforces a baseline and refuses to weaken
# anything already stronger than that baseline.
set -euo pipefail

# -chdir is relative to the caller's working directory, so any command this
# script advertises must be anchored to the script's own location rather than
# assuming it was invoked from infra/terraform.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# These defaults must match the backend block in snapshot/versions.tf. A
# Terraform backend cannot read variables, so an override here cannot reach it
# automatically -- the script prints the required init flags instead of
# reporting success against a bucket Terraform will never use.
DEFAULT_BUCKET="dpic-prdw-tfstate"
DEFAULT_REGION="ap-south-1"

BUCKET="${TF_STATE_BUCKET:-$DEFAULT_BUCKET}"
if [ "$BUCKET" = "$DEFAULT_BUCKET" ]; then
  # versions.tf pins this bucket's region, so an ambient AWS_REGION must not
  # make the cross-region guard reject a correctly placed default bucket.
  REGION="$DEFAULT_REGION"
else
  REGION="${AWS_REGION:-$DEFAULT_REGION}"
fi

# Every call is pinned to $REGION. Omitting it would let a differently
# configured profile target another region than the bucket was created in.
aws_s3() { aws s3api "$@" --region "$REGION"; }

# head-bucket fails for "does not exist" and for "exists but you may not see
# it" alike. Discarding stderr would send a 403 down the create path, where it
# hits BucketAlreadyOwnedByYou and set -e aborts before any hardening runs.
if head_err="$(aws_s3 head-bucket --bucket "$BUCKET" 2>&1)"; then
  bucket_exists=true
elif printf '%s' "$head_err" | grep -qE '404|Not Found'; then
  bucket_exists=false
else
  echo "cannot determine whether s3://$BUCKET exists:" >&2
  printf '%s\n' "$head_err" >&2
  exit 1
fi

if [ "$bucket_exists" = true ]; then
  # A bucket already sitting in another region reports success, skips the
  # override warning below, and then breaks `terraform init` -- the same silent
  # divergence this script exists to prevent.
  if ! actual_region="$(aws_s3 get-bucket-location --bucket "$BUCKET" \
      --query 'LocationConstraint' --output text 2>&1)"; then
    echo "cannot read the region of s3://$BUCKET:" >&2
    printf '%s\n' "$actual_region" >&2
    exit 1
  fi
  # The API returns None for us-east-1.
  [ "$actual_region" = "None" ] && actual_region="us-east-1"
  if [ "$actual_region" != "$REGION" ]; then
    echo "s3://$BUCKET is in $actual_region, not $REGION; refusing to continue" >&2
    exit 1
  fi
  echo "bucket $BUCKET already exists in $actual_region"
else
  # us-east-1 is the API's default and rejects an explicit LocationConstraint,
  # so the argument must be omitted there rather than passed unconditionally.
  if [ "$REGION" = "us-east-1" ]; then
    aws_s3 create-bucket --bucket "$BUCKET"
  else
    aws_s3 create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
  echo "created $BUCKET in $REGION"
fi

aws_s3 put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled

aws_s3 put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# AES256 is the baseline, not a target. Replacing an existing SSE-KMS
# configuration with it would be a silent downgrade on a documented-idempotent
# re-run.
if sse_out="$(aws_s3 get-bucket-encryption --bucket "$BUCKET" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
    --output text 2>&1)"; then
  current_sse="$sse_out"
elif printf '%s' "$sse_out" | grep -q "ServerSideEncryptionConfigurationNotFoundError"; then
  current_sse="none"
else
  # Anything else -- AccessDenied, throttling -- must not be read as "unencrypted".
  echo "cannot read the encryption configuration of s3://$BUCKET:" >&2
  printf '%s\n' "$sse_out" >&2
  exit 1
fi
if [ "$current_sse" = "aws:kms" ]; then
  echo "keeping existing SSE-KMS encryption (stronger than this baseline)"
else
  aws_s3 put-bucket-encryption --bucket "$BUCKET" \
    --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
fi

# Terraform state records resource metadata and can carry sensitive values, so
# refuse to move it over a plaintext connection. Blocking public access alone
# does not stop an authenticated caller configured with an HTTP S3 endpoint.
# An existing policy is never overwritten: it may carry statements this script
# knows nothing about.
if policy_out="$(aws_s3 get-bucket-policy --bucket "$BUCKET" --query Policy --output text 2>&1)"; then
  current_policy="$policy_out"
elif printf '%s' "$policy_out" | grep -q "NoSuchBucketPolicy"; then
  current_policy=""
else
  echo "cannot read the bucket policy of s3://$BUCKET:" >&2
  printf '%s\n' "$policy_out" >&2
  exit 1
fi

# 6. A Sid substring match would accept a statement whose effect was flipped or
#    whose condition was dropped, then report the bucket as TLS-only.
if printf '%s' "$current_policy" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
for st in doc.get("Statement", []):
    if (
        st.get("Effect") == "Deny"
        and st.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") in ("false", False)
        and "s3:*" in (st.get("Action") if isinstance(st.get("Action"), list) else [st.get("Action")])
    ):
        sys.exit(0)
sys.exit(1)
'; then
  echo "keeping existing bucket policy (already denies insecure transport)"
elif [ -n "$current_policy" ]; then
  echo "WARNING: s3://$BUCKET has a bucket policy without DenyInsecureTransport." >&2
  echo "Refusing to replace it. Add the statement manually, then re-run." >&2
  exit 1
else
  aws_s3 put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": ["arn:aws:s3:::$BUCKET", "arn:aws:s3:::$BUCKET/*"],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
POLICY
)"
fi

echo "state bucket ready: s3://$BUCKET (versioned, private, encrypted, TLS-only)"

if [ "$BUCKET" != "$DEFAULT_BUCKET" ] || [ "$REGION" != "$DEFAULT_REGION" ]; then
  cat <<NOTE

WARNING: you overrode the defaults, but snapshot/versions.tf hard-codes
  bucket = "$DEFAULT_BUCKET"
  region = "$DEFAULT_REGION"
A backend block cannot read variables, so \`terraform init\` would target those
and ignore the bucket this script just prepared. Initialise with:

  terraform -chdir="$SCRIPT_DIR/snapshot" init -reconfigure -backend-config="bucket=$BUCKET" -backend-config="region=$REGION"

NOTE
fi
