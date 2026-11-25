# NEXUS self-hosted CI/CD: GitHub → Jenkins → Docker → Trivy → Minikube

This guide deploys **the NEXUS platform itself** from its GitHub repository. It is separate from `jenkins/Jenkinsfile`, which is the pipeline NEXUS uses to deploy customer applications through its own API.

## Target architecture and responsibilities

1. **GitHub** stores NEXUS source. Jenkins polls the configured branch every five minutes. This avoids exposing Jenkins publicly just for a webhook.
2. **Jenkins** checks out the root `Jenkinsfile`, installs test dependencies in a temporary virtual environment, runs tests, builds the image, runs Trivy, pushes a commit/build-tagged image to Docker Hub and applies Kubernetes manifests.
3. **Docker** builds and stores NEXUS's image on the Jenkins host.
4. **Trivy** scans the built image. The configured severity is a deployment gate.
5. **Minikube/Kubernetes** runs NEXUS in namespace `nexus`. Jenkins waits for rollout and `/healthz` before marking deployment successful.
6. **Ansible** prepares the Ubuntu host (Docker, Java 21, Jenkins, Trivy, kubectl and Minikube).
7. **Terraform** provisions an optional new AWS EC2 host. It does not install software and it does not adopt an existing EC2 instance automatically.
8. **Prometheus/Grafana** are deployed separately into namespace `monitoring`.
9. **Argo CD** is an optional GitOps route for customer workloads; it is not the Jenkins self-deploy mechanism.

## Resource constraint

The intended EC2 shape is `m7i-flex.large` (2 vCPU, 8 GiB RAM), with exactly **25 GB encrypted gp3 root storage**. Minikube is configured for 2 CPUs, 3072 MiB and an 8 GiB node disk. Keep Jenkins builds and monitoring sequential; a 25 GB disk fills quickly with image layers and the vulnerability database. Do not increase the EBS volume automatically.

## 1. Prepare the Ubuntu host

### 1.1 Access the existing host

```bash
ssh -i /path/to/admin-key.pem ubuntu@YOUR_EC2_PUBLIC_IP
```

Check the requested machine resources before installing anything:

```bash
lscpu | grep '^CPU(s):'
free -h
df -h /
```

The playbook expects Ubuntu 24.04 and an administrator account named `ubuntu` by default. Do not run it against the wrong host.

### 1.2 Prepare Ansible on the administrator machine

Run Ansible from a trusted workstation/control machine, not from the target by default. Install Ansible and its collection:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip ansible
cd ansible
ansible-galaxy collection install -r requirements.yml
cp inventory/hosts.example.yml inventory/hosts.yml
```

Edit `inventory/hosts.yml` and set the actual EC2 public IP, SSH user and path to the **private key on your control machine**. Keep `hosts.yml` private; it is git-ignored. Confirm SSH access first.

Check syntax and proposed changes:

```bash
ansible-playbook -i inventory/hosts.yml site.yml --syntax-check
ansible-playbook -i inventory/hosts.yml site.yml --check --diff
```

Review the check output. Apply only when you intend to configure that host:

```bash
ansible-playbook -i inventory/hosts.yml site.yml --diff
```

The playbook installs Docker Engine, Java 21, Jenkins, Trivy, kubectl and Minikube. Jenkins is bound to `127.0.0.1`; use an SSH tunnel to open its UI. Minikube runs as the normal `ubuntu` user with Docker driver.

### 1.3 Docker access and mTLS for NEXUS running inside Minikube

A Kubernetes Pod cannot use the host's Unix Docker socket automatically. The local NEXUS manifest therefore uses an **optional remote Docker API protected by mutual TLS**. The Ansible Docker role generates a CA, server certificate and client certificate. It listens on TCP 2376 with TLS verification enabled and adds UFW allow rules only for the configured Minikube node (`192.168.49.0/24`) and Pod (`10.244.0.0/16`) CIDRs.

The Terraform security group does **not** open port 2376. Do not add a public or `0.0.0.0/0` rule for Docker API access. Possession of a Docker client certificate grants root-equivalent control of the host; upload it only to the trusted Jenkins instance and store it as a Jenkins Secret file credential.

After Ansible completes, on EC2:

```bash
sudo systemctl status docker --no-pager
sudo ss -lntp | grep 2376
sudo ls -l /home/ubuntu/nexus-docker-client-certs.tar.gz
```

The listener should be TLS-enabled. The client bundle is generated at `/home/ubuntu/nexus-docker-client-certs.tar.gz` with mode 0600. Copy it securely to the administrator workstation:

```bash
scp -i /path/to/admin-key.pem \
  ubuntu@YOUR_EC2_PUBLIC_IP:/home/ubuntu/nexus-docker-client-certs.tar.gz .
```

Do not commit or email this file. Upload it directly to Jenkins as credential ID `docker-client-certs` (Kind: Secret file). Remove the local copy after Jenkins has stored it securely. The Docker API port must remain absent from the AWS security group.

### 1.4 Check Minikube

On EC2, as `ubuntu`:

```bash
minikube status
kubectl config current-context
kubectl get nodes -o wide
kubectl get pods -A
```

Expected context is `minikube` and the node should show `Ready`. Check the host name mapping from a disposable Pod if Docker TLS cannot connect:

```bash
kubectl run host-check --rm -it --restart=Never --image=busybox:1.36 -- nslookup host.minikube.internal
kubectl run host-exporter-check --rm -it --restart=Never --image=busybox:1.36 -- sh -c 'wget -qO- http://host.minikube.internal:9100/metrics | grep -m1 node_cpu_seconds_total; wget -qO- http://host.minikube.internal:8081/metrics | grep -m1 container_cpu_usage_seconds_total'
```

Minikube documents `host.minikube.internal` for host access from Pods; the address varies by driver/cluster, so run this connectivity check before relying on the EC2 scrape targets: https://minikube.sigs.k8s.io/docs/handbook/host-access/ .

Do not disable TLS verification to work around certificate failures. Check the certificate SAN, secret mount and `DOCKER_CERT_PATH` instead.

## 2. Create the Docker Hub repository and token

1. Sign in to Docker Hub and create a repository named `nexus-platform` (or choose another repository name).
2. Create a Docker Hub personal access token with the read/write permissions required for that repository.
3. In Jenkins, add a global credential:
   - Kind: **Username with password**
   - Username: Docker Hub username
   - Password: access token (not the account password)
   - ID: `docker_creds`
4. Never place that token in a Jenkinsfile, `terraform.tfvars`, GitHub, or `.env.example`.

## 3. Create Kubernetes access and runtime credentials

On EC2 as the `ubuntu` user:

```bash
kubectl config view --raw --flatten --minify > /tmp/nexus-kubeconfig
chmod 600 /tmp/nexus-kubeconfig
kubectl --kubeconfig=/tmp/nexus-kubeconfig get nodes
```

Transfer the file securely to your workstation, then add it to Jenkins:

- Kind: **Secret file**
- File: flattened kubeconfig
- ID: `kubeconfig`

The Jenkins kubeconfig should point at the Minikube API server address reachable from the Jenkins agent. For a Jenkins agent on the same EC2 host, the `minikube` context normally points to a host-local Docker network address; verify it from the agent before running a build.

Generate an NEXUS API key on a trusted machine:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Add it in Jenkins:

- Kind: **Secret text**
- Secret: generated value
- ID: `nexus-api-key`

The root pipeline creates/updates a Kubernetes Secret named `nexus-runtime` from this value. Do not put the key in Git.

## 4. Configure Jenkins

Open Jenkins over an SSH tunnel from your Windows/macOS/Linux workstation:

```bash
ssh -i /path/to/admin-key.pem -L 8080:127.0.0.1:8080 ubuntu@YOUR_EC2_PUBLIC_IP
```

Then visit `http://127.0.0.1:8080` locally. Retrieve the first-run password on EC2 with:

```bash
sudo cat /var/lib/jenkins/secrets/initialAdminPassword
```

Install the Pipeline, Git and Credentials plugins (plus recommended dependencies). Create **these global credentials** with exact IDs:

| ID | Jenkins kind | Purpose |
|---|---|---|
| `docker_creds` | Username with password | Push image to Docker Hub and create image pull secret |
| `kubeconfig` | Secret file | Apply/verify Kubernetes resources |
| `nexus-api-key` | Secret text | Protect NEXUS API |
| `docker-client-certs` | Secret file | mTLS client bundle created by Ansible for in-Pod Docker API access |

The pipeline is pinned to the `linux-docker` label. For a single-machine academic lab, open **Manage Jenkins → Nodes → Built-In Node → Configure → Labels** and add `linux-docker` to the built-in node only if this Jenkins instance and repository are private and only trusted administrators can modify/run the job. A hardened installation should use a dedicated, administrator-managed Linux agent with Python 3, Git, Docker CLI/daemon, kubectl, Trivy and curl. Restrict who can configure jobs that use this label; Docker access is root-equivalent.

Create a new **Pipeline** job, for example `nexus-self-deploy`:

1. Definition: **Pipeline script from SCM**.
2. SCM: **Git**.
3. Repository URL: your NEXUS GitHub repository.
4. Branch: the branch that contains the final NEXUS project (for example `*/main`).
5. Script Path: `Jenkinsfile` (the root-level file, not `jenkins/Jenkinsfile`).
6. Save.

The root Jenkinsfile polls SCM every five minutes, so a public Jenkins webhook is not required. If you later add a webhook, expose it only through a secured reverse proxy and use the correct GitHub webhook verification. Keep Jenkins bound to localhost.

## 5. Run the NEXUS self-deployment pipeline

Click **Build with Parameters**. Set:

- `DOCKERHUB_REPO`: `YOUR_DOCKERHUB_USERNAME/nexus-platform` (namespace/repository only).
- `TRIVY_SEVERITY`: `CRITICAL` for the recommended initial security gate. `HIGH,CRITICAL` blocks both High and Critical findings. `NONE` is report-only and should be used only for controlled diagnostics.
- `K8S_MANIFEST`: `k8s/nexus-local.yaml` for Minikube. `k8s/nexus-eks.yaml` only if a real EKS cluster is already prepared and the kubeconfig has EKS access.

The root pipeline runs the following stages in order:

1. Checkout NEXUS from GitHub.
2. Verify Python, Git, Docker, kubectl and Trivy exist on the Jenkins agent.
3. Create a temporary Python venv; compile and run `backend/tests`.
4. Build the NEXUS image with a commit/build-specific tag and `latest`.
5. Scan the exact image with Trivy (vulnerability, misconfiguration and secret scanners); archive JSON report.
6. Push tags to Docker Hub using the `docker_creds` token.
7. Create/update namespace, RBAC, the Kubernetes image-pull Secret (`nexus-registry`), a separate publisher Secret (`nexus-registry-push` with `REGISTRY_URL`, `REGISTRY_USERNAME` and `REGISTRY_PASSWORD`), runtime API key and (for the local Minikube manifest) the mTLS Docker client Secret. The publisher Secret lets NEXUS push customer application images; it is not the same as the Kubernetes image-pull Secret. Use a dedicated Docker Hub access token with only the required repository permissions. The pipeline validates the certificate archive and extracts only `ca.pem`, `cert.pem` and `key.pem` before creating that Secret.
8. Apply the NEXUS manifest with the immutable build tag.
9. Wait for Kubernetes rollout and at least one ready replica. If a new rollout fails after a previous revision exists, request `kubectl rollout undo` and wait for that rollback; the pipeline still reports the failed new deployment.
10. Port-forward the Service from the Jenkins agent and verify `GET /healthz` returns HTTP 200.

Image tags are commit/build-specific. Do not rely on `latest` as the deployment identity. Review the archived `nexus-trivy.json` report in Jenkins after each run.

## 6. Access the running NEXUS dashboard privately

On the EC2 host, start a port-forward (keep this terminal/session running):

```bash
kubectl port-forward -n nexus service/nexus 8000:8000 --address 127.0.0.1
```

On your local machine, open a second terminal and create an SSH tunnel:

```bash
ssh -i /path/to/admin-key.pem -L 8000:127.0.0.1:8000 ubuntu@YOUR_EC2_PUBLIC_IP
```

Open `http://127.0.0.1:8000`. Do not open port 8000 to the world. If accessing through a separate private reverse proxy, configure `NEXUS_CORS_ORIGINS` and authentication before exposing it.

## 7. Install Prometheus and Grafana in Kubernetes

Deploy monitoring only after NEXUS is healthy. The Kubernetes monitoring stack uses namespace `monitoring`, a 1 GiB Prometheus PVC and a 1 GiB Grafana PVC. It deploys kube-state-metrics plus node-exporter/cAdvisor DaemonSets for Kubernetes node/container metrics. The Ansible monitoring role also starts `monitoring/host-exporters/docker-compose.yml` directly on EC2: node-exporter reports EC2 host CPU, memory and filesystem metrics, while cAdvisor reports Docker container metrics. In-cluster Prometheus scrapes these host exporters at `host.minikube.internal:9100` and `host.minikube.internal:8081`. Ansible restricts UFW access to the Minikube node and Pod CIDRs; neither exporter port is opened in the AWS security group. Verify DNS and connectivity from a Pod before expecting the targets to be UP. Set `host_exporters_deploy: false` to disable host-level collection. cAdvisor needs privileged host visibility, so only use it on this trusted single host. Monitoring consumes memory, CPU and disk; watch `free -h` and `df -h` on the small EC2 host.

Create a strong Grafana admin password without putting it in YAML or Git:

```bash
openssl rand -hex 24 > /tmp/nexus-grafana-password
chmod 600 /tmp/nexus-grafana-password
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic grafana-admin -n monitoring \
  --from-file=admin-password=/tmp/nexus-grafana-password \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create configmap nexus-grafana-dashboard -n monitoring \
  --from-file=nexus-overview.json=monitoring/grafana/dashboards/nexus-overview.json \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/nexus-grafana-password
```

Apply and wait:

```bash
kubectl apply -f monitoring/k8s/kube-state-metrics.yaml
kubectl apply -f monitoring/k8s/monitoring-stack.yaml
kubectl rollout status deployment/prometheus -n monitoring --timeout=180s
kubectl rollout status deployment/grafana -n monitoring --timeout=180s
kubectl rollout status deployment/kube-state-metrics -n kube-system --timeout=120s
```

Access Grafana/Prometheus only through SSH tunnels:

```bash
kubectl port-forward -n monitoring service/grafana 3000:3000 --address 127.0.0.1
kubectl port-forward -n monitoring service/prometheus 9090:9090 --address 127.0.0.1
```

Create separate SSH tunnels for 3000 and 9090. Grafana's Prometheus data source is provisioned to `http://prometheus.monitoring.svc.cluster.local:9090`; the NEXUS dashboard obtains integration status from its configured URLs. A panel with no data means the scrape target or required exporter is unavailable; it is not evidence of zero CPU/memory.

## 8. Terraform: provision a *new* EC2 host only when intended

The Terraform module creates a VPC, public subnet, security group, SSH key pair, optional ECR-only EC2 role and one EC2 instance. Defaults preserve `m7i-flex.large` and 25 GB encrypted gp3 storage. It intentionally does not open Jenkins, Grafana, Prometheus, Kubernetes API or Docker TCP 2376 in the AWS security group.

If you already have an EC2 host, **do not run `terraform apply` against this configuration without an import/adoption plan**: the default behaviour is to create a new host, not modify the existing one.

On a trusted machine with AWS CLI credentials:

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit aws_region, admin_cidr and ssh_public_key. Never put a private key here.
terraform fmt -check -recursive
terraform init
terraform validate
terraform plan -out nexus.plan
terraform show nexus.plan
```

`terraform apply nexus.plan` creates billable AWS resources. Only run after reviewing the plan and obtaining explicit approval. `terraform destroy` deletes infrastructure; do not run it casually. Terraform state can contain sensitive data and must remain private.

## 9. Validate Ansible without modifying a host

From the Ansible directory on the control machine:

```bash
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i inventory/hosts.yml site.yml --syntax-check
ansible-playbook -i inventory/hosts.yml site.yml --check --diff
```

`--check` is a preview, not proof the services will be installed successfully. A real run requires SSH access, package repository access and explicit authorization to configure the EC2 host.

## 10. Troubleshooting

### Jenkins says Docker permission denied

Check `id jenkins` and `getent group docker`. The Ansible Docker role adds Jenkins to the Docker group. Restart Jenkins after group membership changes:

```bash
sudo systemctl restart jenkins
sudo -u jenkins docker version
```

Docker group membership is root-equivalent. Keep Jenkins private and do not allow untrusted pull requests to execute arbitrary pipeline code on this agent.

### Kubernetes image pull fails

Check that both `nexus-registry` (image pull) and `nexus-registry-push` (NEXUS publisher) exist in namespace `nexus`, and that the Jenkins credential is a valid read/write token:

```bash
kubectl get secret nexus-registry -n nexus
kubectl get secret nexus-registry-push -n nexus
kubectl describe pod -n nexus -l app=nexus
```

Do not decode or paste `.dockerconfigjson` into chat/logs.

### NEXUS Docker diagnostics report a TLS/connection error

Check the Docker service and listener on EC2:

```bash
sudo systemctl status docker --no-pager
sudo ss -lntp | grep 2376
sudo ufw status numbered
```

Check the secret exists and is mounted:

```bash
kubectl get secret nexus-docker-client -n nexus
kubectl describe pod -n nexus -l app=nexus
```

Inside the NEXUS Pod, verify `DOCKER_HOST=tcp://host.minikube.internal:2376`, `DOCKER_TLS_VERIFY=1`, and `DOCKER_CERT_PATH=/var/run/docker-certs`. Do not turn off certificate verification. To rotate the CA/server/client certificates, explicitly run the Docker role with `-e docker_tls_force_rotate=true`, then securely retrieve the regenerated client bundle, replace the Jenkins Secret-file credential and run the pipeline again. The role does not rotate keys on an ordinary idempotent run.

### NEXUS Pod is Pending or OOMKilled

```bash
kubectl describe pod -n nexus -l app=nexus
kubectl get events -n nexus --sort-by=.lastTimestamp
free -h
df -h /
```

Stop optional monitoring before adding more workloads if the 8 GiB host is under pressure. Do not expand the 25 GB root volume automatically.

### Roll back

```bash
kubectl rollout history deployment/nexus -n nexus
kubectl rollout undo deployment/nexus -n nexus --to-revision=REVISION
kubectl rollout status deployment/nexus -n nexus --timeout=300s
kubectl get pods -n nexus
```

A rollback is successful only when Kubernetes reports the rollout complete and a ready pod is running. Verify `/healthz` again through the port-forward.

## Security rules

- Do not commit `.env`, `terraform.tfvars`, Terraform state, SSH private keys, kubeconfig or Docker client certificates.
- Jenkins, Grafana, Prometheus and NEXUS are accessed over SSH tunnels by default.
- Do not expose TCP 2376 or the Docker socket to the public internet.
- Docker socket/API access grants root-equivalent privileges, even when protected by mTLS.
- Use a dedicated, trusted Jenkins agent; do not run untrusted pipeline code with Docker privileges.
- Terraform creates billable infrastructure; review plans before applying.
- Terraform provisions the host; Ansible configures it; Jenkins builds and deploys NEXUS; Prometheus/Grafana observe the runtime. These responsibilities are deliberately separate.
