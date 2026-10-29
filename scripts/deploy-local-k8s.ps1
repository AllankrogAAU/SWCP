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

$ErrorActionPreference = 'Continue'
& kubectl get crd scaledobjects.keda.sh 2>$null | Out-Null
$kedaMissing = $LASTEXITCODE -ne 0
$ErrorActionPreference = 'Stop'
if ($kedaMissing) {
    Invoke-Native kubectl @('apply', '--server-side', '-f', 'https://github.com/kedacore/keda/releases/download/v2.16.1/keda-2.16.1.yaml')
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

$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    $secretData = @{}
    $vllmData = @{}
    $llmWorkerData = @{}
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*AzureAIFoundry_*Models_*([A-Za-z0-9]+)_*ApiKey\s*=\s*(.+?)\s*$') {
            $secretData["AzureAIFoundry__Models__$($Matches[1])__ApiKey"] = $Matches[2]
        }
        elseif ($line -match '^\s*(LLM_MODEL_PATH|LLM_MODEL_NAME|LOCAL_LLM_PORT|VLLM_QUANTIZATION|VLLM_KV_CACHE_DTYPE|VLLM_MAX_MODEL_LEN|VLLM_GPU_MEMORY_UTILIZATION|VLLM_MAX_NUM_SEQS|LLM_LOCAL_MAX_CONCURRENT|LLM_LOCAL_MAX_ACK_PENDING)\s*=\s*([^#]+)') {
            $k = $Matches[1]
            $v = $Matches[2].Trim().Trim('"').Trim("'")
            if ($v) {
                switch ($k) {
                    'LOCAL_LLM_PORT' { $vllmData['VLLM_PORT'] = $v }
                    'LLM_LOCAL_MAX_CONCURRENT' { $llmWorkerData['LocalLlm__MaxConcurrent'] = $v }
                    'LLM_LOCAL_MAX_ACK_PENDING' { $llmWorkerData['LocalLlm__MaxAckPending'] = $v }
                    default {
                        $vllmData[$k] = $v
                        if ($k -eq 'LLM_MODEL_NAME') { $llmWorkerData['LocalLlm__ModelName'] = $v }
                        if ($k -eq 'VLLM_MAX_NUM_SEQS') {
                            if (-not $llmWorkerData.ContainsKey('LocalLlm__MaxConcurrent')) { $llmWorkerData['LocalLlm__MaxConcurrent'] = $v }
                            if (-not $llmWorkerData.ContainsKey('LocalLlm__MaxAckPending')) { $llmWorkerData['LocalLlm__MaxAckPending'] = $v }
                        }
                    }
                }
            }
        }
    }
    if ($vllmData.Count -gt 0) {
        $patch = @{ data = $vllmData } | ConvertTo-Json -Compress
        $patchFile = Join-Path ([System.IO.Path]::GetTempPath()) "vllm-config-$([guid]::NewGuid()).json"
        try {
            [System.IO.File]::WriteAllText($patchFile, $patch)
            Invoke-Native kubectl @('patch', 'configmap', 'vllm-config', '-n', 'swcp', '--type=merge', "--patch-file=$patchFile")
        }
        finally {
            Remove-Item -Force -ErrorAction SilentlyContinue $patchFile
        }
    }
    if ($llmWorkerData.Count -gt 0) {
        $patch = @{ data = $llmWorkerData } | ConvertTo-Json -Compress
        $patchFile = Join-Path ([System.IO.Path]::GetTempPath()) "llm-worker-config-$([guid]::NewGuid()).json"
        try {
            [System.IO.File]::WriteAllText($patchFile, $patch)
            Invoke-Native kubectl @('patch', 'configmap', 'llm-worker-config', '-n', 'swcp', '--type=merge', "--patch-file=$patchFile")
        }
        finally {
            Remove-Item -Force -ErrorAction SilentlyContinue $patchFile
        }
    }
    if ($secretData.Count -gt 0) {
        $patch = @{ stringData = $secretData } | ConvertTo-Json -Compress
        $patchFile = Join-Path ([System.IO.Path]::GetTempPath()) "llm-secret-$([guid]::NewGuid()).json"
        try {
            [System.IO.File]::WriteAllText($patchFile, $patch)
            Invoke-Native kubectl @('patch', 'secret', 'llm-worker-secrets', '-n', 'swcp', '--type=merge', "--patch-file=$patchFile")
        }
        finally {
            Remove-Item -Force -ErrorAction SilentlyContinue $patchFile
        }
    }
    if ($secretData.Count -gt 0 -or $llmWorkerData.Count -gt 0) {
        Invoke-Native kubectl @('rollout', 'restart', 'deployment/llm-worker', '-n', 'swcp')
    }
}

if ($env:SWCP_GVISOR -eq '1') {
    Write-Host 'SWCP_GVISOR=1: sandbox pods will use the gvisor RuntimeClass (node needs runsc + label swcp.io/gvisor=true).'
}
else {
    Write-Warning 'Local mode: sandbox pods run WITHOUT gVisor (Docker Desktop has no runsc). Set SWCP_GVISOR=1 on a node with gVisor installed.'
    $patchFile = Join-Path ([System.IO.Path]::GetTempPath()) "sandbox-runtime-$([guid]::NewGuid()).json"
    try {
        [System.IO.File]::WriteAllText($patchFile, '{"data":{"SANDBOX_RUNTIME_CLASS":""}}')
        Invoke-Native kubectl @('patch', 'configmap', 'c-sandbox-config', '-n', 'swcp', '--type=merge', "--patch-file=$patchFile")
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $patchFile
    }
}
Invoke-Native kubectl @('rollout', 'restart', 'deployment/c-sandbox', '-n', 'swcp')
$ErrorActionPreference = 'Continue'
& kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s
$rolloutExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($rolloutExit -ne 0) {
    & kubectl get pods -n swcp -l app.kubernetes.io/name=c-sandbox
    throw 'c-sandbox rollout did not complete within 120s'
}
