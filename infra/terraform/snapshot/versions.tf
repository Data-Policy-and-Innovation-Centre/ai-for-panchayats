terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State locking uses S3 conditional writes, so no DynamoDB table is needed.
  # The bucket itself is created by ../bootstrap_state_bucket.sh.
  backend "s3" {
    bucket       = "dpic-prdw-tfstate"
    key          = "prdw/snapshot/terraform.tfstate"
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
      Component = "duckdb-snapshot"
      ManagedBy = "terraform"
      Milestone = "current-deployment"
    }
  }
}
