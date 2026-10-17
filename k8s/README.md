# Running the project locally

1. Enable Kubernetes: Docker Desktop > Settings > Kubernetes > Enable Kubernetes > Apply & restart. Wait until the Kubernetes indicator is green.
2. Add the hostname `swcp.local` to your hosts file (see below).

`kubectl` is bundled with Docker Desktop. Check with `kubectl get nodes`; it should show one `Ready` node. If the context is wrong, run `kubectl config use-context docker-desktop`.

## Hosts file entry (You only need to do this once)

Windows (PowerShell as Administrator):

```powershell
Add-Content -Path C:\Windows\System32\drivers\etc\hosts -Value "127.0.0.1 swcp.local"
```

macOS:

```bash
echo "127.0.0.1 swcp.local" | sudo tee -a /etc/hosts
```

## Deploy

Run one command from anywhere in the repo.

Windows:

```powershell
.\scripts\deploy-local-k8s.ps1
```

If PowerShell complains: `powershell -ExecutionPolicy Bypass -File .\scripts\deploy-local-k8s.ps1`

macOS:

```bash
bash scripts/deploy-local-k8s.sh
```

The script installs metrics-server and ingress-nginx, builds all images locally, creates a `standard` storage class if the cluster lacks one, applies every manifest in `k8s/`, and waits for c-sandbox. The first run takes several minutes because of the image builds.

When it finishes, open http://swcp.local.

Run the script again after code changes to rebuild the images. Then restart the affected deployment so pods pick up the new image:

```bash
kubectl rollout restart deployment/core-api -n swcp
```

Note: Docker Desktop's own Kubernetes runs inside a sealed, Docker-managed VM, so the sandbox pods above run **without gVisor** (the script warns about this). Use the WSL2 path below if you need real gVisor isolation locally.

## Local Kubernetes with gVisor (Windows, via WSL2 + k3s)

Docker Desktop Kubernetes can't host gVisor because its control plane runs inside a VM you don't control. To actually test the sandbox with gVisor enabled, run a small [k3s](https://k3s.io/) cluster inside a WSL2 distro instead (completely separate from Docker Desktop's own Kubernetes — you don't need "Enable Kubernetes" turned on for this path).

Prerequisites:

1. WSL2 installed, with a Linux distro (e.g. `Debian` or `Ubuntu`).
2. Docker Desktop > Settings > Resources > WSL Integration > enable integration for that distro. You do **not** need to enable Docker Desktop's Kubernetes.
3. The hosts file entry from above (`127.0.0.1 swcp.local`).

Then run, from the repo root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\deploy-local-k8s-wsl.ps1
```

Pass `-Distro <name>` if your distro isn't named `Debian`.

This single command, run inside the WSL distro as root, will:

- install k3s (a lightweight Kubernetes distribution with its own bundled containerd), if not already installed;
- install and verify `runsc` (gVisor) and register it as a containerd/Kubernetes runtime, then label the node `swcp.io/gvisor=true`;
- build all 7 service images with `docker build` and import them straight into k3s's containerd (no registry needed);
- install metrics-server and ingress-nginx, and apply every manifest in `k8s/`;
- patch the LLM worker's secret from your local `.env` and wait for the c-sandbox rollout to finish.

It's safe and idempotent to re-run after code changes — it rebuilds and re-imports the images and restarts the affected deployments. The first run takes several minutes (k3s + gVisor install plus image builds); later runs are much faster.

When it finishes, open http://swcp.local — this time the c-sandbox pods really run under gVisor (`runtimeClassName: gvisor`), the same as on a production node.

Any teammate with Docker Desktop and WSL Integration enabled for their distro can run the exact same command; they don't need to install anything manually first (no "Enable Kubernetes" checkbox, no separate gVisor install) — the script does it all.

To tear the WSL-based cluster down completely:

```bash
wsl -d Debian -u root -- /usr/local/bin/k3s-uninstall.sh
```

## Some useful commands

```bash
kubectl get pods -n swcp
kubectl get all -n swcp
kubectl get pvc -n swcp
kubectl get ingress -n swcp
kubectl get hpa -n swcp
kubectl top pods -n swcp
```

Logs:

```bash
kubectl logs deployment/core-api -n swcp
kubectl logs deployment/core-api -n swcp -f
kubectl logs deployment/c-sandbox -n swcp -c c-sandbox-worker
kubectl get pods -n swcp-sandbox
kubectl logs <pod-name> -n swcp --previous
```

Inspect a problem pod:

```bash
kubectl describe pod <pod-name> -n swcp
```

Restart a service:

```bash
kubectl rollout restart deployment/<name> -n swcp
kubectl rollout status deployment/<name> -n swcp
```

Scale manually (the HPA may change it back):

```bash
kubectl scale deployment/<name> -n swcp --replicas=3
```

Open a shell in a container:

```bash
kubectl exec -it deployment/core-api -n swcp -- sh
```

Reach a service directly without the ingress:

```bash
kubectl port-forward svc/frontend -n swcp 8081:80
```

Connect to Postgres from your machine:

```bash
kubectl port-forward svc/postgres -n swcp 5432:5432
```

## Stop and clean up

Remove the app but keep the data volumes:

```bash
kubectl delete -f k8s/ --ignore-not-found
```

Remove everything including the database and NATS data:

```bash
kubectl delete namespace swcp
```

Removing the namespace also deletes the persistent volumes, so the database starts empty next time.

## Sandbox (gVisor)

Each submission runs in its own short-lived pod in the `swcp-sandbox` namespace (`k8s/06-sandbox-runtime.yaml`). The c-sandbox worker creates the pod through the Kubernetes API (no privileged Docker-in-Docker anymore), keeps a few warm pods ready, runs compile, lint and execute in one `exec`, and always deletes the pod afterwards.

Pods use `runtimeClassName: gvisor` (runsc), restricted Pod Security, a read-only root filesystem, no service account token and a default-deny NetworkPolicy.

Linux node/cluster setup:

```bash
sudo bash scripts/install-gvisor-node.sh   # installs runsc and labels the node swcp.io/gvisor=true
```

Local Docker Desktop has no runsc, so `deploy-local-k8s` runs the sandbox pods without gVisor (and warns). Set `SWCP_GVISOR=1` to keep gVisor on a node that has it. To actually exercise gVisor locally on Windows, use `deploy-local-k8s-wsl.ps1` instead (see "Local Kubernetes with real gVisor" above), which runs `install-gvisor-node.sh` for you inside a real WSL2/k3s cluster. Tune with `SANDBOX_POOL_SIZE` and `SANDBOX_MAX_CONCURRENT` in `c-sandbox-config`.