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
    Fail "Docker Compose v2 and Buildx must be available inside '$PreferredDistro'. Update Docker Desktop and retry."
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
    Fail "The Docker daemon or Linux data mount is unavailable from '$PreferredDistro'. Ensure Docker Desktop WSL integration is enabled. Details: $($ProbeOutput -join ' ')"
}

$WslCommand = @'
set -eu
cd "$HOME/craftpilot-runtime-agent"
[ ! -L .env ] || { echo ".env must not be a symlink." >&2; exit 1; }
[ -f .env ] || { umask 077; cp .env.example .env; }
export CRAFTPILOT_SERVER_DATA_HOST_PATH="$HOME/craftpilot-runtime-agent/server-data"
export CRAFTPILOT_DOCKER_SERVER_DATA_ROOT="$CRAFTPILOT_SERVER_DATA_HOST_PATH"
export DOCKER_GID="$(stat -c %g /var/run/docker.sock)"
mkdir -p "$CRAFTPILOT_SERVER_DATA_HOST_PATH"
chmod 600 .env
python3 - "$CRAFTPILOT_SERVER_DATA_HOST_PATH" "$CRAFTPILOT_DOCKER_SERVER_DATA_ROOT" "$DOCKER_GID" <<'PY'
from pathlib import Path
import re
import sys
path = Path('.env')
content = path.read_text(encoding='utf-8')
updates = {
    'CRAFTPILOT_SERVER_DATA_HOST_PATH': sys.argv[1],
    'CRAFTPILOT_DOCKER_SERVER_DATA_ROOT': sys.argv[2],
    'DOCKER_GID': sys.argv[3],
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
docker compose config --quiet
docker compose up -d --build
echo "Agent started. Follow logs and approve enrollment in CraftPilot:"
echo "  docker compose logs -f runtime-agent"
'@
$Encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($WslCommand))
$LaunchOutput = @(& wsl.exe -d $PreferredDistro -- bash -lc "echo '$Encoded' | base64 -d | bash" 2>&1)
if ($LASTEXITCODE -ne 0) {
    Fail "Setup failed inside WSL distribution '$PreferredDistro'. Details: $($LaunchOutput -join ' ')"
}
$LaunchOutput | ForEach-Object { Write-Host $_ }
