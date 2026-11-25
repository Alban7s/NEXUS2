terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Credentials are NEVER written here. The AWS provider reads them from the
# normal credential chain: `aws configure`, AWS_PROFILE, or AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY environment variables.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "nexus"
      ManagedBy = "terraform"
    }
  }
}
