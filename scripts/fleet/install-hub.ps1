# install-hub.ps1 - register the zmem hub on this Windows box (issue #291).
#
# Contract (Workstream T PR 12, AC3):
#   * the hub runs as a scheduled task bound to 127.0.0.1 ONLY (never a
#     wildcard bind), started at boot as the MAP OWNER via S4U logon (no
#     password, fires without an interactive logon, and the identity can
#     READ the issuer-hardened principal map);
#   * the task action is a generated wrapper that sets the hub env
#     (principal map + data dir) before launching python - the raw task
#     action cannot carry environment variables;
#   * a principal map with a broad ACE (Users/Everyone/Authenticated
#     Users, matched by well-known SID so localized Windows cannot defeat
#     it) is REFUSED before anything is configured;
#   * idempotent: re-running replaces the wrapper, the task, and re-points
#     serve, and RESTARTS the task so the running hub converges onto the
#     new configuration.
#
# Usage:
#   .\install-hub.ps1 -RepoRoot C:\path\to\zmem -DataDir D:\zmem-data `
#       -TokensFile D:\zmem-data\principal-map.json -Port 8765
param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$DataDir,
    [Parameter(Mandatory = $true)][string]$TokensFile,
    [int]$Port = 8765,
    [int]$HttpsPort = 443,
    [string]$TaskName = "zmem-hub"
)

$ErrorActionPreference = "Stop"

if ($Port -lt 1 -or $Port -gt 65535) {
    Write-Error "-Port must be 1-65535 (got: $Port)"
}
if ($HttpsPort -lt 1 -or $HttpsPort -gt 65535) {
    Write-Error "-HttpsPort must be 1-65535 (got: $HttpsPort)"
}

# --- refuse a broad-ACL principal map (SID-based, locale-independent) -------
if (-not (Test-Path $TokensFile)) {
    Write-Error "principal map not found: $TokensFile (create it with issue-principal.py first)"
}
$wellKnownSids = @("S-1-1-0", "S-1-5-32-545", "S-1-5-11")
$broad = (Get-Acl $TokensFile).Access |
    Where-Object { $_.AccessControlType -eq "Allow" } |
    Where-Object {
        $sid = $null
        try {
            $sid = $_.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]).Value
        } catch {
            $sid = $_.IdentityReference.Value
        }
        $wellKnownSids -contains $sid
    }
if ($broad) {
    Write-Error "REFUSED: $TokensFile grants a broad allow ACE (Everyone / Users / Authenticated Users - matched by SID). Restrict it to the hub service account first."
}

# --- data dir + wrapper script (the task action cannot carry env) -----------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$serverPy = Join-Path $RepoRoot "hermes-plugin\server\mcp_server.py"
if (-not (Test-Path $serverPy)) { Write-Error "mcp_server.py not found under $RepoRoot" }
$pythonExe = (Get-Command python).Source
if (-not $pythonExe) { Write-Error "python not found on PATH" }

$wrapper = Join-Path $DataDir "hub-run.cmd"
@"
@echo off
set "ZMEM_MCP_TOKENS_FILE=$TokensFile"
set "ZMEM_DATA=$DataDir"
"$pythonExe" -X utf8 "$serverPy" --host 127.0.0.1 --port $Port
"@ | Set-Content -Path $wrapper -Encoding ASCII

# --- the service: startup scheduled task as the map owner (S4U) --------------
$action = New-ScheduledTaskAction -Execute "$wrapper"
$trigger = New-ScheduledTaskTrigger -AtStartup
# Run the task as the ACL OWNER of the principal map with S4U
# logon: no
# password prompt, fires without an interactive logon, and the identity
# can READ the issuer-hardened principal map (issue-principal strips
# inheritance and grants only the issuer - a SYSTEM principal could not
# read it and the hub would exit 2 at startup; review round 2).
$owner = (Get-Acl $TokensFile).Owner
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType S4U
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -ne "Running") {
    Write-Error "REFUSED: hub task state is '$state' (expected Running) - not configuring tailscale serve against a hub that is not up (fix the task/event-log first)"
}

# --- tailscale serve: tailnet HTTPS -> localhost ONLY -----------------------
# Issue #423: the serve HTTPS port is a parameter, and an occupied target
# port is REFUSED before re-pointing serve -- configuring a handler over a
# live non-zmem backend would silently break that service, and the
# documented idempotent re-point makes it worse on re-runs.
$serve = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $serve) {
    Write-Warning "tailscale not found - install it, then run: tailscale serve --bg --https=$HttpsPort http://127.0.0.1:$Port"
} else {
    # Review round 1: detection is LINE-scoped and PORT-aware. Tailscale
    # prints TCP-forward trees and the Funnel header BEFORE web entries, so
    # an anchored whole-blob match no-ops on exactly the busy hosts this
    # guard exists for; and a handler on another port is not a :443
    # occupier. An occupier line is a web handler whose host carries no
    # explicit port or :443 (tailscale suppresses the default) or a
    # TLS-terminated TCP forward on 443. A web handler that already proxies
    # OUR localhost port is the idempotent re-run and never an occupier.
    $status = tailscale serve status 2>$null | Out-String
    $lines = $status -split "`r?`n"
    $occupier = @($lines | Where-Object {
        $_ -match '^https?://[^/: ]+(:443)?(\s|$)' -or
        $_ -match '^\|-- tcp://[^ ]+:443 '
    }).Count -gt 0
    $ours = @($lines | Where-Object {
        $_ -match ("proxy http://127\.0\.0\.1:" + $Port + "(\D|$)")
    }).Count -gt 0
    if ($HttpsPort -eq 443 -and $occupier -and -not $ours) {
        Write-Error ("REFUSED: tailscale serve already has a handler on " +
            ":443 (an operator service is live there). Re-run with an " +
            "explicit -HttpsPort <n> to publish the hub on another " +
            "port instead of replacing it. Current serve status:`n$status")
    }
    tailscale serve --bg --https=$HttpsPort "http://127.0.0.1:$Port"
    if ($LASTEXITCODE -ne 0) { Write-Error "tailscale serve failed (exit $LASTEXITCODE)" }
    tailscale serve status
}

Write-Host ""
Write-Host "Hub installed (task $TaskName; wrapper $wrapper)."
Write-Host "Issue one principal per broker with python scripts/fleet/issue-principal.py,"
Write-Host "then run scripts/fleet/install-broker.ps1 on each broker machine."
