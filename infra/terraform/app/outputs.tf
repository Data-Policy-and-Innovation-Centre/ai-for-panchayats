output "url" {
  description = <<-EOT
    Where testers reach the chatbot, and what the frontend must be built
    against: the bundle hardcodes its API base at build time.
  EOT
  value = (
    var.enable_cdn ? "https://${aws_cloudfront_distribution.app[0].domain_name}" :
    var.certificate_arn == "" ? "http://${aws_lb.main.dns_name}" :
    "https://${aws_lb.main.dns_name}"
  )
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "snapshot_bucket" {
  description = "Read from the snapshot module's state, not restated here."
  value       = local.snapshot_bucket
}

output "openai_secret_name" {
  description = "Set its value with `aws secretsmanager put-secret-value`."
  value       = aws_secretsmanager_secret.openai.name
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

output "service" {
  value = aws_ecs_service.app.name
}

output "audit_bucket" {
  description = "CloudTrail destination. Empty when enable_cloudtrail is false."
  value       = var.enable_cloudtrail ? aws_s3_bucket.audit[0].id : ""
}

output "flow_log_group" {
  description = "VPC flow log group. Empty when enable_flow_logs is false."
  value       = var.enable_flow_logs ? aws_cloudwatch_log_group.flow[0].name : ""
}
