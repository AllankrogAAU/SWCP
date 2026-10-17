param(
    [string]$Distro = 'Debian'
)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$env:WSL_UTF8 = '1'

$installed = wsl.exe -l -q
if (-not ($installed | ForEach-Object { $_.Trim() } | Where-Object { $_ -eq $Distro })) {
    throw "WSL distro '$Distro' not found. Install it (e.g. 'wsl --install -d Debian') and enable Docker Desktop WSL Integration for it, then re-run."
}

$wslRoot = (wsl.exe -d $Distro -- wslpath -a $root.Replace('\', '/')).Trim()
if ([string]::IsNullOrWhiteSpace($wslRoot)) {
    throw "Could not resolve '$root' to a WSL path inside distro '$Distro'."
}

Write-Host "Running scripts/deploy-local-k8s-wsl.sh inside WSL distro '$Distro' as root..."
& wsl.exe -d $Distro -u root -- bash "$wslRoot/scripts/deploy-local-k8s-wsl.sh"
if ($LASTEXITCODE -ne 0) {
    throw "deploy-local-k8s-wsl.sh failed with exit code $LASTEXITCODE"
}

Write-Host ''
Write-Host 'Add this line to C:\Windows\System32\drivers\etc\hosts (as Administrator) if not already present:'
Write-Host '  127.0.0.1 swcp.local'
Write-Host 'Then open http://swcp.local in your browser.'
