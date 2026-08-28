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

variable "snapshot_state_bucket" {
  description = <<-EOT
    S3 bucket holding the snapshot module's state. Must match the bucket the
    snapshot module was itself initialized against -- see
    bootstrap_state_bucket.sh's TF_STATE_BUCKET override. A mismatch here
    either finds no state at all or reads stale outputs from the default
    bucket instead of the one the override actually wrote to.
  EOT
  type        = string
  default     = "dpic-prdw-tfstate"
}

variable "snapshot_state_region" {
  description = "Region of the snapshot module's state bucket. Must match the snapshot module's own backend region."
  type        = string
  default     = "ap-south-1"
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
  # Peak observed CPU is 793 units against 2048 -- 39% -- and measured p95
  # latency is 3.6s dominated by LLM round trips rather than by DuckDB or by
  # CPU. Halving leaves that peak at 77% of the smaller allocation without
  # moving the bottleneck. The one CPU-bound step is the SHA-256 over ~1GB at
  # startup, 13s at 2 vCPU, which roughly doubles and stays far inside the
  # 420s health-check grace.
  default = "1024"
}

variable "task_memory" {
  description = "Fargate memory in MiB. 4 GB, measured under #72."
  type        = string
  # Peak 586 MiB, sampled at ONE-MINUTE resolution across a full cold start --
  # snapshot fetch, database open, view and cache-table construction, and the
  # vector index build. The earlier 630 MiB figure was five-minute data from an
  # already-running task and could not have seen a startup spike; this can.
  #
  # 4096 is not the tightest fit the measurement allows. DuckDB memory-maps the
  # ~1GB snapshot, so the page cache wants room the working-set figure does not
  # show, and 1 vCPU only admits 2048-8192 MiB anyway. That leaves about 7x
  # headroom over the observed peak.
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

    Note what this does NOT gate: whether viewers must authenticate. That is
    enable_basic_auth, further down this file, and the two are independent --
    plain HTTP with a password and HTTPS without one are both reachable states.
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
  description = "Address subscribed to the alarm topic and the budget notifications."
  type        = string
  # Committed rather than left empty or kept in a gitignored tfvars. Empty means
  # the alarms publish to a topic nobody is subscribed to and NO budget is
  # created at all, which is worse than the address being visible -- and it is
  # already visible, in the author line of every commit in this repository.
  # A tfvars would keep it out of the tree at the cost of a clean checkout
  # silently dropping both the subscription and the budget.
  default = "yashaswi@uchicagotrust.org"
}

variable "monthly_cost_alarm_usd" {
  description = "Monthly budget threshold. Account-wide, not per-project: linked accounts cannot use cost allocation tags as a cost filter, so this counts every workload sharing the account."
  type        = number
  # The account's baseline is roughly $390/month from workloads that are not
  # this project. This deployment is about $52. $450 therefore sits ~$60 above
  # the baseline -- about the size of the whole deployment -- so a runaway here
  # is caught while the existing unrelated spend does not trip it. If the other
  # workloads grow this will start firing on them, which is a signal about the
  # account rather than a false alarm.
  default = 450
}

variable "cpu_architecture" {
  description = "Fargate CPU architecture. Must match the architecture the image was built for; a mismatch registers cleanly and then no task can start."
  type        = string
  # ARM64, because that is what is deployed. A default that disagrees with
  # reality is the dangerous one here: image_tag has no default so Terraform
  # forces the operator to supply it, but this one would be silently dropped --
  # a clean checkout running `apply -var image_tag=...-arm64` would register an
  # X86_64 revision pointing at an arm64-only image and exit 0.
  default = "ARM64"

  validation {
    condition     = contains(["X86_64", "ARM64"], var.cpu_architecture)
    error_message = "cpu_architecture must be X86_64 or ARM64."
  }
}

variable "enable_cloudtrail" {
  description = "Create the account trail and its bucket. Off only where a trail already exists at the organisation level -- two trails means paying twice for the same events."
  type        = bool
  default     = true
}

variable "audit_retention_days" {
  description = "How long CloudTrail log files are kept in S3."
  type        = number
  # 400 rather than 365: an annual review looking back a full year still finds
  # the beginning of that year present. Volume is a few MB a month at this
  # size, so the extra five weeks costs cents.
  default = 400
}

variable "enable_flow_logs" {
  description = "Record VPC flow logs to CloudWatch Logs."
  type        = bool
  default     = true
}

variable "flow_log_retention_days" {
  description = "How long flow log records are kept."
  type        = number
  # Flow logs answer 'what reached this VPC recently', which is an incident
  # question, not an audit question -- CloudTrail is the durable record. 30
  # days matches the application log group so the two can be read side by side.
  default = 30
}

variable "request_flood_threshold" {
  description = "Origin requests in a 5-minute period that count as abnormal, sustained over two periods."
  type        = number
  # Measured, not guessed: over the first 30 hours of real testing -- including
  # a benchmark run -- the busiest five minutes at the load balancer carried 24
  # requests of all kinds, page loads included, not 24 questions. 300 is more
  # than ten times that, and less than a scripted client produces in a minute.
  default = 300
}

variable "enable_basic_auth" {
  description = "Gate the distribution behind one shared HTTP Basic credential. Read the password with `terraform output -raw basic_auth_password`."
  type        = bool
  # On by default: this deployment's URL is published in a public repository,
  # so an unprotected apply is exposed the moment it finishes, and defaulting
  # to off would make that the easy path.
  default = true
}

variable "basic_auth_username" {
  description = "Username half of the shared credential. Not a secret; the password is."
  type        = string
  default     = "pilot"
}
