# Argo CD (GitOps) - OPTIONAL

**Status in this project: optional / unverified.** The Argo CD code path in NEXUS (commit manifests, read
application sync/health, show it in the dashboard) is implemented, but it could not be exercised
end-to-end in the development sandbox (no cluster, no network). Treat it as *configured but not
demonstrated* until you run the steps below and see an application turn **Synced / Healthy**.

## Install (Minikube or any cluster)

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.12.3/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server
# first admin password:
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo
# open the UI on your laptop:
kubectl -n argocd port-forward svc/argocd-server 8443:443        # https://localhost:8443
```

## Connect it to Git and NEXUS

1. Create an (empty) **GitOps repository** on GitHub, e.g. `nexus-gitops`, with a `main` branch.
2. Edit `argocd/application.yaml` (`repoURL`) and `kubectl apply -n argocd -f argocd/application.yaml`.
   (Private repo: add it in Argo CD UI -> Settings -> Repositories with a token.)
3. In the Argo CD UI create an API token (Settings -> Accounts -> admin -> *Generate New* token; enable
   `apiKey` capability for the account first), then set in `.env`:
   ```
   ARGOCD_URL=https://localhost:8443        # reachable from NEXUS
   ARGOCD_TOKEN=<token>
   ARGOCD_VERIFY_TLS=false                  # only for the local self-signed certificate
   GIT_REPO_URL=https://github.com/<you>/nexus-gitops.git
   GIT_TOKEN=<token with repo write access>
   ```
4. Run a release in NEXUS. It pushes `apps/<name>/*.yaml`; Argo CD creates the Application and syncs it.
   NEXUS **Verify** waits for `Synced` + `Healthy`.

If you do not set `ARGOCD_URL`, NEXUS simply deploys directly (or to a local preview). Nothing else breaks.
