terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # A separate state key from the app module, deliberately (#87). The roles CI
  # is allowed to APPLY must not live in the same root module as the roles that
  # grant CI its access, or a compromised deploy could widen its own trust
  # policy in the same run that it deploys.
  backend "s3" {
    bucket       = "dpic-prdw-tfstate"
    key          = "prdw/ci/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "odisha-prdw"
      Component = "ci"
      ManagedBy = "terraform"
      Milestone = "continuous-delivery"
    }
  }
}
