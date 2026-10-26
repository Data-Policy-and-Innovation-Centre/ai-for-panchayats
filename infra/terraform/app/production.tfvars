# The production variable set, recorded rather than implied (#92 review).
#
# Codex's finding: `terraform apply -var image_tag=...` supplies ONE variable
# and evaluates every other at its default, so an automated deploy would
# silently revert any production input somebody had set by hand. docs/runbook.md
# warns about exactly this for the ALB TLS cutover.
#
# Verified 2026-09-06, not assumed: a plan against live production with only
# image_tag supplied returned "0 to add, 1 to change, 0 to destroy", and the
# single change was this branch's own circuit-breaker block. So today these
# values ARE the defaults and the risk is latent, not active. Writing them down
# is what keeps it latent: once production diverges, this file is where the
# divergence lives, and a `default` that moves in variables.tf can no longer
# move production with it.
#
# image_tag is deliberately absent. It is the one variable the workflow is
# supposed to supply, and pinning it here would make every deploy a no-op.

# --- topology: what a viewer reaches, and over what -------------------------
enable_cdn        = true
certificate_arn   = ""
public_domain     = ""
allow_public_http = false
ingress_cidrs     = ["0.0.0.0/0"]

# --- authentication ---------------------------------------------------------
enable_basic_auth   = true
basic_auth_username = "pilot"

# --- capacity: one task, fixed, no autoscaling (#54) ------------------------
task_cpu         = "1024"
task_memory      = "4096"
desired_count    = 1
cpu_architecture = "ARM64"

# --- audit and cost ---------------------------------------------------------
enable_cloudtrail       = true
audit_retention_days    = 400
enable_flow_logs        = true
flow_log_retention_days = 30
monthly_cost_alarm_usd  = 450
request_flood_threshold = 300
