# NEXUS server infrastructure: one VPC, one public subnet, one locked-down EC2.
# Software on the instance (Docker, Jenkins, kubectl, Minikube) is installed by
# the Ansible project in ../ansible, not by Terraform.

data "aws_availability_zones" "available" {
  state = "available"
}

# Latest Ubuntu 24.04 LTS from Canonical (used when var.ami_id is empty).
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  ami_id = var.ami_id != "" ? var.ami_id : data.aws_ami.ubuntu.id
}

# ---------------------------------------------------------------- Networking
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.instance_name}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.instance_name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.subnet_cidr
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.instance_name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${var.instance_name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ------------------------------------------------------------ Security group
# Only SSH from the administrator. NEXUS (8000), Jenkins (8080), Grafana (3000),
# Prometheus (9090) and the Kubernetes API are deliberately NOT opened: reach
# them through an SSH tunnel (see docs/setup). Egress is open so the server can
# download packages and images.
resource "aws_security_group" "nexus" {
  name_prefix = "${var.instance_name}-sg-"
  description = "NEXUS server: SSH from the administrator only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "SSH from administrator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_cidr]
  }

  # Optional: GitHub webhook delivery to NEXUS (signature-checked by NEXUS).
  dynamic "ingress" {
    for_each = length(var.webhook_ingress_cidrs) > 0 ? [1] : []
    content {
      description = "GitHub webhooks to NEXUS"
      from_port   = 8000
      to_port     = 8000
      protocol    = "tcp"
      cidr_blocks = var.webhook_ingress_cidrs
    }
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.instance_name}-sg" }

  lifecycle {
    create_before_destroy = true
  }
}

# ----------------------------------------------------------------- SSH key
resource "aws_key_pair" "admin" {
  key_name_prefix = "${var.instance_name}-"
  public_key      = var.ssh_public_key
}

# --------------------------------------------------------------------- IAM
# Justification: with an instance role, NEXUS/Jenkins can push images to ECR
# without long-lived AWS keys stored on disk. Scope is limited to ECR.
data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  count              = var.enable_ecr_access ? 1 : 0
  name_prefix        = "${var.instance_name}-role-"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

data "aws_iam_policy_document" "ecr" {
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPushPull"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
      "ecr:CreateRepository", "ecr:DescribeRepositories"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ecr" {
  count  = var.enable_ecr_access ? 1 : 0
  name   = "nexus-ecr"
  role   = aws_iam_role.instance[0].id
  policy = data.aws_iam_policy_document.ecr.json
}

resource "aws_iam_instance_profile" "instance" {
  count       = var.enable_ecr_access ? 1 : 0
  name_prefix = "${var.instance_name}-profile-"
  role        = aws_iam_role.instance[0].name
}

# --------------------------------------------------------------------- EC2
resource "aws_instance" "nexus" {
  ami                    = local.ami_id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.nexus.id]
  key_name               = aws_key_pair.admin.key_name
  iam_instance_profile   = var.enable_ecr_access ? aws_iam_instance_profile.instance[0].name : null

  root_block_device {
    volume_size           = var.root_volume_gb # 25 GB by project constraint
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  # IMDSv2 only: blocks the classic SSRF-to-credentials attack.
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2 # 2 so containers can still reach the role credentials
  }

  tags = { Name = var.instance_name }
}
