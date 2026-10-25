# install-broker.ps1 — point this Windows box at the fleet hub (issue #291).
#
# Contract (Workstream T PR 12): the broker machine gets a PER-BROKER
# credential — a singular ZMEM_MCP_TOKEN_FILE the CLIENT transport reads.
# The hub's principal MAP never leaves the hub. The installer:
#   * writes the token file with an owner-only DACL hardened BEFORE content;
#   * persists ZMEM_REMOTE_URL (full /mcp URL) + ZMEM_MCP_TOKEN_FILE at
#     MACHINE scope (a relogin/restart is required for harnesses to see it);
#   * refuses conflicting effective config in ANY scope: ZMEM_HERMES_MODE
#     values other than unset/mcp, a differing ZMEM_MCP_URL, any plural
#     ZMEM_MCP_TOKENS_FILE, or any ZMEM_STORE (config error with the URL);
#   * validates with a FRESH child process whose env is RECONSTRUCTED from
#     the persisted machine values and asserts the transport resolves MCP;
#   * exits nonzero when `doctor --fleet` reports any fail check.
#
# Usage (token is piped via stdin, never an argument):
#   Get-Content token.txt | .\install-broker.ps1 -RepoRoot C:\path\to\zmem `
#       -HubUrl https://vsan-1.<tailnet>.ts.net/mcp -Namespaces project:myrepo
param(
    [Parameter(Mandatory = $true)][string]$RepoRoot,
    [Parameter(Mandatory = $true)][string]$HubUrl,
    [string]$Namespaces = "",
    [string]$TokenDir = "$env:ProgramData\zmem"
)

$ErrorActionPreference = "Stop"

if ($HubUrl -notmatch "/mcp$") {
    Write-Error "-HubUrl must be the full MCP endpoint ending in /mcp (got: $HubUrl)"
}

# --- refuse conflicting effective config (machine + user + process) --------
function Get-Effective([string]$Name) {
    $m = [Environment]::GetEnvironmentVariable($Name, "Machine")
    $u = [Environment]::GetEnvironmentVariable($Name, "User")
    $p = [Get-Item] "Env:$Name" -ErrorAction SilentlyContinue
    if ($p) { return $p.Value }
    if ($u) { return $u }
    if ($m) { return $m }
    return $null
}
$mode = Get-Effective "ZMEM_HERMES_MODE"
if ($mode -and $mode -ne "mcp") {
    Write-Error "REFUSED: effective ZMEM_HERMES_MODE='$mode' outranks both URL variables and would bypass the hub (only unset or 'mcp' is allowed)"
}
$mcpUrl = Get-Effective "ZMEM_MCP_URL"
if ($mcpUrl -and $mcpUrl -ne $HubUrl) {
    Write-Error "REFUSED: effective ZMEM_MCP_URL='$mcpUrl' outranks ZMEM_REMOTE_URL and differs from -HubUrl"
}
if (Get-Effective "ZMEM_MCP_TOKENS_FILE") {
    Write-Error "REFUSED: a ZMEM_MCP_TOKENS_FILE (the hub-side principal map) is set on a broker machine — unset it; the hub map never leaves the hub"
}
if (Get-Effective "ZMEM_STORE") {
    Write-Error "REFUSED: an effective ZMEM_STORE is set — ZMEM_STORE together with ZMEM_REMOTE_URL is a configuration error (docs/FLEET.md)"
}

# --- token file: owner-only DACL BEFORE content -----------------------------
$token = [Console]::In.ReadToEnd()
$token = $token.Trim()
if (-not $token) { Write-Error "no token piped on stdin" }
New-Item -ItemType Directory -Force -Path $TokenDir | Out-Null
$tokenFile = Join-Path $TokenDir "broker-token.json"
if (Test-Path $tokenFile) { Remove-Item $tokenFile -Force }
New-Item -ItemType File -Path $tokenFile | Out-Null
$user = $env:USERNAME
icacls $tokenFile /inheritance:r /grant:r "${user}:F" | Out-Null
$payload = @{ token = $token }
if ($Namespaces) { $payload["namespaces"] = $Namespaces -split "," }
($payload | ConvertTo-Json -Compress) | Set-Content -NoNewline -Path $tokenFile

# --- persist machine-scope config (relogin/restart required) ----------------
[Environment]::SetEnvironmentVariable("ZMEM_REMOTE_URL", $HubUrl, "Machine")
[Environment]::SetEnvironmentVariable("ZMEM_MCP_TOKEN_FILE", $tokenFile, "Machine")
[Environment]::SetEnvironmentVariable("ZMEM_MCP_TOKEN", $null, "Machine")
if (Get-Effective "ZMEM_MCP_TOKEN") {
    Remove-Item "Env:ZMEM_MCP_TOKEN" -ErrorAction SilentlyContinue
    [Environment]::SetEnvironmentVariable("ZMEM_MCP_TOKEN", $null, "User")
}

# --- ZMEM_DATA must be a writable directory (the sidecar home) --------------
$dataDir = Get-Effective "ZMEM_DATA"
if ($dataDir) {
    if (Test-Path $dataDir -PathType Leaf) {
        Write-Error "REFUSED: effective ZMEM_DATA ('$dataDir') names a FILE — set it to a writable directory"
    }
} else {
    $dataDir = Join-Path $TokenDir "data"
    [Environment]::SetEnvironmentVariable("ZMEM_DATA", $dataDir, "Machine")
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
}

# --- validate: FRESH child with the RECONSTRUCTED effective config ----------
$doctorPy = Join-Path $RepoRoot "skills\memory\scripts\doctor.py"
$probe = @"
import json, os, subprocess, sys
env = dict(os.environ)
env["ZMEM_REMOTE_URL"] = sys.argv[2]
env["ZMEM_MCP_TOKEN_FILE"] = sys.argv[3]
env.pop("ZMEM_MCP_TOKEN", None)
env.pop("ZMEM_MCP_TOKENS_FILE", None)
scripts = sys.argv[1]
sys.path.insert(0, scripts)
from fleet import transport
mode = transport.resolve_transport_mode(env)
if mode is not transport.TransportMode.MCP:
    print("transport mode resolved to " + str(mode) + ", not MCP", file=sys.stderr)
    sys.exit(10)
rc = subprocess.run([sys.executable, "-X", "utf8", sys.argv[4], "--fleet",
                     "--format", "json"], env=env, capture_output=True,
                    text=True).returncode
sys.exit(rc)
"@
$probeFile = Join-Path $env:TEMP "zmem-broker-validate.py"
Set-Content -Path $probeFile -Value $probe
$scriptsDir = Join-Path $RepoRoot "skills\memory\scripts"
& python $probeFile $scriptsDir $HubUrl $tokenFile $doctorPy
if ($LASTEXITCODE -ne 0) {
    Write-Error "broker validation failed (exit $LASTEXITCODE) — fix the reported checks before harnesses rely on this box"
}
Remove-Item $probeFile -Force

Write-Host ""
Write-Host "Broker installed. Machine-scope env requires a relogin/restart before"
Write-Host "harnesses inherit it. Residual: user/process-scope overrides set later"
Write-Host "can still shadow the machine values (documented in docs/FLEET.md)."
