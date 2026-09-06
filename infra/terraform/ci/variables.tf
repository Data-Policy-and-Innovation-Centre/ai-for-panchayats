variable "region" {
  description = "Region for the CI roles. Must match the region holding the ECR repository they push to."
  type        = string
  default     = "ap-south-1"
}

variable "github_owner" {
  description = "GitHub organisation that owns the repository allowed to assume these roles."
  type        = string
  default     = "Data-Policy-and-Innovation-Centre"
}

variable "github_repository" {
  description = "Repository name allowed to assume these roles. Combined with github_owner into the OIDC `sub` claim; never widened with a wildcard."
  type        = string
  default     = "ai-for-panchayats"
}

variable "deploy_branch" {
  description = "The single branch whose workflow runs may push images. A branch other than the deployment branch here would let an unreviewed ref publish to the registry production pulls from."
  type        = string
  default     = "main"
}

variable "ecr_repository_name" {
  description = "The one ECR repository the push role may write to. Created and owned by infra/terraform/app; referenced here, never managed here."
  type        = string
  default     = "prdw-chatbot"
}
