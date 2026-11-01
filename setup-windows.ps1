<#
.SYNOPSIS
Prepares and starts the CraftPilot Runtime Agent on Windows through WSL 2.

.PARAMETER Local
Connect to a CraftPilot stack running on this machine instead of the
production master. Needs -CaFile.

.PARAMETER CaFile
Path to the local CraftPilot CA (CraftPilot/runtime-agent/local-certs/ca.crt).

.PARAMETER Force
Continue past environment preflight failures: missing Docker Compose/Buildx,
a Docker socket or bind-mount probe that fails, and a missing local CraftPilot
network. Safety checks (.env symlink, server-data path) are still enforced.
#>
param(
    [switch] $Local,
    [string] $CaFile,
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

function Fail([string] $Message) {
    throw "ERROR: $Message"
}

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    Fail 'Run this script from Windows PowerShell or PowerShell 7 on Windows. Use setup.sh on Linux or setup-macos.sh on macOS.'
}

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
if (-not $Wsl) {
    Fail 'WSL 2 is required for the Windows setup. Install WSL 2 and Docker Desktop with the WSL 2 backend: https://docs.docker.com/desktop/features/wsl/'
}

$Distros = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
if ($LASTEXITCODE -ne 0 -or $Distros -notcontains 'docker-desktop') {
    Fail 'Docker Desktop WSL 2 integration is unavailable. Install/start Docker Desktop, enable the WSL 2 backend, and enable integration for a Linux distribution in Docker Desktop > Settings > Resources > WSL Integration.'
}

$PreferredDistro = if ($Distros -contains 'Ubuntu') { 'Ubuntu' } else { $Distros | Where-Object { $_ -ne 'docker-desktop' } | Select-Object -First 1 }
if (-not $PreferredDistro) {
    Fail 'Install a WSL 2 Linux distribution (Ubuntu is recommended) and enable its Docker Desktop WSL integration.'
}

# Drive management through Docker's integrated WSL CLI, where the socket is real.
$WslDocker = @(& wsl.exe -d $PreferredDistro -- bash -lc 'command -v docker && docker info --format "{{.OSType}}"' 2>&1)
if ($LASTEXITCODE -ne 0 -or ($WslDocker -join "`n") -notmatch '(?m)^linux\s*$') {
    Fail "Docker CLI/daemon is unavailable from WSL distribution '$PreferredDistro'. Enable its Docker Desktop WSL integration and make sure Docker Desktop is running."
}
$ComposeCheck = @(& wsl.exe -d $PreferredDistro -- bash -lc 'docker compose version && docker buildx version' 2>&1)
if ($LASTEXITCODE -ne 0) {
    if ($Force) {
        Write-Warning "Docker Compose v2 or Buildx is missing inside '$PreferredDistro'; continuing because -Force was given. Update Docker Desktop if the build fails."
    } else {
        Fail "Docker Compose v2 and Buildx must be available inside '$PreferredDistro'. Update Docker Desktop and retry."
    }
}

# Local mode: verify the CA here, then reference it through its WSL path.
$WslCaFile = ''
if ($Local) {
    $caPath = if ($CaFile) { $CaFile } else { $env:CRAFTPILOT_LOCAL_CA_FILE }
    if (-not $caPath) {
        Fail 'Local mode needs the local CraftPilot CA: pass -CaFile C:\Projects\CraftPilot\runtime-agent\local-certs\ca.crt (generated in the CraftPilot checkout by runtime-agent/create-local-certs.sh)'
    }
    if (-not (Test-Path -LiteralPath $caPath -PathType Leaf)) {
        Fail "Local CA not found: $caPath. It is generated in the CraftPilot checkout by: bash runtime-agent/create-local-certs.sh"
    }
    $ResolvedCa = (Resolve-Path -LiteralPath $caPath).Path
    $WslCaFile = (@(& wsl.exe -d $PreferredDistro -- wslpath -a ($ResolvedCa -replace '\\', '/')) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or -not $WslCaFile.StartsWith('/')) {
        Fail 'Could not translate the local CA path into the WSL distribution.'
    }
}

# Store the repository and server data in the WSL Linux filesystem, not /mnt/c.
# Docker Desktop supports this path through the integrated WSL distribution and
# Linux permissions work for Minecraft's container UID/GID.
$WslHome = (@(& wsl.exe -d $PreferredDistro -- bash -lc 'printf %s "$HOME"' 2>&1) -join '').Trim()
if ($LASTEXITCODE -ne 0 -or -not $WslHome.StartsWith('/')) {
    Fail "Could not determine the Linux home directory in '$PreferredDistro'."
}
$WslRepo = "$WslHome/craftpilot-runtime-agent"
$WslDataPath = "$WslRepo/server-data"
$WslRepoExists = @(& wsl.exe -d $PreferredDistro -- bash -lc "test -f '$WslRepo/docker-compose.yml'" 2>&1)
if ($LASTEXITCODE -ne 0) {
    $WindowsRepoPath = (& wsl.exe -d $PreferredDistro -- wslpath -a ($RepoRoot -replace '\\', '/'))
    if ($LASTEXITCODE -ne 0 -or -not $WindowsRepoPath) {
        Fail 'Could not translate the Windows repository path into WSL. Copy the repository into the WSL Linux filesystem and retry.'
    }
    $WindowsRepoPath = ($WindowsRepoPath -join '').Trim()
    $CopyOutput = @(& wsl.exe -d $PreferredDistro -- bash -lc "mkdir -p '$WslRepo' && cp -a '$WindowsRepoPath/.' '$WslRepo/'" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Fail "Could not copy the repository into WSL at '$WslRepo'. Details: $($CopyOutput -join ' ')"
    }
    Write-Host "Copied the repository into WSL's Linux filesystem: $WslRepo"
} else {
    # Sync source files on repeat runs, while preserving the WSL .env and server-data.
    $WindowsRepoPath = (& wsl.exe -d $PreferredDistro -- wslpath -a ($RepoRoot -replace '\\', '/'))
    if ($LASTEXITCODE -ne 0 -or -not $WindowsRepoPath) {
        Fail 'Could not translate the Windows repository path into WSL.'
    }
    $WindowsRepoPath = ($WindowsRepoPath -join '').Trim()
    $SyncOutput = @(& wsl.exe -d $PreferredDistro -- bash -lc "tar -C '$WindowsRepoPath' --exclude='./.env' --exclude='./server-data' --exclude='./.git' -cf - . | tar -xf - -C '$WslRepo'" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Fail "Could not synchronize the WSL working copy. Details: $($SyncOutput -join ' ')"
    }
}

# Verify WSL's Docker socket and daemon-side bind mounts before changing .env.
$ProbeOutput = @(& wsl.exe -d $PreferredDistro -- bash -lc "set -eu; test -S /var/run/docker.sock; mkdir -p '$WslDataPath'; docker run --rm --mount 'type=bind,source=$WslDataPath,target=/probe' alpine:3.21 sh -ec 'chown 0:33 /probe && chmod 2770 /probe'; docker run --rm --user 33:33 --mount 'type=bind,source=$WslDataPath,target=/probe' alpine:3.21 sh -ec 'touch /probe/.write-test && rm /probe/.write-test'" 2>&1)
if ($LASTEXITCODE -ne 0) {
    if ($Force) {
        Write-Warning "The Docker daemon or Linux data mount probe failed inside '$PreferredDistro'; continuing because -Force was given. Ensure Docker Desktop WSL integration is enabled if servers cannot start. Details: $($ProbeOutput -join ' ')"
    } else {
        Fail "The Docker daemon or Linux data mount is unavailable from '$PreferredDistro'. Ensure Docker Desktop WSL integration is enabled. Details: $($ProbeOutput -join ' ')"
    }
}

$SetupMode = if ($Local) { 'local' } else { 'deploy' }
$ForceValue = if ($Force) { '1' } else { '0' }
$ModePrelude = "export SETUP_MODE='$SetupMode'`nexport CRAFTPILOT_SETUP_FORCE='$ForceValue'`nexport CRAFTPILOT_LOCAL_CA_FILE_OVERRIDE='$WslCaFile'`n"

$WslCommand = $ModePrelude + @'
set -eu
cd "$HOME/craftpilot-runtime-agent"
[ ! -L .env ] || { echo ".env must not be a symlink." >&2; exit 1; }
[ -f .env ] || { umask 077; cp .env.example .env; }
if [ "$SETUP_MODE" = local ]; then
  [ -n "$CRAFTPILOT_LOCAL_CA_FILE_OVERRIDE" ] || { echo "Local mode needs a CA file." >&2; exit 1; }
  [ -r "$CRAFTPILOT_LOCAL_CA_FILE_OVERRIDE" ] || { echo "Local CA is not readable from WSL: $CRAFTPILOT_LOCAL_CA_FILE_OVERRIDE" >&2; exit 1; }
  MASTER_URL=${CRAFTPILOT_LOCAL_MASTER_URL:-https://runtime-tls}
else
  MASTER_URL=${CRAFTPILOT_DEPLOY_MASTER_URL:-https://craftpilot.se}
fi
export CRAFTPILOT_SERVER_DATA_HOST_PATH="$HOME/craftpilot-runtime-agent/server-data"
export CRAFTPILOT_DOCKER_SERVER_DATA_ROOT="$CRAFTPILOT_SERVER_DATA_HOST_PATH"
export DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
mkdir -p "$CRAFTPILOT_SERVER_DATA_HOST_PATH"
chmod 600 .env
python3 - "$CRAFTPILOT_SERVER_DATA_HOST_PATH" "$CRAFTPILOT_DOCKER_SERVER_DATA_ROOT" "$DOCKER_GID" "$MASTER_URL" "$CRAFTPILOT_LOCAL_CA_FILE_OVERRIDE" <<'PY'
from pathlib import Path
import re
import sys
path = Path('.env')
content = path.read_text(encoding='utf-8')
updates = {
    'CRAFTPILOT_SERVER_DATA_HOST_PATH': sys.argv[1],
    'CRAFTPILOT_DOCKER_SERVER_DATA_ROOT': sys.argv[2],
    'DOCKER_GID': sys.argv[3],
    'CRAFTPILOT_MASTER_URL': sys.argv[4],
    'CRAFTPILOT_LOCAL_CA_FILE': sys.argv[5],
}
for key, value in updates.items():
    line = re.compile(rf'^{re.escape(key)}=.*$', re.MULTILINE)
    replacement = f'{key}={value}'
    if line.search(content):
        content = line.sub(lambda _: replacement, content)
    else:
        content += ('\n' if content and not content.endswith('\n') else '') + replacement + '\n'
path.write_text(content, encoding='utf-8')
path.chmod(0o600)
PY
if [ "$SETUP_MODE" = local ]; then
  docker compose -f docker-compose.yml -f docker-compose.local.yml config --quiet
  if ! docker network inspect "${CRAFTPILOT_MASTER_NETWORK:-craftpilot_default}" >/dev/null 2>&1; then
    if [ "${CRAFTPILOT_SETUP_FORCE:-0}" = 1 ]; then
      echo "WARNING: Local CraftPilot network '${CRAFTPILOT_MASTER_NETWORK:-craftpilot_default}' was not found; continuing because -Force was given. Start the CraftPilot stack first." >&2
    else
      echo "Local CraftPilot network '${CRAFTPILOT_MASTER_NETWORK:-craftpilot_default}' was not found. Start the CraftPilot stack first." >&2
      exit 1
    fi
  fi
  docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
  echo "Agent started against the local CraftPilot stack (approve its enrollment request in the panel):"
else
  docker compose config --quiet
  docker compose up -d --build
  echo "Agent started. Follow logs and approve enrollment in CraftPilot:"
fi
echo "  docker compose logs -f runtime-agent"
'@
$Encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($WslCommand))
$LaunchOutput = @(& wsl.exe -d $PreferredDistro -- bash -lc "echo '$Encoded' | base64 -d | bash" 2>&1)
if ($LASTEXITCODE -ne 0) {
    Fail "Setup failed inside WSL distribution '$PreferredDistro'. Details: $($LaunchOutput -join ' ')"
}
$LaunchOutput | ForEach-Object { Write-Host $_ }
