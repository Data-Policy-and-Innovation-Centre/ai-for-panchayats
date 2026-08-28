variable "region" {
  description = "AWS region hosting the deployment snapshot."
  type        = string
  default     = "ap-south-1"
}

variable "snapshot_bucket_name" {
  description = "Private, versioned bucket holding deployable DuckDB snapshots."
  type        = string
  default     = "dpic-prdw-snapshots"
}

variable "deployment_prefix" {
  description = "Key prefix the application task role is allowed to read."
  type        = string
  default     = "duckdb/"
}

variable "noncurrent_version_retention_days" {
  description = <<-EOT
    How long superseded snapshot versions stay retrievable. Rollback restores a
    prior object version, so this is the rollback window and must not be
    shortened without accepting a shorter one.
  EOT
  type        = number
  default     = 180
}
