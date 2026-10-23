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

---

## 7. Monitoring, Distributed Tracing & Observability (Grafana Stack)

SWCP includes a fully integrated, production-grade telemetry and observability stack deployed in the `monitoring` namespace:
- **Grafana**: Unified Web UI for dashboards, log exploration, and distributed tracing.
- **Loki**: Centralized log aggregation capturing structured JSON logs from all pods.
- **Tempo**: High-throughput distributed tracing store tracking end-to-end W3C request traces.
- **Prometheus**: Real-time cluster metrics, pod CPU/RAM usage, and NATS queue depths.
- **Alloy**: DaemonSet collecting pod stdout/stderr logs and forwarding them into Loki.

### Deploying or Redeploying the Monitoring Stack
```bash
bash scripts/deploy-monitoring.sh
```

### Accessing the Grafana Web UI
Grafana is exposed directly on host port `3000`:
- **Local Access**: `http://localhost:3000/`
- **Tailscale Network**: `http://<tailscale-ip>:3000/` (e.g. `http://100.111.90.116:3000/`)

*Anonymous Admin mode is enabled for development convenience — no password or login prompt required.*

### Pre-Configured Dashboard
Open **Dashboards** → **SWCP Pipeline Stress Test & Timings**:
- **Total Pipeline Duration**: Real-time p50, p95, and p99 submission completion latency.
- **NATS Queue Delay vs Execution Time**: Compares queue wait times against actual worker execution duration to detect worker starvation under stress.
- **Sandbox Evaluation Latency**: Granular GCC compile duration vs binary execution duration.
- **Recent Pipeline Submissions**: Real-time structured log stream with clickable **TraceID** badges that jump straight into Tempo's waterfall view.

---

## 8. CLI Stress Testing & Pipeline Timings (`show-pipeline-timings.sh`)

To quickly inspect pipeline performance and pinpoint bottlenecks without opening a web browser:

```bash
# View timings and trace waterfall for the most recent completed submission
./scripts/show-pipeline-timings.sh

# View timings for a specific submission ID
./scripts/show-pipeline-timings.sh <submission-uuid>
```

### Example Output
```text
================================================================================
 SUBMISSION TIMINGS & WATERFALL: a49b177c-d060-46b8-b56c-1066c995511a
 Trace ID:     f61ed1c57329c061740ca2ca28fcbf67
 Grafana URL:  http://100.111.90.116:3000/d/swcp-pipeline-timings/swcp-pipeline-stress-test-and-timings
================================================================================

[Pipeline Stages (from Loki Logs)]
STAGE                      DURATION         QUEUE WAIT     STATUS         OUTCOME
---------------------------------------------------------------------------------
sandbox (queue wait)       -                45 ms          SANDBOX_PROCESSING 
submission_enqueue         163 ms           -              queued         
TOTAL PIPELINE             491 ms           -              completed      sandbox_run_completed
sandbox                    264 ms           -              completed      success;compiler-flags-not-yet-appl

[Deep Waterfall & Sub-Span Breakdown (from Tempo)]
OFFSET     SERVICE            SPAN NAME                  DURATION     DETAILS
-----------------------------------------------------------------------------
+0.0ms     core-api           POST /api/submissions/     174.4 ms     http: 202
+156.1ms   core-api           publish sandbox.tasks      13.5 ms      
+165.7ms   c-sandbox-worker   sandbox.evaluate           264.3 ms     outcome: success;compiler-flags-not-yet
+166.0ms   c-sandbox-worker   sandbox.execute_code       263.2 ms     
+429.1ms   c-sandbox-worker   sandbox.compile            0.0 ms       gcc compile: 89ms
+429.2ms   c-sandbox-worker   sandbox.run_binary         0.0 ms       binary run: 11ms (exit: 0)
+449.8ms   core-api           process sandbox.results    62.6 ms      
================================================================================
```


