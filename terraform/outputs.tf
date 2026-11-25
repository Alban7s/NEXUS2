output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.nexus.id
}

output "public_ip" {
  description = "Public IP address of the server."
  value       = aws_instance.nexus.public_ip
}

output "ssh_command" {
  description = "Command to log in (replace the key path with your PRIVATE key)."
  value       = "ssh -i ~/.ssh/<your-private-key> ubuntu@${aws_instance.nexus.public_ip}"
}

output "nexus_tunnel_command" {
  description = "Open NEXUS (and Jenkins/Grafana) on your laptop through an SSH tunnel."
  value       = "ssh -i ~/.ssh/<your-private-key> -L 8000:localhost:8000 -L 8080:localhost:8080 -L 3000:localhost:3000 ubuntu@${aws_instance.nexus.public_ip}"
}

output "ansible_inventory_line" {
  description = "Paste into ansible/inventory/hosts.yml."
  value       = "nexus-server ansible_host=${aws_instance.nexus.public_ip} ansible_user=ubuntu"
}

output "root_volume_gb" {
  description = "Configured root disk size."
  value       = var.root_volume_gb
}

output "ami_used" {
  description = "AMI the instance was created from."
  value       = local.ami_id
}
