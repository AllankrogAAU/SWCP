<#
.SYNOPSIS
    Runs the SWCP benchmark suite locally on Windows or remotely against a Kubernetes cluster.
.DESCRIPTION
    Supports both on-cluster execution and off-cluster remote execution via
    --ssh-host <user@control-node> (with out-of-band kubectl + monitoring tunnels).
.EXAMPLE
    .\scripts\run-benchmarks.ps1 smoke-test --ssh-host school-project-pc
.EXAMPLE
    .\scripts\run-benchmarks.ps1 run --suite ramp_capacity --ssh-host school-project-pc
.EXAMPLE
    .\scripts\run-benchmarks.ps1 run --suite infinite_gpu --ssh-host school-project-pc
#>
$ErrorActionPreference = "Stop"
$ROOT = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $ROOT

. "$PSScriptRoot\lib-benchmark-cluster.ps1"

$ResultsDir = Join-Path $ROOT "benchmarks\results"
if (-not (Test-Path $ResultsDir)) {
    New-Item -ItemType Directory -Path $ResultsDir -Force | Out-Null
}

$IsMockSuite = $false
$IsRunCmd = $false
$HasHelp = $false
$SkipWorkerCooldown = $false
$TargetUrl = "http://localhost"
$TargetUrlSet = $false
$CliArgs = @()

$rawArgs = @($args)
$idx = 0
while ($idx -lt $rawArgs.Count) {
    $arg = $rawArgs[$idx]
    if ($arg -eq '--skip-worker-cooldown') {
        $SkipWorkerCooldown = $true
        $idx++
        continue
    }
    if ($arg -eq '--ssh-host') {
        if ($idx + 1 -ge $rawArgs.Count) {
            Write-Error "--ssh-host requires an argument (e.g. user@control-node)"
            exit 1
        }
        $global:SWCP_CONTROL_SSH = $rawArgs[$idx + 1]
        $idx += 2
        continue
    }
    if ($arg -match '^--ssh-host=(.+)$') {
        $global:SWCP_CONTROL_SSH = $matches[1]
        $idx++
        continue
    }
    if ($arg -eq '--target-url') {
        $CliArgs += $arg
        if ($idx + 1 -lt $rawArgs.Count) {
            $TargetUrl = $rawArgs[$idx + 1]
            $TargetUrlSet = $true
            $CliArgs += $TargetUrl
            $idx += 2
            continue
        }
    }
    if ($arg -match '^--target-url=(.+)$') {
        $TargetUrl = $matches[1]
        $TargetUrlSet = $true
        $CliArgs += $arg
        $idx++
        continue
    }
    if ($arg -in @('-h', '--help')) {
        $HasHelp = $true
    }
    if ($arg -eq 'run') {
        $IsRunCmd = $true
    }
    if ($arg -in @('obj2_infinite_gpu', 'infinite_gpu', '--mock-llm')) {
        $IsMockSuite = $true
    }

    $CliArgs += $arg
    $idx++
}

if ($env:SWCP_CONTROL_SSH -and -not $global:SWCP_CONTROL_SSH) {
    $global:SWCP_CONTROL_SSH = $env:SWCP_CONTROL_SSH
}

if ($global:SWCP_CONTROL_SSH -and -not $TargetUrlSet) {
    $resolvedHost = Resolve-SshHostAddress $global:SWCP_CONTROL_SSH
    $TargetUrl = "http://${resolvedHost}"
    if (-not $HasHelp) {
        $CliArgs += @("--target-url", $TargetUrl)
    }
} elseif ($TargetUrlSet) {
    $resolvedUrl = Resolve-TargetUrl $TargetUrl $global:SWCP_CONTROL_SSH
    if ($resolvedUrl -ne $TargetUrl) {
        $TargetUrl = $resolvedUrl
        for ($i = 0; $i -lt $CliArgs.Count; $i++) {
            if ($CliArgs[$i] -eq '--target-url' -and $i + 1 -lt $CliArgs.Count) {
                $CliArgs[$i + 1] = $TargetUrl
            } elseif ($CliArgs[$i] -match '^--target-url=') {
                $CliArgs[$i] = "--target-url=$TargetUrl"
            }
        }
    }
}

if ($HasHelp) {
    Invoke-BenchmarkCli --help
    exit 0
}

$mockActive = $false
$restoreSeqs = Get-RemoteEnvVar "VLLM_MAX_NUM_SEQS" "16"
$restoreThreshold = Get-RemoteEnvVar "VLLM_OVERFLOW_THRESHOLD" $restoreSeqs
$restoreMaxAck = Get-RemoteEnvVar "LLM_LOCAL_MAX_ACK_PENDING" "$([int]$restoreSeqs * 2)"
$restoreOverflowUrl = "http://local-llm-overflow.swcp.svc.cluster.local:8080"

try {
    Start-MonitoringTunnel
    Measure-InterNodeRtt

    if ($IsMockSuite) {
        $mockActive = $true
        Write-Host ""
        Write-Host "=== Enabling Infinite GPU (Mock LLM) Mode on Cluster ===" -ForegroundColor Magenta
        Invoke-Kubectl patch configmap llm-worker-config -n swcp --type=merge -p '{"data":{"LocalLlm__MockEnabled":"true","LocalLlm__MockLatencyMs":"5","LocalLlm__MaxConcurrent":"64","LocalLlm__MaxAckPending":"128"}}'
        Invoke-Kubectl rollout restart deployment/llm-worker -n swcp
        Invoke-Kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
        Write-Host "[OK] Infinite GPU (Mock LLM) mode active." -ForegroundColor Green

        Write-Host "=== Running Warmup Submissions (3 requests) after rollout ===" -ForegroundColor Cyan
        try {
            Invoke-BenchmarkCli warmup --count 3 --target-url $TargetUrl
        } catch {}
    }

    Wait-PipelineDrain
    if ($IsRunCmd -and -not $SkipWorkerCooldown) {
        Wait-WorkerBaseline
    }

    $targetDesc = "target=$TargetUrl"
    if ($global:SWCP_CONTROL_SSH) { $targetDesc += ", ssh=$global:SWCP_CONTROL_SSH" }
    Write-Host "=== Running SWCP Benchmarks ($targetDesc) ===" -ForegroundColor Green
    Invoke-BenchmarkCli @CliArgs

    Wait-PipelineDrain
}
finally {
    if ($mockActive) {
        Write-Host ""
        Write-Host "=== Restoring 2-Node Local LLM Mode (MockEnabled=false, MaxConcurrent=$restoreSeqs, OverflowThreshold=$restoreThreshold, MaxAckPending=$restoreMaxAck) ===" -ForegroundColor Magenta
        try {
            Invoke-Kubectl patch configmap llm-worker-config -n swcp --type=merge -p "{`"data`":{`"LocalLlm__MockEnabled`":`"false`",`"LocalLlm__OverflowBaseUrl`":`"$restoreOverflowUrl`",`"LocalLlm__PrimaryMaxConcurrent`":`"$restoreThreshold`",`"LocalLlm__MaxConcurrent`":`"$restoreSeqs`",`"LocalLlm__MaxAckPending`":`"$restoreMaxAck`"}}"
            Invoke-Kubectl rollout restart deployment/llm-worker -n swcp
            Invoke-Kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
            Write-Host "[OK] Restored local LLM mode on cluster." -ForegroundColor Green

            Write-Host "=== Running Warmup Submissions (3 requests) after rollout ===" -ForegroundColor Cyan
            try {
                Invoke-BenchmarkCli warmup --count 3 --target-url $TargetUrl
            } catch {}
            Wait-PipelineDrain
        } catch {
            Write-Warning "Failed to restore LLM worker mode automatically: $_"
        }
    }
    Stop-MonitoringTunnel
}
