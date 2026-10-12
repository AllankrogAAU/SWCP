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

The script installs metrics-server and ingress-nginx, builds all images locally, creates a `standard` storage class if the cluster lacks one, applies every manifest in `k8s/`, waits for c-sandbox, and loads the sandbox runtime image into each c-sandbox pod. The first run takes several minutes because of the image builds.

When it finishes, open http://swcp.local.

Run the script again after code changes to rebuild the images. Then restart the affected deployment so pods pick up the new image:

```bash
kubectl rollout restart deployment/core-api -n swcp
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
kubectl logs deployment/c-sandbox -n swcp -c dind
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