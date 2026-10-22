# Running SWCP on Linux (Ubuntu / Debian)

This guide explains how to set up, deploy, and iterate on the SWCP application natively on a Linux host (Ubuntu 22.04+, 24.04+, 26.04+ or Debian 12+) using **k3s**, **Docker**, **gVisor (`runsc`)**, and **Tailscale**.

---

## 1. Initial Host Setup (Run Once)

Before deploying for the first time, install the required host packages (Docker, k3s with Traefik disabled, Google gVisor) and configure user permissions:

```bash
sudo bash scripts/setup-host-prereqs.sh
```

### What this script does:
- Installs `docker.io`, `bzip2`, `tar`, and `curl`.
- Enables the Docker daemon and adds your current user to the `docker` group.
- Installs `k3s` (lightweight Kubernetes) with Traefik disabled and kubeconfig mode 644.
- Configures `~/.kube/config` and `/etc/sudoers.d/$USER` for passwordless sudo (needed for k3s containerd image imports).
- Configures UFW firewall rules to allow ports `80`, `443`, and `6443`.
- Installs `runsc` (Google gVisor), configures k3s containerd runtime templates, labels the node `swcp.io/gvisor=true`, and executes a verification probe pod.

---

## 2. Deploy the Full Application

To build all 7 service container images, import them into k3s's containerd, and roll out the entire application:

```bash
bash scripts/deploy-linux-k8s.sh
```

Once the rollout completes, the frontend and backend are immediately accessible on host port 80:
- **Local Browser**: `http://localhost/`
- **Tailscale Network**: `http://<tailscale-ip>/` or `http://<hostname>/`

Default seeded teacher account:
- **Username**: `teacher@swcp.local` (or value of `DEV_TEACHER_EMAIL` in `.env`)
- **Password**: `replace-with-a-strong-local-password` (or value of `DEV_TEACHER_PASSWORD` in `.env`)

---

## 3. Fast Inner-Loop Development (Targeted Rebuilds)

During development, you don't need to rebuild all 7 services. You can rebuild and redeploy only the specific service you are working on in ~5–10 seconds.

```bash
./scripts/deploy-linux-k8s.sh <service-name>
```

### Available Service Targets

| Command | What It Rebuilds | What It Restarts | Typical Duration |
| :--- | :--- | :--- | :--- |
| `./scripts/deploy-linux-k8s.sh frontend` | Vue 3 SPA (`apps/frontend`) | `deployment/frontend` | ~5 seconds |
| `./scripts/deploy-linux-k8s.sh core-api` | .NET Core Web API (`apps/coreApi`) | `deployment/core-api` | ~8 seconds |
| `./scripts/deploy-linux-k8s.sh api-gateway` | ASP.NET YARP Gateway (`apps/apiGateway`) | `deployment/api-gateway` | ~7 seconds |
| `./scripts/deploy-linux-k8s.sh llm-worker` | .NET LLM Worker (`apps/llmWorker`) | `deployment/llm-worker` | ~7 seconds |
| `./scripts/deploy-linux-k8s.sh c-sandbox-worker` | Python NATS Consumer (`apps/cSandbox/Dockerfile.worker`) | `deployment/c-sandbox` (drains and respawns warm pool) | ~5 seconds |
| `./scripts/deploy-linux-k8s.sh c-sandbox-runtime` | C Runner Sandbox image (`apps/cSandbox/Dockerfile`) | Cleans existing warm pods in `swcp-sandbox` & restarts `deployment/c-sandbox` | ~6 seconds |
| `./scripts/deploy-linux-k8s.sh c-sandbox` | **Both** worker and sandbox runtime images | Cleans sandbox pods & restarts `deployment/c-sandbox` | ~10 seconds |
| `./scripts/deploy-linux-k8s.sh postgres` | Custom Postgres container (`apps/postgres`) | Re-imports Postgres image | ~5 seconds |
| `./scripts/deploy-linux-k8s.sh all` | Rebuilds and imports **all 7 images** | Restarts all deployments | ~1–2 minutes |

---

## 4. External & Tailscale Access

Because the NGINX Ingress controller runs on `hostNetwork`, host ports `80` and `443` are open on all network interfaces, including `tailscale0`.

### Direct Tailnet Access (HTTP)
Any machine connected to your Tailscale network can reach SWCP without extra configuration:
```
http://<your-tailscale-ip>/
http://<your-hostname>/
```

### Secure HTTPS Access (Tailscale Serve)
To expose SWCP over HTTPS with Tailscale-managed TLS certificates on `https://<hostname>.<tailnet>.ts.net/`:

```bash
bash scripts/expose-frontend.sh
```

*Note: If prompted, authorize the node once via the URL printed in the terminal.*

To turn off Tailscale Serve:
```bash
tailscale serve reset
```

---

## 5. Environment Configuration (`.env`)

The deployment script dynamically reads `.env` on every run and synchronizes Kubernetes secrets and configmaps, remember not to push it to github.

## 6. Useful Inspection & Debugging Commands

```bash
# View all pods and status in swcp namespace
kubectl get pods -n swcp -o wide

# View warm sandbox pods running under gVisor
kubectl get pods -n swcp-sandbox
kubectl get pods -n swcp-sandbox -o jsonpath='{.items[*].spec.runtimeClassName}'

# Follow logs of a service
kubectl logs -f deployment/core-api -n swcp
kubectl logs -f deployment/api-gateway -n swcp
kubectl logs -f deployment/c-sandbox -n swcp -c c-sandbox-worker
kubectl logs -f deployment/llm-worker -n swcp

# Inspect a failed or restarting pod
kubectl describe pod <pod-name> -n swcp

# Connect directly to Postgres database
kubectl port-forward svc/postgres -n swcp 5432:5432
psql -h localhost -U swcp_user -d swcp_db

# Reset / teardown the application (keeps k3s and system controllers)
kubectl delete namespace swcp
```

