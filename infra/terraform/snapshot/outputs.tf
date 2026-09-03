output "snapshot_bucket" {
  description = "Bucket holding deployable snapshots."
  value       = aws_s3_bucket.snapshots.id
}

output "snapshot_bucket_arn" {
  value = aws_s3_bucket.snapshots.arn
}

output "deployment_prefix" {
  description = "Prefix the #56 application task role should be scoped to."
  value       = var.deployment_prefix
}

output "kms_key_arn" {
  description = "CMK the #56 task role needs kms:Decrypt on."
  value       = aws_kms_key.snapshots.arn
}

output "kms_key_alias" {
  value = aws_kms_alias.snapshots.name
}
