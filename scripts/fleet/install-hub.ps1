# install-hub.ps1 — register the zmem hub on this Windows box (issue #291).
#
# Contract (Workstream T PR 12, AC3):
#   * the hub runs as a service bound to 127.0.0.1 ONLY (never a wildcard);
#   * the tailnet exposes it via `tailscale serve` HTTPS (no wildcard bind);
#   * a principal map with a broad ACE (Users/Everyone/Authenticated Users)
#     is REFUSED before anything is configured;
#   * idempotent: re-running replaces the task and re-points serve.
#
# Usage:
#   .\install-hub.ps1 -RepoRoot C:\path\to\zmem -DataDir D:\zmem-data `
#       -TokensFile D:\zmem-data\principal-map.json -Port 8765
param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$TokensFile,
    [int]$Port = 8765,
    [string]$TaskName = "zmem-hub"
)

$ErrorActionPreference = "Stop"

# --- refuse a world-readable principal map ---------------------------------
if (-not (Test-Path $TokensFile)) {
    Write-Error "principal map not found: $TokensFile (create it with issue-principal.py first)"
}
$acl = (Get-Acl $TokensFile).Access |
    Where-Object { $_.AccessControlType -eq "Allow" } |
    Where-Object {
        $_.IdentityReference -match "^(BUILTIN\\Users|Everyone|BUILTIN\\Authenticated Users|NT AUTHORITY\\Authenticated Users)$"
    }
if ($acl) {
    Write-Error "REFUSED: $TokensFile grants a broad allow ACE (Users/Everyone/Authenticated Users). Restrict it to the hub service account first."
}

# --- data dir ---------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# --- the service: startup scheduled task bound to 127.0.0.1 -----------------
$serverPy = Join-Path $RepoRoot "hermes-plugin\server\mcp_server.py"
if (-not (Test-Path $serverPy)) { Write-Error "mcp_server.py not found under $RepoRoot" }

$action = New-ScheduledTaskAction -Execute "python" `
    -Argument "-X utf8 `"$serverPy`" --host 127.0.0.1 --port $Port"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

# --- tailscale serve: tailnet HTTPS -> localhost ONLY -----------------------
$serve = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $serve) {
    Write-Warning "tailscale not found — install it, then run: tailscale serve --bg --https=443 http://127.0.0.1:$Port"
} else {
    tailscale serve --bg --https=443 "http://127.0.0.1:$Port"
    if ($LASTEXITCODE -ne 0) { Write-Error "tailscale serve failed (exit $LASTEXITCODE)" }
    tailscale serve status
}

Write-Host ""
Write-Host "Hub installed. Broker machines connect to this box's tailnet name."
Write-Host "Issue one principal per broker with python scripts/fleet/issue-principal.py,"
Write-Host "then run scripts/fleet/install-broker.ps1 on each broker machine."
