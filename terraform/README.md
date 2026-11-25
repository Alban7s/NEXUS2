# Terraform - NEXUS AWS infrastructure

Creates: VPC, public subnet, internet gateway, route table, security group
(**SSH from your IP only**), SSH key pair, optional ECR-only IAM role, and one
`m7i-flex.large` Ubuntu 24.04 EC2 instance with a **25 GB** encrypted gp3 disk.

No AWS secrets live in these files. Terraform reads credentials from `aws configure`
or `AWS_*` environment variables.

## Commands (run inside this folder)

```bash
cp terraform.tfvars.example terraform.tfvars   # then edit admin_cidr and ssh_public_key
terraform fmt -check          # formatting
terraform init                # downloads the AWS provider
terraform validate            # checks the configuration is valid
terraform plan -out nexus.plan   # PREVIEW only - changes nothing
```

`terraform apply nexus.plan` **creates billable AWS resources**. Only run it when you have
read the plan and decided to proceed.

## Cleanup

```bash
terraform destroy            # type "yes" only when you really want everything deleted
```

## Notes
* Ports 8000 / 8080 / 3000 / 9090 are **not** opened. Use the `nexus_tunnel_command` output (SSH tunnel).
* The root disk is 25 GB (project constraint). Docker images fill it quickly; see docs/setup (disk hygiene).
* `terraform.tfstate` can contain sensitive values - it is git-ignored. Do not commit or share it.
