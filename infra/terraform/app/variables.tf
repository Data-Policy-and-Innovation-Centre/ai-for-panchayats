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
  description = "Fargate CPU units. 1 vCPU, measured under #72."
  type        = string
  # Was 2048. Container Insights over six hours of live serving recorded a peak
  # of 793 CPU units against 2048 reserved -- 39%. Measured p95 latency is 3.6s
  # and is dominated by LLM round trips, not by DuckDB or by CPU, so halving
  # this leaves the same peak at 77% of a smaller allocation without moving the
  # bottleneck. Startup is the one CPU-bound step (SHA-256 over ~1GB, 13s at 2
  # vCPU) and roughly doubles here, which is immaterial against a 420s grace.
  default = "1024"
}

variable "task_memory" {
  description = "Fargate memory in MiB. 4 GB, measured under #72."
  type        = string
  # Was 8192. Peak observed container memory is 630 MiB -- 7.7% of what was
  # reserved. 4096 is deliberately not the tightest fit the measurement would
  # allow: DuckDB memory-maps the ~1GB snapshot, so the page cache wants room
  # the working-set figure does not show, and 1 vCPU only admits 2048-8192 MiB.
  # This keeps roughly 6x headroom over the observed peak and still saves about
  # $45/month against the original pair.
  default = "4096"
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
    Acknowledges serving VIEWERS over plain HTTP. #59 does not accept this for
    release; it exists so a bounded pre-cutover test is possible. It is only
    consulted when neither enable_cdn nor certificate_arn provides TLS.

    Note what this does NOT gate: the chatbot is published to the internet
    without authentication in every configuration here. That is a separate
    exposure, tracked on #59, and no variable in this module withholds it.
  EOT
  type        = bool
  default     = false
}

variable "enable_cdn" {
  description = <<-EOT
    Put CloudFront in front of the load balancer, which gives viewers HTTPS on
    an AWS-issued *.cloudfront.net certificate without a registered domain.
    This is on by default because the dashboard calls crypto.randomUUID()
    during mount: outside a secure context that throws and the page renders
    blank, so plain HTTP does not merely leak traffic, it breaks the app.

    Mutually exclusive with certificate_arn -- with a real certificate,
    terminate TLS at the load balancer and turn this off.
  EOT
  type        = bool
  default     = true
}

variable "alarm_email" {
  description = "Address to subscribe to the alarm topic. Empty creates the topic and alarms but no subscription, so alarms are visible in the console and nothing is emailed."
  type        = string
  default     = ""
}

variable "monthly_cost_alarm_usd" {
  description = "Estimated-charges threshold for the account billing alarm. The chatbot alone projects to about $109/month; the default leaves room for that plus the unrelated workloads sharing this account, while still firing well before a runaway."
  type        = number
  default     = 600
}
