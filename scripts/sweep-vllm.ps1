<#
.SYNOPSIS
    Automates single-node vLLM Hardware Parameter Sweep across ranges or lists of
    VLLM_GPU_MEMORY_UTILIZATION and VLLM_MAX_NUM_SEQS, then optionally replicates
    the winning config across both cluster GPUs.
.DESCRIPTION
    Supports both local and off-cluster execution via --ssh-host.
.EXAMPLE
    .\scripts\sweep-vllm.ps1 --ssh-host school-project-pc --mem-values 0.88 --seqs-values 4,8,12,16 --apply-winner
.EXAMPLE
    .\scripts\sweep-vllm.ps1 --ssh-host school-project-pc --report-only
#>
$ErrorActionPreference = "Stop"
$ROOT = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $ROOT

. "$PSScriptRoot\lib-benchmark-cluster.ps1"

function Get-TomlVal {
    param([string]$Section, [string]$Key, [string]$Fallback)
    $tomlPath = Join-Path $ROOT "benchmarks\suites.toml"
    if (Test-Path $tomlPath) {
        $py = if (Get-Command "py" -ErrorAction SilentlyContinue) { "py" } else { "python" }
        $val = try {
            & $py -c @"
import tomllib, sys
try:
    with open(r'$tomlPath', 'rb') as f:
        d = tomllib.load(f)
    s = d
    for p in '$Section'.split('.'):
        s = s.get(p, {}) if isinstance(s, dict) else {}
    v = s.get('$Key') if isinstance(s, dict) else None
    if v is not None:
        print(v)
except Exception:
    pass
"@ 2>$null
        } catch { $null }
        if ($val -and $val.Trim()) { return $val.Trim() }
    }
    return $Fallback
}

# Defaults
$MemValues = Get-TomlVal "suites.vllm_sweep" "mem_values" "0.88"
$SeqsValues = Get-TomlVal "suites.vllm_sweep" "seqs_values" "4,8,12,16"
$MemRange = ""
$SeqsRange = ""
$ExplicitPairs = ""
$TargetUrl = "http://localhost"
$TargetUrlSet = $false
$Rate = "16"
$RateSet = $false
$Concurrency = Get-TomlVal "suites.vllm_sweep" "concurrency" "16"
$Duration = Get-TomlVal "suites.vllm_sweep" "step_duration_s" "120"
$MinCompletions = Get-TomlVal "suites.vllm_sweep" "min_completions" "16"
$Users = Get-TomlVal "common" "users" "200"
$HardwarePrefix = "rtx5070ti"
$DrainTimeout = 90
$ApplyWinner = $false
$ReportOnly = $false
$SweepId = ""
$OverflowServiceUrl = "http://local-llm-overflow.swcp.svc.cluster.local:8080"

# Parse CLI arguments
$rawArgs = @($args)
$idx = 0
while ($idx -lt $rawArgs.Count) {
    $a = $rawArgs[$idx]
    switch -Regex ($a) {
        '^--mem-range$'          { $MemRange = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--mem-range=(.+)$'     { $MemRange = $matches[1]; $idx++; continue }
        '^--mem-values$'         { $MemValues = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--mem-values=(.+)$'    { $MemValues = $matches[1]; $idx++; continue }
        '^--seqs-range$'         { $SeqsRange = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--seqs-range=(.+)$'    { $SeqsRange = $matches[1]; $idx++; continue }
        '^--seqs-values$'        { $SeqsValues = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--seqs-values=(.+)$'   { $SeqsValues = $matches[1]; $idx++; continue }
        '^--pairs$'              { $ExplicitPairs = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--pairs=(.+)$'         { $ExplicitPairs = $matches[1]; $idx++; continue }
        '^--rate$'               { $Rate = $rawArgs[$idx + 1]; $RateSet = $true; $Concurrency = ""; $idx += 2; continue }
        '^--rate=(.+)$'          { $Rate = $matches[1]; $RateSet = $true; $Concurrency = ""; $idx++; continue }
        '^--concurrency$'        { $Concurrency = $rawArgs[$idx + 1]; $RateSet = $false; $idx += 2; continue }
        '^--concurrency=(.+)$'   { $Concurrency = $matches[1]; $RateSet = $false; $idx++; continue }
        '^--duration$'           { $Duration = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--duration=(.+)$'      { $Duration = $matches[1]; $idx++; continue }
        '^--min-completions$'    { $MinCompletions = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--min-completions=(.+)$'{ $MinCompletions = $matches[1]; $idx++; continue }
        '^--drain-timeout$'      { $DrainTimeout = [int]$rawArgs[$idx + 1]; $idx += 2; continue }
        '^--drain-timeout=(.+)$' { $DrainTimeout = [int]$matches[1]; $idx++; continue }
        '^--users$'              { $Users = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--users=(.+)$'         { $Users = $matches[1]; $idx++; continue }
        '^--hardware$'           { $HardwarePrefix = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--hardware=(.+)$'      { $HardwarePrefix = $matches[1]; $idx++; continue }
        '^--target-url$'         { $TargetUrl = $rawArgs[$idx + 1]; $TargetUrlSet = $true; $idx += 2; continue }
        '^--target-url=(.+)$'    { $TargetUrl = $matches[1]; $TargetUrlSet = $true; $idx++; continue }
        '^--ssh-host$'           { $global:SWCP_CONTROL_SSH = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--ssh-host=(.+)$'      { $global:SWCP_CONTROL_SSH = $matches[1]; $idx++; continue }
        '^--sweep-id$'           { $SweepId = $rawArgs[$idx + 1]; $idx += 2; continue }
        '^--sweep-id=(.+)$'      { $SweepId = $matches[1]; $idx++; continue }
        '^--apply-winner$'       { $ApplyWinner = $true; $idx++; continue }
        '^--report-only$'        { $ReportOnly = $true; $idx++; continue }
        '^(-h|--help)$' {
            Write-Host @"
Usage: .\scripts\sweep-vllm.ps1 [OPTIONS]

Variable Range / Value Options:
  --mem-range START:END:STEP     Range for VLLM_GPU_MEMORY_UTILIZATION (e.g. 0.80:0.92:0.04)
  --mem-values v1,v2,...         Comma-separated memory utilization values (default: $MemValues)
  --seqs-range START:END:STEP    Range for VLLM_MAX_NUM_SEQS (e.g. 4:16:4)
  --seqs-values s1,s2,...        Comma-separated max_num_seqs values (default: $SeqsValues)
  --pairs m1:s1,m2:s2,...        Test specific (mem:seqs) pairs instead of full Cartesian grid

Load & Execution Options:
  --rate RPM                     Use Poisson arrival rate/min instead of default closed-loop concurrency
  --concurrency COUNT            Closed-loop concurrency level (default from suites.toml: $Concurrency)
  --duration SECONDS             Duration in seconds per configuration (default: $Duration)
  --min-completions COUNT        Minimum completed submissions per configuration (default: $MinCompletions)
  --drain-timeout SECONDS        Max seconds to wait for queues/DB to drain between runs (default: 90)
  --users COUNT                  Pre-seeded virtual user pool size (default: $Users)
  --hardware TAG                 Hardware prefix tag (default: rtx5070ti)
  --target-url URL               Target ingress URL (default: http://localhost, or http://<ssh-host>)
  --ssh-host USER@HOST           Run kubectl & monitoring tunnels over SSH (off-cluster runner mode)
  --sweep-id ID                  Custom sweep ID folder name
  --apply-winner                 Automatically apply the winning config to k8s when finished
  --report-only                  Do not run new tests; print table & plot from existing SQLite data
  -h, --help                     Show this help message
"@
            exit 0
        }
        default {
            Write-Error "Unknown option: $a"
            exit 1
        }
    }
}

if ($env:SWCP_CONTROL_SSH -and -not $global:SWCP_CONTROL_SSH) {
    $global:SWCP_CONTROL_SSH = $env:SWCP_CONTROL_SSH
}

if ($global:SWCP_CONTROL_SSH -and -not $TargetUrlSet) {
    $resolvedHost = Resolve-SshHostAddress $global:SWCP_CONTROL_SSH
    $TargetUrl = "http://${resolvedHost}"
} else {
    $TargetUrl = Resolve-TargetUrl $TargetUrl $global:SWCP_CONTROL_SSH
}

$ResultsDir = Join-Path $ROOT "benchmarks\results"
if (-not (Test-Path $ResultsDir)) {
    New-Item -ItemType Directory -Path $ResultsDir -Force | Out-Null
}

$needRestoreConfig = $false
$winnerApplied = $false
$origMem = Get-RemoteEnvVar "VLLM_GPU_MEMORY_UTILIZATION" "0.88"
$origSeqs = Get-RemoteEnvVar "VLLM_MAX_NUM_SEQS" "16"
$origThreshold = Get-RemoteEnvVar "VLLM_OVERFLOW_THRESHOLD" $origSeqs
$origMaxAck = Get-RemoteEnvVar "LLM_LOCAL_MAX_ACK_PENDING" "$([int]$origSeqs * 2)"
$origSbxReadyTimeout = (Invoke-KubectlCapture get configmap c-sandbox-config -n swcp -o jsonpath="{.data.SANDBOX_READY_TIMEOUT_S}")

function Restore-TwoNodeConfig {
    if (-not $needRestoreConfig -or $winnerApplied) { return }
    Write-Host ""
    Write-Host "=== Restoring 2-Node Configuration (MEM=$origMem, SEQS=$origSeqs, OverflowThreshold=$origThreshold, MaxAckPending=$origMaxAck) ===" -ForegroundColor Magenta
    Write-Host ">>> Restoring vllm-config and llm-worker-config..." -ForegroundColor Cyan
    Invoke-Kubectl patch configmap vllm-config -n swcp --type=merge -p "{`"data`":{`"VLLM_GPU_MEMORY_UTILIZATION`":`"$origMem`",`"VLLM_MAX_NUM_SEQS`":`"$origSeqs`"}}" | Out-Null
    Invoke-Kubectl patch configmap llm-worker-config -n swcp --type=merge -p "{`"data`":{`"LocalLlm__MockEnabled`":`"false`",`"LocalLlm__OverflowBaseUrl`":`"$OverflowServiceUrl`",`"LocalLlm__PrimaryMaxConcurrent`":`"$origThreshold`",`"LocalLlm__MaxConcurrent`":`"$origSeqs`",`"LocalLlm__MaxAckPending`":`"$origMaxAck`"}}" | Out-Null
    if ($origSbxReadyTimeout) {
        Invoke-Kubectl patch configmap c-sandbox-config -n swcp --type=merge -p "{`"data`":{`"SANDBOX_READY_TIMEOUT_S`":`"$origSbxReadyTimeout`"}}" | Out-Null
    } else {
        Invoke-Kubectl patch configmap c-sandbox-config -n swcp --type=json -p '[{"op":"remove","path":"/data/SANDBOX_READY_TIMEOUT_S"}]' 2>$null | Out-Null
    }
    Write-Host ">>> Triggering rollout restart of vllm, vllm-overflow, llm-worker, c-sandbox..." -ForegroundColor Cyan
    Invoke-Kubectl rollout restart deployment/vllm deployment/vllm-overflow deployment/llm-worker deployment/c-sandbox -n swcp | Out-Null
    Write-Host ">>> Waiting for deployments to become ready..." -ForegroundColor Cyan
    Invoke-Kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
    Invoke-Kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s
    Invoke-Kubectl rollout status deployment/vllm -n swcp --timeout=300s
    Invoke-Kubectl rollout status deployment/vllm-overflow -n swcp --timeout=300s
    Write-Host "[OK] Cluster configuration and pods restored." -ForegroundColor Green
}

try {
    Start-MonitoringTunnel
    Measure-InterNodeRtt

    if ($ReportOnly) {
        Write-Host "=== Generating vLLM Sweep Table and Consolidated Plot from SQLite ===" -ForegroundColor Cyan
        if ($SweepId) {
            Invoke-BenchmarkCli sweep-report --sweep-id $SweepId
        } else {
            Invoke-BenchmarkCli sweep-report
        }
        exit 0
    }

    # Generate Test Pairs
    $TestPairs = @()
    if ($ExplicitPairs) {
        $TestPairs = ($ExplicitPairs -split ',') | Where-Object { $_.Trim() }
    } else {
        $memList = @()
        if ($MemRange) {
            $mParts = $MemRange -split ':'
            $mStart = [double]$mParts[0]; $mEnd = [double]$mParts[1]; $mStep = [double]$mParts[2]
            for ($curr = $mStart; $curr -le $mEnd + 0.0001; $curr += $mStep) {
                $memList += ("{0:N2}" -f $curr)
            }
        } else {
            $memList = ("$MemValues" -split '[, ]+') | Where-Object { $_.Trim() }
        }

        $seqsList = @()
        if ($SeqsRange) {
            $sParts = $SeqsRange -split ':'
            $sStart = [int]$sParts[0]; $sEnd = [int]$sParts[1]; $sStep = [int]$sParts[2]
            for ($curr = $sStart; $curr -le $sEnd; $curr += $sStep) {
                $seqsList += "$curr"
            }
        } else {
            $seqsList = ("$SeqsValues" -split '[, ]+') | Where-Object { $_.Trim() }
        }

        foreach ($m in $memList) {
            foreach ($s in $seqsList) {
                $TestPairs += "${m}:${s}"
            }
        }
    }

    if ($TestPairs.Count -eq 0) {
        Write-Error "No (MEM:SEQS) combinations generated."
        exit 1
    }

    if (-not $SweepId) {
        $SweepId = (Get-Date).ToString("yyyy-MM-dd_HH-mm-ss") + "_obj3_vllm_sweep"
    }

    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host " Starting Single-Node vLLM Parameter Sweep ($SweepId)" -ForegroundColor Cyan
    Write-Host " Target: $TargetUrl$(if ($global:SWCP_CONTROL_SSH) { " | Cluster SSH: $global:SWCP_CONTROL_SSH" })" -ForegroundColor Cyan
    Write-Host " Total configurations to test: $($TestPairs.Count) -> $($TestPairs -join ', ')" -ForegroundColor Cyan
    if ($Concurrency) {
        Write-Host " Closed-loop concurrency: $Concurrency | Duration: ${Duration}s (overflow isolated/disabled)" -ForegroundColor Cyan
    } else {
        Write-Host " Target rate: ${Rate}/min | Duration: ${Duration}s | Min completions: $MinCompletions (overflow isolated/disabled)" -ForegroundColor Cyan
    }
    Write-Host "==================================================================" -ForegroundColor Cyan

    $needRestoreConfig = $true

    # Raise sandbox ready timeout to 60s
    Invoke-Kubectl patch configmap c-sandbox-config -n swcp --type=merge -p '{"data":{"SANDBOX_READY_TIMEOUT_S":"60"}}' | Out-Null
    Invoke-Kubectl rollout restart deployment/c-sandbox -n swcp | Out-Null
    Invoke-Kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s | Out-Null

    Wait-PipelineDrain -TimeoutSeconds $DrainTimeout
    Invoke-BenchmarkCli seed --users $Users --target-url $TargetUrl

    $idx = 0
    $total = $TestPairs.Count
    foreach ($cfg in $TestPairs) {
        $idx++
        $mem = ($cfg -split ':')[0]
        $seqs = ($cfg -split ':')[1]

        Write-Host ""
        Write-Host "------------------------------------------------------------------" -ForegroundColor Yellow
        Write-Host " [$idx/$total] Testing Single-Node VLLM_GPU_MEMORY_UTILIZATION=$mem, VLLM_MAX_NUM_SEQS=$seqs" -ForegroundColor Yellow
        Write-Host "------------------------------------------------------------------" -ForegroundColor Yellow

        # 1. Patch vllm-config and isolate llm-worker to primary vLLM only
        Invoke-Kubectl patch configmap vllm-config -n swcp --type=merge -p "{`"data`":{`"VLLM_GPU_MEMORY_UTILIZATION`":`"$mem`",`"VLLM_MAX_NUM_SEQS`":`"$seqs`"}}" | Out-Null
        Invoke-Kubectl patch configmap llm-worker-config -n swcp --type=merge -p "{`"data`":{`"LocalLlm__MockEnabled`":`"false`",`"LocalLlm__OverflowBaseUrl`":`"`",`"LocalLlm__PrimaryMaxConcurrent`":`"$seqs`",`"LocalLlm__MaxConcurrent`":`"$seqs`",`"LocalLlm__MaxAckPending`":`"$seqs`"}}" | Out-Null

        # 2. Restart primary vllm & llm-worker
        Invoke-Kubectl rollout restart deployment/vllm deployment/llm-worker -n swcp | Out-Null
        Invoke-Kubectl rollout status deployment/llm-worker -n swcp --timeout=120s | Out-Null
        $vllmRollout = (Invoke-Kubectl rollout status deployment/vllm -n swcp --timeout=300s)
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "vLLM failed to start within 300s for MEM=$mem, SEQS=$seqs (likely OOM). Skipping..."
            continue
        }

        Write-Host ">>> Warming up primary vLLM (3 requests) and allowing c-sandbox warm pool to refill..." -ForegroundColor Cyan
        try {
            Invoke-BenchmarkCli warmup --count 3 --target-url $TargetUrl
        } catch {}
        Wait-PipelineDrain -TimeoutSeconds $DrainTimeout
        Start-Sleep -Seconds 12

        # 3. Record this point into SQLite
        $arrivalArgs = @()
        if ($Concurrency) {
            $arrivalArgs = @("--arrival-mode", "closed-loop", "--concurrency", "$Concurrency")
        } else {
            $arrivalArgs = @("--arrival-mode", "stepped-poisson", "--rates", "$Rate", "--min-completions", "$MinCompletions")
        }

        $benchArgs = @(
            "run",
            "--suite", "obj3_vllm_sweep",
            "--sweep-id", $SweepId,
            "--skip-plots",
            "--vllm-gpu-mem", $mem,
            "--vllm-max-seqs", $seqs,
            "--hardware-tag", "${HardwarePrefix}_mem${mem}_seqs${seqs}"
        ) + $arrivalArgs + @(
            "--step-duration-s", "$Duration",
            "--users", "$Users",
            "--target-url", $TargetUrl
        )

        try {
            Invoke-BenchmarkCli @benchArgs
        } catch {
            Write-Warning "Benchmark step failed for MEM=$mem, SEQS=$seqs; continuing sweep..."
        }

        Wait-PipelineDrain -TimeoutSeconds $DrainTimeout
    }

    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host " Generating Consolidated Sweep Report and Plot ($SweepId)" -ForegroundColor Cyan
    Write-Host "==================================================================" -ForegroundColor Cyan
    Invoke-BenchmarkCli sweep-report --sweep-id $SweepId

    $winnerFile = Join-Path $ROOT "benchmarks\results\$SweepId\winner.env"
    if ($ApplyWinner -and (Test-Path $winnerFile)) {
        $bestMem = ""
        $bestSeqs = ""
        Get-Content $winnerFile | ForEach-Object {
            if ($_ -match '^BEST_MEM=(.+)$') { $bestMem = $matches[1].Trim() }
            if ($_ -match '^BEST_SEQS=(.+)$') { $bestSeqs = $matches[1].Trim() }
        }
        if ($bestMem -and $bestSeqs) {
            $bestThreshold = $bestSeqs
            $bestMaxAck = [string]([int]$bestSeqs * 2)
            Write-Host ""
            Write-Host "=== Replicating Winning Config (MEM=$bestMem, SEQS=$bestSeqs) to Both Primary and Overflow vLLM ===" -ForegroundColor Magenta
            Invoke-Kubectl patch configmap vllm-config -n swcp --type=merge -p "{`"data`":{`"VLLM_GPU_MEMORY_UTILIZATION`":`"$bestMem`",`"VLLM_MAX_NUM_SEQS`":`"$bestSeqs`"}}" | Out-Null
            Invoke-Kubectl patch configmap llm-worker-config -n swcp --type=merge -p "{`"data`":{`"LocalLlm__MockEnabled`":`"false`",`"LocalLlm__OverflowBaseUrl`":`"$OverflowServiceUrl`",`"LocalLlm__PrimaryMaxConcurrent`":`"$bestThreshold`",`"LocalLlm__MaxConcurrent`":`"$bestSeqs`",`"LocalLlm__MaxAckPending`":`"$bestMaxAck`"}}" | Out-Null
            if ($origSbxReadyTimeout) {
                Invoke-Kubectl patch configmap c-sandbox-config -n swcp --type=merge -p "{`"data`":{`"SANDBOX_READY_TIMEOUT_S`":`"$origSbxReadyTimeout`"}}" | Out-Null
            } else {
                Invoke-Kubectl patch configmap c-sandbox-config -n swcp --type=json -p '[{"op":"remove","path":"/data/SANDBOX_READY_TIMEOUT_S"}]' 2>$null | Out-Null
            }
            Invoke-Kubectl rollout restart deployment/vllm deployment/vllm-overflow deployment/llm-worker deployment/c-sandbox -n swcp | Out-Null
            Invoke-Kubectl rollout status deployment/llm-worker -n swcp --timeout=120s | Out-Null
            Invoke-Kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s | Out-Null
            Invoke-Kubectl rollout status deployment/vllm -n swcp --timeout=300s | Out-Null
            Invoke-Kubectl rollout status deployment/vllm-overflow -n swcp --timeout=300s | Out-Null
            $winnerApplied = $true
            Write-Host "[OK] Both GPUs now running optimal settings: MEM=$bestMem, SEQS=$bestSeqs, OverflowThreshold=$bestThreshold, MaxAckPending=$bestMaxAck" -ForegroundColor Green

            Set-RemoteEnvVar "VLLM_GPU_MEMORY_UTILIZATION" $bestMem
            Set-RemoteEnvVar "VLLM_MAX_NUM_SEQS" $bestSeqs
            Set-RemoteEnvVar "VLLM_OVERFLOW_THRESHOLD" $bestThreshold
            Set-RemoteEnvVar "LLM_LOCAL_MAX_ACK_PENDING" $bestMaxAck
            Write-Host "[OK] Updated .env: MEM=$bestMem, SEQS=$bestSeqs, VLLM_OVERFLOW_THRESHOLD=$bestThreshold, LLM_LOCAL_MAX_ACK_PENDING=$bestMaxAck" -ForegroundColor Green
        }
    }
}
finally {
    Restore-TwoNodeConfig
    Stop-MonitoringTunnel
}
