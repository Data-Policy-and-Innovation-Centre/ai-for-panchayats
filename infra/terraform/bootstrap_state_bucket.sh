#!/usr/bin/env bash
# Create the Terraform state bucket.
#
# Terraform cannot create the bucket that holds its own state, so this one-off
# step runs before `terraform init`. It is idempotent: re-running it on an
# existing, correctly configured bucket is a no-op.
#
# State locking uses S3 conditional writes (`use_lockfile`), which needs no
# DynamoDB table on Terraform >= 1.10.
set -euo pipefail

BUCKET="${TF_STATE_BUCKET:-dpic-prdw-tfstate}"
REGION="${AWS_REGION:-ap-south-1}"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket $BUCKET already exists"
else
  aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION"
  echo "created $BUCKET in $REGION"
fi

aws s3api put-bucket-versioning \
  --bucket "$BUCKET" --versioning-configuration Status=Enabled

aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

# Terraform state records resource metadata and can carry sensitive values, so
# refuse to move it over a plaintext connection. Blocking public access alone
# does not stop an authenticated caller configured with an HTTP S3 endpoint.
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<POLICY
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

echo "state bucket ready: s3://$BUCKET (versioned, private, encrypted, TLS-only)"
