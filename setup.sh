#!/usr/bin/env sh
# Invoke portably with `sh setup.sh`; Git checkouts on some hosts omit +x.
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ "$(uname -s)" != "Linux" ]; then
  echo "This setup script supports Linux hosts with Docker Engine." >&2
  exit 1
fi
run_privileged() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "Administrative permissions are required. Run setup.sh as root or install/configure sudo, then retry." >&2
    exit 1
  fi
}

install_docker_stack() {
  if [ ! -f /etc/os-release ]; then
    echo "Cannot identify this Linux distribution (/etc/os-release missing). Install Docker Engine and Compose manually." >&2
    exit 1
  fi

  # /etc/os-release is the operating system's trusted distribution metadata.
  . /etc/os-release
  # This agent needs the local daemon socket, not just a Docker CLI binary.
  docker_present=false
  if command -v docker >/dev/null 2>&1 && [ -S /var/run/docker.sock ]; then
    docker_present=true
  fi

  if [ -t 0 ]; then
    printf "Docker Engine or Compose is missing. Install Docker's official Engine and Compose packages for this host? [y/N] "
    IFS= read -r answer || answer=
  else
    answer=
  fi
  case "$answer" in
    y|Y|yes|YES) ;;
    *)
      echo "No system packages were installed. Re-run interactively to approve installation, or follow https://docs.docker.com/engine/install/ and https://docs.docker.com/compose/install/linux/." >&2
      exit 1
      ;;
  esac

  case "$ID" in
    debian|ubuntu)
      if [ "$ID" = debian ]; then
        docker_repo="https://download.docker.com/linux/debian"
        suite=${VERSION_CODENAME:-}
      else
        docker_repo="https://download.docker.com/linux/ubuntu"
        suite=${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}
      fi
      if [ -z "$suite" ] || ! command -v dpkg >/dev/null 2>&1; then
        echo "Could not determine the APT suite/architecture for $ID. Install Docker manually using the official distribution guide." >&2
        exit 1
      fi
      architecture=$(dpkg --print-architecture)
      case "$suite$architecture" in
        *[!A-Za-z0-9._-]*) echo "Unsafe or unsupported APT suite/architecture detected; refusing repository setup." >&2; exit 1 ;;
      esac
      run_privileged apt-get update
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
      run_privileged install -m 0755 -d /etc/apt/keyrings
      key_path=/etc/apt/keyrings/docker.asc
      run_privileged sh -c 'curl -fsSL "$1/gpg" -o "$2" && chmod a+r "$2"' sh "$docker_repo" "$key_path"
      printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: %s\n' \
        "$docker_repo" "$suite" "$architecture" "$key_path" \
        | run_privileged tee /etc/apt/sources.list.d/docker.sources >/dev/null
      run_privileged apt-get update
      if [ "$docker_present" = true ]; then
        run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin
      else
        run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y \
          docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      fi
      ;;
    fedora|rhel|centos)
      case "$ID" in
        fedora) docker_repo="https://download.docker.com/linux/fedora/docker-ce.repo" ;;
        rhel)
          major_version=${VERSION_ID%%.*}
          case "$major_version" in 8|9|10) ;; *) echo "RHEL $VERSION_ID is outside the supported versions in Docker's current guide." >&2; exit 1 ;; esac
          docker_repo="https://download.docker.com/linux/rhel/docker-ce.repo"
          ;;
        centos)
          case "${NAME:-}" in *Stream*) ;; *) echo "Docker's CentOS repository is intended for CentOS Stream; refusing to guess a compatible derivative." >&2; exit 1 ;; esac
          major_version=${VERSION_ID%%.*}
          case "$major_version" in 9|10) ;; *) echo "CentOS Stream $VERSION_ID is outside the supported versions in Docker's current guide." >&2; exit 1 ;; esac
          docker_repo="https://download.docker.com/linux/centos/docker-ce.repo"
          ;;
      esac
      if command -v dnf >/dev/null 2>&1; then
        package_manager=dnf
      elif command -v yum >/dev/null 2>&1; then
        package_manager=yum
      else
        echo "Neither dnf nor yum is available; install Docker manually using https://docs.docker.com/engine/install/." >&2
        exit 1
      fi
      run_privileged "$package_manager" install -y curl
      repo_tmp=$(mktemp)
      if ! curl -fsSL "$docker_repo" -o "$repo_tmp"; then
        rm -f "$repo_tmp"
        echo "Could not download Docker's repository configuration from $docker_repo." >&2
        exit 1
      fi
      run_privileged install -m 0644 "$repo_tmp" /etc/yum.repos.d/docker-ce.repo
      rm -f "$repo_tmp"
      if [ "$docker_present" = true ]; then
        run_privileged "$package_manager" install -y docker-compose-plugin
      else
        run_privileged "$package_manager" install -y \
          docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        if ! command -v systemctl >/dev/null 2>&1; then
          echo "Docker packages installed, but systemctl is unavailable; start the Docker service manually." >&2
          exit 1
        fi
        run_privileged systemctl enable --now docker
      fi
      ;;
    *)
      echo "Automatic Docker installation is not configured for '$ID' (${NAME:-unknown}). Supported: Debian, Ubuntu, Fedora, RHEL 8-10, CentOS Stream 9-10." >&2
      echo "No repository was added. Follow https://docs.docker.com/engine/install/ for this distribution, then rerun setup.sh." >&2
      exit 1
      ;;
  esac

  if { [ "$ID" = debian ] || [ "$ID" = ubuntu ]; } && [ "$docker_present" != true ]; then
    if ! command -v systemctl >/dev/null 2>&1; then
      echo "Docker packages installed, but systemctl is unavailable; start the Docker service manually." >&2
      exit 1
    fi
    run_privileged systemctl enable --now docker
  fi
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE='docker compose'
elif command -v docker >/dev/null 2>&1 && command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
  COMPOSE=docker-compose
else
  install_docker_stack
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker installation did not provide the docker CLI; check the package-manager output." >&2
    exit 1
  fi
  if docker compose version >/dev/null 2>&1; then
    COMPOSE='docker compose'
  elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    COMPOSE=docker-compose
  else
    echo "Compose installation was not detected. Verify Docker installation, then rerun setup.sh." >&2
    exit 1
  fi
fi
if [ ! -S /var/run/docker.sock ]; then
  echo "Docker socket /var/run/docker.sock was not found. Is Docker running?" >&2
  exit 1
fi
if ! command -v stat >/dev/null 2>&1; then
  echo "stat is required to detect the Docker socket group." >&2
  exit 1
fi

DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)
case "$DOCKER_GID" in
  ''|*[!0-9]*) echo "Could not determine a numeric Docker socket GID." >&2; exit 1 ;;
esac

if [ -L .env ]; then
  echo ".env must not be a symbolic link; refusing to overwrite its target." >&2
  exit 1
fi
if [ -e .env ] && [ ! -f .env ]; then
  echo ".env exists but is not a regular file." >&2
  exit 1
fi
if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    echo "Missing .env.example; cannot initialize configuration." >&2
    exit 1
  fi
  cp .env.example .env
  echo "Created .env from .env.example."
fi

ENV_TMP=$(mktemp .env.tmp.XXXXXX)
trap 'rm -f "$ENV_TMP"' EXIT HUP INT TERM
if grep -q '^DOCKER_GID=' .env; then
  sed "s/^DOCKER_GID=.*/DOCKER_GID=$DOCKER_GID/" .env > "$ENV_TMP"
else
  cat .env > "$ENV_TMP"
  printf '\nDOCKER_GID=%s\n' "$DOCKER_GID" >> "$ENV_TMP"
fi
chmod 600 "$ENV_TMP"
mv -f -- "$ENV_TMP" .env
chmod 600 .env

SERVER_DATA_PATH=$(sed -n 's/^CRAFTPILOT_SERVER_DATA_HOST_PATH=//p' .env | tail -n 1)
SERVER_DATA_PATH=${SERVER_DATA_PATH:-/srv/craftpilot/servers}
case "$SERVER_DATA_PATH" in
  *\"*|*\'*)
    echo "Use an unquoted path in CRAFTPILOT_SERVER_DATA_HOST_PATH." >&2
    exit 1
    ;;
esac
case "$SERVER_DATA_PATH" in
  /*) ;;
  *) echo "CRAFTPILOT_SERVER_DATA_HOST_PATH must be an absolute Linux path (got: $SERVER_DATA_PATH)." >&2; exit 1 ;;
esac
case "$SERVER_DATA_PATH" in
  *//*|*/../*|*/./*|*/..|*/.)
    echo "CRAFTPILOT_SERVER_DATA_HOST_PATH must not contain empty, '.' or '..' path components." >&2
    exit 1
    ;;
esac
case "$SERVER_DATA_PATH" in
  /|/srv|/home|/var|/etc|/usr|/opt|/tmp|/root|/run|/mnt|/media)
    echo "Refusing to change permissions on a broad system directory: $SERVER_DATA_PATH" >&2
    echo "Choose a dedicated subdirectory, e.g. /srv/craftpilot/servers." >&2
    exit 1
    ;;
esac
CHECK_PATH=
PATH_PARTS=${SERVER_DATA_PATH#/}
while [ -n "$PATH_PARTS" ]; do
  PATH_COMPONENT=${PATH_PARTS%%/*}
  if [ "$PATH_COMPONENT" = "$PATH_PARTS" ]; then
    PATH_PARTS=
  else
    PATH_PARTS=${PATH_PARTS#*/}
  fi
  CHECK_PATH="$CHECK_PATH/$PATH_COMPONENT"
  if [ -L "$CHECK_PATH" ]; then
    echo "The configured server-data path must not traverse a symlink: $CHECK_PATH" >&2
    exit 1
  fi
done

DATA_GID=$(sed -n 's/^CRAFTPILOT_SERVER_DATA_GID=//p' .env | tail -n 1)
DATA_GID=${DATA_GID:-33}
case "$DATA_GID" in
  ''|*[!0-9]*) echo "CRAFTPILOT_SERVER_DATA_GID must be numeric." >&2; exit 1 ;;
esac

if ! mkdir -p -- "$SERVER_DATA_PATH" 2>/dev/null; then
  if command -v sudo >/dev/null 2>&1; then
    sudo mkdir -p -- "$SERVER_DATA_PATH"
  else
    echo "Could not create $SERVER_DATA_PATH. Run setup as root or install sudo." >&2
    exit 1
  fi
fi
if ! chgrp "$DATA_GID" "$SERVER_DATA_PATH" 2>/dev/null; then
  if command -v sudo >/dev/null 2>&1; then
    sudo chgrp "$DATA_GID" "$SERVER_DATA_PATH"
  else
    echo "Could not set group $DATA_GID on $SERVER_DATA_PATH. Run setup as root or install sudo." >&2
    exit 1
  fi
fi
if ! chmod 2770 "$SERVER_DATA_PATH" 2>/dev/null; then
  if command -v sudo >/dev/null 2>&1; then
    sudo chmod 2770 "$SERVER_DATA_PATH"
  else
    echo "Could not set permissions on $SERVER_DATA_PATH. Run setup as root or install sudo." >&2
    exit 1
  fi
fi

$COMPOSE config --quiet
echo "Setup complete. Configuration is in .env (mode 600)."
echo "Server data: $SERVER_DATA_PATH (group $DATA_GID, setgid)."
echo "Start the agent with: $COMPOSE up -d --build"
echo "Review the pending enrollment in CraftPilot before it receives node credentials."
