variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "ap-southeast-2"
}

variable "instance_name" {
  description = "Value of the Name tag on the EC2 instance."
  type        = string
  default     = "nexus-server"
}

variable "instance_type" {
  description = "EC2 instance type (2 vCPU / 8 GB RAM)."
  type        = string
  default     = "m7i-flex.large"
}

variable "ami_id" {
  description = "AMI ID. Leave empty to automatically use the latest Ubuntu 24.04 LTS (Canonical) image in the region."
  type        = string
  default     = ""
}

variable "root_volume_gb" {
  description = "Root disk size in GB. The NEXUS project constraint is 25 GB - do not raise it without deciding to."
  type        = number
  default     = 25
}

variable "ssh_public_key" {
  description = "Contents of your SSH PUBLIC key (e.g. the text of ~/.ssh/id_ed25519.pub). Never put a private key here."
  type        = string
}

variable "admin_cidr" {
  description = "The ONLY address allowed to reach the server, in CIDR form, e.g. 203.0.113.10/32. Find yours at https://checkip.amazonaws.com"
  type        = string

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0)) && var.admin_cidr != "0.0.0.0/0"
    error_message = "admin_cidr must be a valid CIDR and must not be 0.0.0.0/0 (SSH open to the world)."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "subnet_cidr" {
  description = "CIDR block for the public subnet."
  type        = string
  default     = "10.20.1.0/24"
}

variable "enable_ecr_access" {
  description = "Attach an IAM role letting the instance push/pull ECR images (so no AWS keys are stored on the server)."
  type        = bool
  default     = true
}

variable "webhook_ingress_cidrs" {
  description = "OPTIONAL. CIDRs allowed to reach NEXUS port 8000 so GitHub webhooks can arrive (use GitHub's published 'hooks' ranges from https://api.github.com/meta). Empty = port 8000 stays closed and you use the manual 'Deploy now' button or an SSH tunnel."
  type        = list(string)
  default     = []

  validation {
    condition     = !contains(var.webhook_ingress_cidrs, "0.0.0.0/0")
    error_message = "Do not open NEXUS to the whole internet. List specific CIDRs (GitHub hook ranges)."
  }
}
