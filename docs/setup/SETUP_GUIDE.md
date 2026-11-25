# NEXUS setup guide (beginner-friendly)

Follow the parts in order. Each part ends with **how to check it worked**.
Steps marked **(untested)** were written from the tools' official documentation but could not be run in the
development sandbox — expect to adjust small details and note what you changed for your viva.

---
## Part 1 — Windows: run NEXUS locally

### 1.1 Install the tools
| Tool | How | Check |
|---|---|---|
| Python 3.10+ | https://www.python.org/downloads/ — tick **Add python.exe to PATH** | `python --version` |
| Docker Desktop | https://www.docker.com/products/docker-desktop/ — keep **Use WSL 2** ticked, restart when asked | `docker version` prints **Client** and **Server** |
| Git (optional) | https://git-scm.com | `git --version` |

### 1.2 Start Docker Desktop (the fix for “Couldn't reach the Docker daemon … CreateFile”)
That error means Windows has no Docker *named pipe*, i.e. **the Docker engine is not running**.
1. Start **Docker Desktop** from the Start menu.
2. Wait until the bottom-left says **Engine running** (green).
3. In PowerShell run `docker version`. If you see a *Server:* section, Docker is ready.
4. Open NEXUS → **Settings → Docker Engine → Check again**. NEXUS tells you which of these it found:
   *not installed*, *Docker Desktop not running*, *engine unavailable*, *bad DOCKER_HOST*, *permission problem*.

If `docker version` works but NEXUS says it cannot connect, set the endpoint explicitly in `.env`:
`DOCKER_HOST=npipe:////./pipe/dockerDesktopLinuxEngine` (run `docker context ls` to see the active endpoint).

### 1.3 Run the NEXUS backend
```powershell
cd path\to\NEXUS
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000 . **Check:** the top-right pill shows “N of M services connected”.

### 1.4 First release (ZIP)
```powershell
python tests\fixtures\build_fixtures.py      # creates tests\fixtures\_generated\*.zip
```
NEXUS → **ZIP Upload** → pick `static-site.zip` → **Start release**.
**Check:** all stages turn green (Scan shows *Skipped* until Trivy is installed) and an *Open preview* link appears.
Try `corrupt.zip`, `path-traversal.zip`, `symlink.zip`: NEXUS must refuse them with a clear message.

### 1.5 Trivy (security scanning)
`winget install AquaSecurity.Trivy` (or Scoop/Chocolatey), open a **new** terminal, `trivy --version`, restart NEXUS.
The first scan downloads a vulnerability database (needs internet). Choose the gate in `.env`:
`TRIVY_FAIL_ON_SEVERITY=CRITICAL` (or HIGH / MEDIUM / LOW / NONE = report only).
**Check:** Security page shows counts and a JSON report you can download.

### 1.6 Docker Compose alternative
`docker compose up -d --build` (set `NEXUS_API_KEY` first). Inside a container NEXUS reaches Docker through the mounted socket.

---
## Part 2 — AWS EC2 (Terraform + Ansible)

### 2.1 Prepare
* AWS account, an IAM user for yourself with EC2/VPC/IAM permissions, **AWS CLI** + `aws configure` (keys stay in `~/.aws`, never in files here).
* Install Terraform and Ansible (Ansible runs best in **WSL** on Windows).
* Make an SSH key: `ssh-keygen -t ed25519` (the `.pub` file is public; keep the other file private).
* Find your public IP: https://checkip.amazonaws.com

### 2.2 Terraform — create the server
```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars    # set admin_cidr = "<your-ip>/32" and ssh_public_key
terraform init
terraform validate
terraform plan -out nexus.plan      # READ it. It creates billable resources.
terraform apply nexus.plan          # only when you are sure
```
Result: `m7i-flex.large` (2 vCPU / 8 GB), **25 GB** disk, SSH only from your IP. Note the `public_ip` output.
**Cleanup when done:** `terraform destroy`.

### 2.3 Ansible — install the software
```bash
cd ../ansible
cp inventory/hosts.example.yml inventory/hosts.yml     # put the public IP and your private key path
ansible-galaxy collection install community.general
ansible all -m ping
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --check --diff               # dry run
ansible-playbook site.yml                              # real run
```
Installs Java 21, Docker (log rotation + weekly prune), Trivy, Jenkins (**bound to localhost**), kubectl, Minikube binary.
**Check:** `ssh ubuntu@IP "docker --version && trivy --version && kubectl version --client && systemctl is-active jenkins"`.

### 2.4 Put NEXUS on the server
```bash
ssh -i ~/.ssh/<key> ubuntu@<IP>
git clone <your repo> NEXUS && cd NEXUS            # or scp the project folder
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env && nano .env                  # set NEXUS_API_KEY, K8S_IMAGE_SOURCE=minikube ...
sudo cp deploy/nexus.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now nexus
```
NEXUS listens on `127.0.0.1:8000` only. From your laptop open a tunnel and browse http://localhost:8000 :
```bash
ssh -i ~/.ssh/<key> -L 8000:localhost:8000 -L 8080:localhost:8080 -L 3000:localhost:3000 ubuntu@<IP>
```
> **Why a tunnel?** The security group opens only SSH. NEXUS controls Docker (root-equivalent) and Jenkins can run code,
> so neither should face the internet.

### 2.5 Disk hygiene on 25 GB
Docker images and build cache fill the disk. Check **Infrastructure → Disk**. When above ~80%:
`docker system prune -af --filter until=72h`. Ansible installs a weekly prune and log rotation.

---
## Part 3 — Minikube (Kubernetes)

On the server **as the `ubuntu` user (not root)**:
```bash
minikube start --driver=docker --cpus=2 --memory=4096     # leave RAM for NEXUS/Jenkins
kubectl get nodes                                          # Ready
```
In NEXUS `.env`: `K8S_IMAGE_SOURCE=minikube` (NEXUS runs `minikube image load`, no registry needed), then
`sudo systemctl restart nexus`. **Check:** Kubernetes page shows the cluster; after a release it shows the deployment.

Reach a deployed app: `kubectl port-forward svc/<app> 8081:80` then http://localhost:8081 (tunnel that port too).
Using ECR instead: set `ECR_REPOSITORY_PREFIX`, `AWS_REGION`, `K8S_IMAGE_SOURCE=registry` (the Terraform instance role grants ECR access).
**(untested end-to-end)**

### Rollback
Release the same app twice (change something), then **Kubernetes → Revisions and rollback → Roll back** on the older revision.
NEXUS replaces the pod template, waits for the rollout, then re-reads the pods. It shows success **only** if the pods are Ready
on the old image. Result is in **Deployment History**.

---
## Part 4 — Jenkins + GitHub (administrator, one time) **(untested)**

1. Browse http://localhost:8080 (via tunnel). Unlock with `sudo cat /var/lib/jenkins/secrets/initialAdminPassword`; install suggested plugins; create an admin user.
2. **Manage Jenkins → Credentials → Global → Add**
   * *Secret text*, ID `nexus-api-key` = your `NEXUS_API_KEY`.
   * *Username with password*, ID `github-repo-creds` = GitHub username + **personal access token** (only for private repos; read-only “Contents”).
3. **Manage Jenkins → System → Global properties → Environment variables:** `NEXUS_URL = http://localhost:8000`.
4. **New Item → Pipeline** named `nexus-github-deploy` → *This project is parameterized* is automatic from the Jenkinsfile → *Pipeline script from SCM* or paste `jenkins/Jenkinsfile`.
5. Jenkins user → **Configure → API Token → Add new token**. In NEXUS `.env`:
   ```
   JENKINS_URL=http://localhost:8080
   JENKINS_USER=<your jenkins user>
   JENKINS_API_TOKEN=<token>
   GITHUB_REPO_URL=https://github.com/<you>/<repo>
   GITHUB_BRANCH=main
   GITHUB_WEBHOOK_SECRET=<random string>
   ```
   Restart NEXUS. **Check:** NEXUS → GitHub Deployment shows Jenkins *Connected*. Click **Deploy now**: Jenkins runs, uploads to NEXUS, the release appears with source `github`.
6. **Webhook (automatic deploys):** GitHub repo → Settings → Webhooks → Add: payload URL `https://<public-host>/api/webhooks/github`, content type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`, event **push**.
   GitHub must be able to reach that URL. With the SSH-only security group it cannot. Options: set Terraform `webhook_ingress_cidrs` to GitHub’s published `hooks` ranges and put HTTPS (a reverse proxy) in front of port 8000; or use the manual **Deploy now** button for the demo. NEXUS verifies the HMAC signature and ignores other branches/repositories.

---
## Part 5 — Monitoring
```bash
cd monitoring
cp .env.example .env            # set GRAFANA_ADMIN_PASSWORD
docker compose up -d
```
Prometheus http://localhost:9090 · Grafana http://localhost:3000 (user `admin`). In NEXUS `.env`:
`PROMETHEUS_URL=http://localhost:9090`, `GRAFANA_URL=http://localhost:3000`; restart NEXUS.
**Check:** Prometheus → Status → Targets: `nexus`, `node`, `cadvisor` are UP. Grafana → NEXUS → *NEXUS Overview* has data. NEXUS → Monitoring shows the same numbers.
Kubernetes pod panels need kube-state-metrics: `kubectl apply -f monitoring/k8s/kube-state-metrics.yaml`, then put `minikube ip` into `prometheus.yml`.

## Part 6 — Argo CD (optional)
See `argocd/README.md`. Treat it as optional until you see an app **Synced / Healthy**.

---
## Troubleshooting
| Symptom | Likely cause | Fix |
|---|---|---|
| “Docker Desktop is not running” | Engine stopped/starting | Start Docker Desktop, wait for *Engine running*, **Check again** |
| `docker` package error / `sdk_missing` | venv not active | Activate `.venv`, `pip install -r requirements.txt` |
| `permission_denied` (Linux) | user not in `docker` group | `sudo usermod -aG docker $USER`, log out/in |
| 401 on every call | `NEXUS_API_KEY` set | Click **Access key** and enter it |
| Upload rejected “path traversal / policy” | Unsafe or secret file in the ZIP | Remove `.env`, keys, symlinks, nested zips |
| Build fails “no build script” | `package.json` lacks `build` | Add `"build": "vite build"` |
| Scan shows *Skipped* | Trivy not installed | Install Trivy, restart NEXUS |
| Scan *Error* first time | DB download failed (no internet) | Allow internet once, or set `TRIVY_FAIL_ON_SEVERITY=NONE` |
| Kubernetes “Not connected” | No kubeconfig / Minikube stopped | `minikube start`, run NEXUS as the same user |
| Rollback “unverified” | New pods not Ready | See pod status on Kubernetes page; fix image, roll back again |
| No disk space | Docker images | `docker system prune -af` |
| Mobile page cut off | — | Hard-refresh (Ctrl+F5) |
