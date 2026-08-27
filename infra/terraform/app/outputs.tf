output "url" {
  description = "Where testers reach the chatbot."
  value       = var.certificate_arn == "" ? "http://${aws_lb.main.dns_name}" : "https://${aws_lb.main.dns_name}"
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
