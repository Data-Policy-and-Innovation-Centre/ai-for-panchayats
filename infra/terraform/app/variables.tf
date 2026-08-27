variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "name" {
  description = "Name prefix for every resource in this module."
  type        = string
  default     = "prdw-chatbot"
}

variable "snapshot_state_key" {
  description = "State key of the snapshot module, whose outputs this module reads."
  type        = string
  default     = "prdw/snapshot/terraform.tfstate"
}

variable "image_tag" {
  description = "Image tag to deploy. Required: an empty value registers <repo>: and every task fails to pull."
  type        = string

  validation {
    condition     = length(trimspace(var.image_tag)) > 0
    error_message = "image_tag must name an image already pushed to ECR."
  }
}

variable "task_cpu" {
  description = "Fargate CPU units. 2 vCPU is the starting point #72 will measure."
  type        = string
  default     = "2048"
}

variable "task_memory" {
  description = <<-EOT
    Fargate memory in MiB. The snapshot is ~1 GB on disk and DuckDB memory-maps
    it, so this must leave room for the file plus query working set. #72 sweeps
    this together with the adapter's own memory_limit.
  EOT
  type        = string
  default     = "8192"
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "certificate_arn" {
  description = <<-EOT
    ACM certificate for HTTPS. Leave empty ONLY for pre-cutover testing: the
    listener then serves plain HTTP on port 80, which #59 does not accept for
    release. Supplying a certificate switches the ALB to HTTPS and redirects
    port 80.
  EOT
  type        = string
  default     = ""
}

variable "ingress_cidrs" {
  description = "Who may reach the load balancer. Narrow this for a private test."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "allow_public_http" {
  description = <<-EOT
    Acknowledges serving over plain HTTP without a certificate. #59 does not
    accept this for release; it exists so a bounded pre-cutover test is
    possible. Without it, a certificate_arn is required, so the default apply
    cannot quietly publish an unauthenticated chatbot to the internet.
  EOT
  type        = bool
  default     = false
}
