# ==============================================================================
# lib-benchmark-cluster.ps1
# Shared cluster orchestration, SSH tunnel, and telemetry helpers for
# run-benchmarks.ps1 and sweep-vllm.ps1 on Windows PowerShell.
# ==============================================================================

$script:SWCP_TUNNEL_PROC = $null
$script:LocalPromPort = $null
$script:LocalLokiPort = $null

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    return $port
}

function Resolve-SshHostAddress {
    param([string]$SshTarget)
    if (-not $SshTarget) { return "" }
    $hostPart = ($SshTarget -split '@')[-1]
    if ($hostPart -match '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$') {
        return $hostPart
    }

    # 1. Direct SSH config file lookup
    $sshConfigFile = Join-Path $env:USERPROFILE ".ssh\config"
    if (Test-Path $sshConfigFile) {
        try {
            $inTargetHost = $false
            foreach ($line in (Get-Content $sshConfigFile -ErrorAction SilentlyContinue)) {
                $trimmed = $line.Trim()
                if ($trimmed -match '^Host\s+(.+)$') {
                    $hosts = $matches[1] -split '\s+'
                    $inTargetHost = ($hosts -contains $hostPart)
                } elseif ($inTargetHost -and $trimmed -match '^HostName\s+(.+)$') {
                    return $matches[1].Trim()
                }
            }
        } catch {}
    }

    # 2. ssh -G fallback with ErrorActionPreference protection
    $prevEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $sshG = &(Get-Command ssh.exe -CommandType Application) -q -G $hostPart 2>$null
        $resolvedLine = ($sshG | Where-Object { $_ -match '^hostname\s+(.+)$' } | Select-Object -First 1)
        if ($resolvedLine -match '^hostname\s+(.+)$') {
            return $matches[1].Trim()
        }
    } catch {}
    finally {
        $ErrorActionPreference = $prevEap
    }

    return $hostPart
}

function Resolve-TargetUrl {
    param([string]$Url, [string]$SshTarget = "")
    if (-not $Url) { return $Url }
    try {
        $uri = [System.Uri]$Url
        $hostName = $uri.Host
        if ($hostName -notmatch '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$') {
            $resolves = $false
            try {
                $null = [System.Net.Dns]::GetHostEntry($hostName)
                $resolves = $true
            } catch {}
            if (-not $resolves) {
                $sshCheck = if ($SshTarget) { $SshTarget } else { $hostName }
                $resolved = Resolve-SshHostAddress $sshCheck
                if ($resolved -and $resolved -ne $hostName) {
                    $builder = [System.UriBuilder]::new($uri)
                    $builder.Host = $resolved
                    return $builder.Uri.ToString().TrimEnd('/')
                }
            }
        }
    } catch {}
    return $Url
}

function Invoke-Kubectl {
    $KubectlArgs = @($args)
    if ($global:SWCP_CONTROL_SSH) {
        $escapedArgs = $KubectlArgs | ForEach-Object {
            "'" + ($_ -replace "'", "'\''") + "'"
        }
        $remoteCmd = 'KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"; [ -f "$KUBECONFIG" ] || KUBECONFIG=/etc/rancher/k3s/k3s.yaml; export KUBECONFIG; exec kubectl ' + ($escapedArgs -join ' ')
        $remoteCmd | ssh -o BatchMode=yes -o ConnectTimeout=10 $global:SWCP_CONTROL_SSH "tr -d '\r' | bash"
    } else {
        kubectl @KubectlArgs
    }
}

function Invoke-KubectlCapture {
    $KubectlArgs = @($args)
    if ($global:SWCP_CONTROL_SSH) {
        $escapedArgs = $KubectlArgs | ForEach-Object {
            "'" + ($_ -replace "'", "'\''") + "'"
        }
        $remoteCmd = 'KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"; [ -f "$KUBECONFIG" ] || KUBECONFIG=/etc/rancher/k3s/k3s.yaml; export KUBECONFIG; exec kubectl ' + ($escapedArgs -join ' ')
        $res = ($remoteCmd | ssh -o BatchMode=yes -o ConnectTimeout=10 $global:SWCP_CONTROL_SSH "tr -d '\r' | bash" 2>$null)
        if ($res -is [array]) {
            return ($res -join "`n")
        }
        return $res
    } else {
        return (kubectl @KubectlArgs 2>$null)
    }
}

function Get-RemoteEnvVar {
    param(
        [string]$Key,
        [string]$Fallback = ""
    )
    $val = ""
    if ($global:SWCP_CONTROL_SSH) {
        $cmd = "f=`$HOME/SWCP/.env; [ -f `"`$f`" ] && grep -E '^${Key}=' `"`$f`" | tail -n 1 | cut -d'=' -f2- | cut -d'#' -f1 | tr -d '[:space:]' || true"
        $val = ($cmd | ssh -o BatchMode=yes -o ConnectTimeout=5 $global:SWCP_CONTROL_SSH "tr -d '\r' | bash" 2>$null)
    } else {
        $root = (Resolve-Path "$PSScriptRoot\..").Path
        if (Test-Path "$root\.env") {
            $line = Get-Content "$root\.env" | Where-Object { $_ -match "^$Key\s*=" } | Select-Object -Last 1
            if ($line) {
                $val = ($line -split '=', 2)[1] -replace '#.*$', '' -replace '\s+', ''
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($val)) { return $Fallback }
    return $val.Trim()
}

function Set-RemoteEnvVar {
    param(
        [string]$Key,
        [string]$Value
    )
    if ($Key -notmatch '^[A-Za-z0-9_]+$') {
        Write-Error "Invalid env var key: $Key"
        return
    }
    if ($global:SWCP_CONTROL_SSH) {
        $cmd = "f=`$HOME/SWCP/.env; [ -f `"`$f`" ] || exit 0; if grep -qE '^${Key}=' `"`$f`" ; then sed -i 's|^${Key}=.*|${Key}=${Value}|' `"`$f`"; else printf '\n%s=%s\n' '${Key}' '${Value}' >> `"`$f`"; fi"
        $cmd | ssh -o BatchMode=yes -o ConnectTimeout=5 $global:SWCP_CONTROL_SSH "tr -d '\r' | bash"
    } else {
        $root = (Resolve-Path "$PSScriptRoot\..").Path
        $envPath = "$root\.env"
        if (Test-Path $envPath) {
            $content = Get-Content $envPath
            if ($content -match "^$Key=") {
                $content = $content -replace "^$Key=.*", "$Key=$Value"
            } else {
                $content += "$Key=$Value"
            }
            Set-Content -Path $envPath -Value $content
        }
    }
}

function Start-MonitoringTunnel {
    if (-not $global:SWCP_CONTROL_SSH) {
        $promIp = Invoke-KubectlCapture get svc prometheus -n monitoring -o jsonpath='{.spec.clusterIP}'
        $lokiIp = Invoke-KubectlCapture get svc loki -n monitoring -o jsonpath='{.spec.clusterIP}'
        if ($promIp -and -not $env:PROMETHEUS_URL) { $env:PROMETHEUS_URL = "http://${promIp}:9090" }
        if ($lokiIp -and -not $env:LOKI_URL) { $env:LOKI_URL = "http://${lokiIp}:3100" }
        return
    }

    # Ensure previous tunnel process and stale env variables are cleaned up
    Stop-MonitoringTunnel

    $promIp = (Invoke-KubectlCapture get svc prometheus -n monitoring -o jsonpath='{.spec.clusterIP}')
    $lokiIp = (Invoke-KubectlCapture get svc loki -n monitoring -o jsonpath='{.spec.clusterIP}')

    $forwards = @()
    if ($promIp) {
        $script:LocalPromPort = Get-FreeTcpPort
        $forwards += @("-L", "127.0.0.1:${script:LocalPromPort}:${promIp}:9090")
        $env:PROMETHEUS_URL = "http://127.0.0.1:${script:LocalPromPort}"
    }
    if ($lokiIp) {
        $script:LocalLokiPort = Get-FreeTcpPort
        $forwards += @("-L", "127.0.0.1:${script:LocalLokiPort}:${lokiIp}:3100")
        $env:LOKI_URL = "http://127.0.0.1:${script:LocalLokiPort}"
    }

    if ($forwards.Count -gt 0) {
        Write-Host ">>> Opening out-of-band SSH monitoring tunnel via $global:SWCP_CONTROL_SSH..." -ForegroundColor Cyan
        $sshArgs = @("-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10") + $forwards + @($global:SWCP_CONTROL_SSH)
        $script:SWCP_TUNNEL_PROC = Start-Process -FilePath "ssh" -ArgumentList $sshArgs -PassThru -NoNewWindow
        Start-Sleep -Milliseconds 800
        if ($script:SWCP_TUNNEL_PROC.HasExited) {
            Write-Warning "SSH monitoring tunnel failed to start."
        } else {
            $msg = "[OK] Monitoring tunnel active (PROMETHEUS_URL=$($env:PROMETHEUS_URL), LOKI_URL=$($env:LOKI_URL))"
            Write-Host $msg -ForegroundColor Green
        }
    }
}

function Stop-MonitoringTunnel {
    if ($script:SWCP_TUNNEL_PROC -and -not $script:SWCP_TUNNEL_PROC.HasExited) {
        try {
            Stop-Process -Id $script:SWCP_TUNNEL_PROC.Id -Force -ErrorAction SilentlyContinue
        } catch {}
        $script:SWCP_TUNNEL_PROC = $null
    }
    if ($env:PROMETHEUS_URL -match '127\.0\.0\.1') {
        Remove-Item env:PROMETHEUS_URL -ErrorAction SilentlyContinue
    }
    if ($env:LOKI_URL -match '127\.0\.0\.1') {
        Remove-Item env:LOKI_URL -ErrorAction SilentlyContinue
    }
}

function Measure-InterNodeRtt {
    if ($env:INTER_NODE_RTT_MS) { return }
    if ($global:SWCP_CONTROL_SSH) {
        $cmd = 'source $HOME/SWCP/scripts/lib-benchmark-cluster.sh; measure_inter_node_rtt'
        $out = ($cmd | ssh -o BatchMode=yes -o ConnectTimeout=5 $global:SWCP_CONTROL_SSH "tr -d '\r' | bash" 2>$null)
        if ($out -match 'RTT:\s*([0-9\.]+)ms') {
            $env:INTER_NODE_RTT_MS = $matches[1]
            Write-Host "[OK] Baseline control<->worker RTT: ${env:INTER_NODE_RTT_MS}ms" -ForegroundColor Green
        }
    }
}

function Wait-PipelineDrain {
    param([int]$TimeoutSeconds = 90)
    if ($global:SWCP_CONTROL_SSH) {
        $cmd = "source `$HOME/SWCP/scripts/lib-benchmark-cluster.sh; drain_pipeline $TimeoutSeconds"
        $cmd | ssh -o BatchMode=yes $global:SWCP_CONTROL_SSH "tr -d '\r' | bash"
    } else {
        Write-Host ">>> Checking pipeline drain status..." -ForegroundColor Cyan
        Start-Sleep -Seconds 2
    }
}

function Wait-WorkerBaseline {
    param([int]$TimeoutSeconds = 360)
    if ($global:SWCP_CONTROL_SSH) {
        $cmd = "source `$HOME/SWCP/scripts/lib-benchmark-cluster.sh; wait_for_worker_baseline $TimeoutSeconds"
        $cmd | ssh -o BatchMode=yes $global:SWCP_CONTROL_SSH "tr -d '\r' | bash"
    } else {
        Write-Host ">>> Checking worker baseline..." -ForegroundColor Cyan
        Start-Sleep -Seconds 2
    }
}

function Invoke-BenchmarkCli {
    $CliArgs = @($args)
    $py = "py"
    if (-not (Get-Command "py" -ErrorAction SilentlyContinue)) {
        $py = "python"
    }
    & $py -m benchmarks.cli @CliArgs
}
