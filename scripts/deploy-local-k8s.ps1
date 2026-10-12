$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Native {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

Invoke-Native kubectl @('apply', '-f', 'https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml')

$existingArgs = kubectl get deployment metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}'
if ($existingArgs -notmatch '--kubelet-insecure-tls') {
    $patch = '[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--kubelet-insecure-tls"}]'
    Invoke-Native kubectl @('patch', 'deployment', 'metrics-server', '-n', 'kube-system', '--type=json', "-p=$patch")
}

Invoke-Native kubectl @('apply', '-f', 'https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/cloud/deploy.yaml')
Invoke-Native kubectl @('rollout', 'status', 'deployment/ingress-nginx-controller', '-n', 'ingress-nginx', '--timeout=180s')

$images = @(
    @('ghcr.io/allankrogaau/core-api:latest', 'apps/coreApi/Dockerfile', '.'),
    @('ghcr.io/allankrogaau/api-gateway:latest', 'apps/apiGateway/Dockerfile', '.'),
    @('ghcr.io/allankrogaau/c-sandbox-worker:latest', 'apps/cSandbox/Dockerfile.worker', 'apps/cSandbox'),
    @('ghcr.io/allankrogaau/c-sandbox-runtime:latest', 'apps/cSandbox/Dockerfile', 'apps/cSandbox'),
    @('ghcr.io/allankrogaau/llm-worker:latest', 'apps/llmWorker/Dockerfile', '.'),
    @('ghcr.io/allankrogaau/frontend:latest', 'apps/frontend/Dockerfile', 'apps/frontend'),
    @('ghcr.io/allankrogaau/postgres:latest', 'apps/postgres/Dockerfile', 'apps/postgres')
)
foreach ($image in $images) {
    Invoke-Native docker @('build', '-t', $image[0], '-f', $image[1], $image[2])
}

$manifests = @(
    'k8s/00-namespace.yaml',
    'k8s/01-configmaps-secrets.yaml'
)
foreach ($manifest in $manifests) {
    Invoke-Native kubectl @('apply', '-f', $manifest)
}

$ErrorActionPreference = 'Continue'
& kubectl get storageclass standard 2>$null | Out-Null
$storageClassMissing = $LASTEXITCODE -ne 0
$ErrorActionPreference = 'Stop'
if ($storageClassMissing) {
    $provisioner = kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class==\"true\")].provisioner}'
    if ([string]::IsNullOrWhiteSpace($provisioner)) {
        throw "No 'standard' storage class and no default storage class to copy a provisioner from"
    }
    $storageClass = @"
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: $provisioner
reclaimPolicy: Delete
volumeBindingMode: Immediate
"@
    $storageClass | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the standard storage class' }
}

$manifests = @(
    'k8s/02-postgres.yaml',
    'k8s/03-nats.yaml',
    'k8s/04-core-api.yaml',
    'k8s/'
)
foreach ($manifest in $manifests) {
    Invoke-Native kubectl @('apply', '-f', $manifest)
}

$ErrorActionPreference = 'Continue'
& kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s
$rolloutExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($rolloutExit -ne 0) {
    & kubectl get pods -n swcp -l app.kubernetes.io/name=c-sandbox
    throw 'c-sandbox rollout did not complete within 120s'
}

$runtimeImage = 'ghcr.io/allankrogaau/c-sandbox-runtime:latest'
$tarPath = Join-Path ([System.IO.Path]::GetTempPath()) "c-sandbox-runtime-$([guid]::NewGuid()).tar"
try {
    Invoke-Native docker @('save', '-o', $tarPath, $runtimeImage)
    $pods = (& kubectl get pods -n swcp -l app.kubernetes.io/name=c-sandbox --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}') -split '\s+' | Where-Object { $_ }
    foreach ($pod in $pods) {
        $loader = Start-Process -FilePath kubectl -ArgumentList @('exec', '-i', '-n', 'swcp', $pod, '-c', 'dind', '--', 'docker', '-H', 'tcp://127.0.0.1:2375', 'load') -RedirectStandardInput $tarPath -NoNewWindow -Wait -PassThru
        if ($loader.ExitCode -ne 0) {
            throw "Loading $runtimeImage into $pod failed with exit code $($loader.ExitCode)"
        }
        Invoke-Native kubectl @('exec', '-n', 'swcp', $pod, '-c', 'dind', '--', 'docker', '-H', 'tcp://127.0.0.1:2375', 'tag', $runtimeImage, 'c-sandbox-runtime:latest')
    }
}
finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $tarPath
}
