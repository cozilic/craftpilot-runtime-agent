#!/usr/bin/env sh
# Invoke portably with `sh setup.sh`; Git checkouts on some hosts omit +x.
set -eu
umask 077

require_commands() {
  missing=
  for required_command do
    if ! command -v "$required_command" >/dev/null 2>&1; then
      missing="$missing $required_command"
    fi
  done
  if [ -n "$missing" ]; then
    echo "Missing host utilities:$missing" >&2
    return 1
  fi
}

ask_to_install() {
  message=$1
  if [ ! -t 0 ]; then
    echo "$message" >&2
    echo "Re-run setup interactively to approve, or install the dependency manually." >&2
    return 1
  fi
  printf '%s [y/N] ' "$message"
  IFS= read -r answer || answer=
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

if ! command -v id >/dev/null 2>&1 || ! command -v uname >/dev/null 2>&1; then
  echo "The id and uname host utilities are required to identify the current user and Linux architecture; install them manually, then rerun setup.sh." >&2
  exit 1
fi

if ! command -v pwd >/dev/null 2>&1; then
  echo "pwd is required to locate the setup files; install it manually, then rerun setup.sh." >&2
  exit 1
fi
case "$0" in
  */*) SCRIPT_DIR=${0%/*}; [ -n "$SCRIPT_DIR" ] || SCRIPT_DIR=/ ;;
  *) SCRIPT_DIR=. ;;
esac
SCRIPT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR" && pwd)
cd "$SCRIPT_DIR"
. "$SCRIPT_DIR/host_resource_checks.sh"

if [ ! -r /etc/os-release ]; then
  echo "This setup script supports Linux distributions that provide /etc/os-release." >&2
  exit 1
fi
. /etc/os-release

DOCKER_ENGINE_MIN_VERSION=19.03.0
BUILDX_MIN_VERSION=0.17.0
BUILDX_PINNED_VERSION=0.38.1
COMPOSE_PINNED_VERSION=2.40.3

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

install_base_utilities() {
  case "$ID" in
    debian|ubuntu)
      require_commands apt-get || { echo "apt-get is required to install missing host utilities." >&2; return 1; }
      run_privileged apt-get update
      run_privileged apt-get install -y ca-certificates coreutils curl grep sed
      ;;
    fedora|rhel|centos|amzn)
      if command -v dnf >/dev/null 2>&1; then package_manager=dnf
      elif command -v yum >/dev/null 2>&1; then package_manager=yum
      else echo "Neither dnf nor yum is available to install missing host utilities." >&2; return 1
      fi
      run_privileged "$package_manager" install -y ca-certificates coreutils curl grep sed
      ;;
    *)
      echo "Automatic utility installation is unsupported for '$ID'; use the distribution's package manager." >&2
      exit 1
      ;;
  esac
}

if ! require_commands sed grep tail stat cp mv chmod chgrp mkdir mktemp cat rm install sha256sum curl tee env; then
  ask_to_install "Required host utilities are missing. Install coreutils, grep, sed and curl?" || {
    echo "Install the missing host utilities manually, then rerun setup.sh." >&2
    exit 1
  }
  install_base_utilities
  if ! require_commands sed grep tail stat cp mv chmod chgrp mkdir mktemp cat rm install sha256sum curl tee env; then
    echo "The package-manager installation did not provide all required host utilities." >&2
    exit 1
  fi
fi
if [ "$(uname -s)" != "Linux" ]; then
  echo "This setup script supports Linux hosts with Docker Engine." >&2
  exit 1
fi

version_at_least() {
  current_version=$1
  required_version=$2
  saved_ifs=$IFS
  IFS=.
  set -- $current_version
  current_major=${1:-0}
  current_minor=${2:-0}
  current_patch=${3:-0}
  set -- $required_version
  required_major=${1:-0}
  required_minor=${2:-0}
  required_patch=${3:-0}
  IFS=$saved_ifs

  if [ "$current_major" -gt "$required_major" ]; then return 0; fi
  if [ "$current_major" -lt "$required_major" ]; then return 1; fi
  if [ "$current_minor" -gt "$required_minor" ]; then return 0; fi
  if [ "$current_minor" -lt "$required_minor" ]; then return 1; fi
  [ "$current_patch" -ge "$required_patch" ]
}

ensure_download_tools() {
  if require_commands curl sha256sum install mktemp; then
    return 0
  fi
  case "$ID" in
    debian|ubuntu)
      require_commands apt-get || return 1
      run_privileged apt-get update
      run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates coreutils curl
      ;;
    fedora|rhel|centos|amzn)
      if command -v dnf >/dev/null 2>&1; then package_manager=dnf
      elif command -v yum >/dev/null 2>&1; then package_manager=yum
      else echo "Neither dnf nor yum is available to install checksum tools." >&2; return 1
      fi
      run_privileged "$package_manager" install -y ca-certificates coreutils curl
      ;;
    *)
      echo "curl, sha256sum, install, and mktemp are required to verify Docker CLI plugins." >&2
      return 1
      ;;
  esac
  require_commands curl sha256sum install mktemp || {
    echo "The package-manager installation did not provide the required download/checksum utilities." >&2
    return 1
  }
}

install_compose_plugin() {
  ask_to_install "Docker Compose plugin is missing. Install the pinned official Compose v$COMPOSE_PINNED_VERSION plugin?" || {
    echo "Install Compose manually: https://docs.docker.com/compose/install/linux/" >&2
    return 1
  }
  ensure_download_tools
  case "$(uname -m)" in
    x86_64|amd64)
      compose_arch=x86_64
      compose_digest=dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372
      ;;
    aarch64|arm64)
      compose_arch=aarch64
      compose_digest=d26373b19e89160546d15407516cc59f453030d9bc5b43ba7faf16f7b4980137
      ;;
    *) echo "Unsupported CPU architecture for the pinned Compose plugin: $(uname -m)" >&2; return 1 ;;
  esac
  compose_tmp=$(mktemp)
  compose_url="https://github.com/docker/compose/releases/download/v$COMPOSE_PINNED_VERSION/docker-compose-linux-$compose_arch"
  if ! curl -fsSL "$compose_url" -o "$compose_tmp"; then
    rm -f "$compose_tmp"
    echo "Could not download the official Compose plugin." >&2
    return 1
  fi
  if ! printf '%s  %s\n' "$compose_digest" "$compose_tmp" | sha256sum -c -; then
    rm -f "$compose_tmp"
    echo "Compose plugin checksum verification failed; refusing to install it." >&2
    return 1
  fi
  plugin_dir=/usr/local/lib/docker/cli-plugins
  run_privileged install -m 0755 -d "$plugin_dir"
  run_privileged install -m 0755 "$compose_tmp" "$plugin_dir/docker-compose"
  rm -f "$compose_tmp"
  if ! docker compose version >/dev/null 2>&1; then
    echo 'Compose plugin was installed but docker compose did not become available.' >&2
    return 1
  fi
}

buildx_current_version() {
  buildx_output=$(docker buildx version 2>/dev/null) || return 1
  printf '%s\n' "$buildx_output" | sed -n 's/.*[vV]\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*/\1/p'
}

install_buildx_plugin() {
  ask_to_install "Docker Buildx $BUILDX_MIN_VERSION or newer is required; install the pinned official Buildx v$BUILDX_PINNED_VERSION plugin?" || {
    echo "Install/update Buildx from Docker's package repository or https://github.com/docker/buildx/releases." >&2
    return 1
  }
  ensure_download_tools
  case "$(uname -m)" in
    x86_64|amd64)
      buildx_arch=amd64
      buildx_digest=e25d5cd675092bb5d1d03632fdc46cdf3fede7b514dc5f023a105e00d3ce5ad4
      ;;
    aarch64|arm64)
      buildx_arch=arm64
      buildx_digest=3b83b3731916ba433feccaaa689fe5bcdc81c641d92c8e6c2ea865dfa141c97a
      ;;
    *) echo "Unsupported CPU architecture for the pinned Buildx plugin: $(uname -m)" >&2; return 1 ;;
  esac
  buildx_tmp=$(mktemp)
  buildx_url="https://github.com/docker/buildx/releases/download/v$BUILDX_PINNED_VERSION/buildx-v$BUILDX_PINNED_VERSION.linux-$buildx_arch"
  if ! curl -fsSL "$buildx_url" -o "$buildx_tmp"; then
    rm -f "$buildx_tmp"
    echo "Could not download the official Buildx plugin." >&2
    return 1
  fi
  if ! printf '%s  %s\n' "$buildx_digest" "$buildx_tmp" | sha256sum -c -; then
    rm -f "$buildx_tmp"
    echo "Buildx plugin checksum verification failed; refusing to install it." >&2
    return 1
  fi
  plugin_dir=/usr/local/lib/docker/cli-plugins
  run_privileged install -m 0755 -d "$plugin_dir"
  run_privileged install -m 0755 "$buildx_tmp" "$plugin_dir/docker-buildx"
  rm -f "$buildx_tmp"
  buildx_version=$(buildx_current_version || true)
  if [ -z "$buildx_version" ] || ! version_at_least "$buildx_version" "$BUILDX_MIN_VERSION"; then
    echo "Buildx is still below the required version after installation; inspect Docker CLI plugin discovery." >&2
    return 1
  fi
  echo "Verified Docker Buildx $buildx_version."
}

install_docker_stack() {
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

  ensure_download_tools
  case "$ID" in
    debian|ubuntu)
      require_commands apt-get dpkg tee || {
        echo "APT, dpkg, and tee are required for automatic Docker installation on $ID." >&2
        exit 1
      }
      if [ "$docker_present" != true ] && ! command -v systemctl >/dev/null 2>&1; then
        echo "systemctl is required to enable/start Docker automatically on $ID; install Docker and start its service manually." >&2
        exit 1
      fi
      ;;
    amzn)
      require_commands yum service || {
        echo "yum and the service command are required for automatic Docker installation on Amazon Linux." >&2
        exit 1
      }
      if [ "$docker_present" != true ] && [ "${VERSION_ID%%.*}" = 2 ] && ! command -v amazon-linux-extras >/dev/null 2>&1; then
        echo "Amazon Linux 2 requires amazon-linux-extras to install Docker; use AWS's manual instructions if it is unavailable." >&2
        exit 1
      fi
      ;;
    fedora|rhel|centos)
      if ! command -v dnf >/dev/null 2>&1 && ! command -v yum >/dev/null 2>&1; then
        echo "Neither dnf nor yum is available; install Docker manually using https://docs.docker.com/engine/install/." >&2
        exit 1
      fi
      if [ "$docker_present" != true ] && ! command -v systemctl >/dev/null 2>&1; then
        echo "systemctl is required to enable/start Docker automatically on $ID; install Docker and start its service manually." >&2
        exit 1
      fi
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
        run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin docker-buildx-plugin
      else
        run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y \
          docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      fi
      ;;
    amzn)
      if [ "$docker_present" = true ]; then
        echo "Compose and Buildx are not provided by the AWS Docker packages on this host; setup will install the checksum-verified plugins separately."
      fi
      case "${VERSION_ID%%.*}" in
        2)
          run_privileged yum update -y
          run_privileged amazon-linux-extras install docker
          ;;
        2023)
          run_privileged yum update -y
          run_privileged yum install -y docker
          ;;
        *) echo "Automatic Docker installation is not configured for Amazon Linux $VERSION_ID." >&2; exit 1 ;;
      esac
      if ! command -v service >/dev/null 2>&1; then
        echo "Docker was installed, but the service command is unavailable; start the docker service manually." >&2
        exit 1
      fi
      run_privileged service docker start
      if [ "$docker_present" != true ] && command -v usermod >/dev/null 2>&1; then
        current_user=${SUDO_USER:-$(id -un)}
        printf "Add %s to the docker group so it can access the daemon without sudo? This grants root-equivalent privileges. [y/N] " "$current_user"
        IFS= read -r add_to_docker_group || add_to_docker_group=
        case "$add_to_docker_group" in
          y|Y|yes|YES)
            run_privileged usermod -aG docker "$current_user"
            echo "Log out and back in before rerunning setup so the new docker-group membership takes effect."
            exit 0
            ;;
        esac
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
        run_privileged "$package_manager" install -y docker-compose-plugin docker-buildx-plugin
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
      echo "Automatic Docker installation is not configured for '$ID' (${NAME:-unknown}). Supported: Debian, Ubuntu, Fedora, RHEL 8-10, CentOS Stream 9-10, Amazon Linux 2 and 2023." >&2
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

if [ "$(uname -s)" != "Linux" ]; then
  echo "This setup script supports Linux hosts with Docker Engine." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  install_docker_stack
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker installation did not provide the docker CLI; check the package-manager output." >&2
  exit 1
fi
if [ ! -S /var/run/docker.sock ] || ! docker info >/dev/null 2>&1; then
  ask_to_install "Docker daemon is not reachable. Start the local Docker service now?" || {
    echo "Start Docker manually (`sudo service docker start` on Amazon Linux, or `sudo systemctl start docker` on systemd hosts) and ensure this user can access its socket." >&2
    exit 1
  }
  if [ "$ID" = amzn ]; then
    service_command=service
  elif command -v systemctl >/dev/null 2>&1; then
    service_command=systemctl
  elif command -v service >/dev/null 2>&1; then
    service_command=service
  else
    echo "No supported service manager was found. Start Docker manually, then rerun setup.sh." >&2
    exit 1
  fi
  if [ "$service_command" = systemctl ]; then
    if ! run_privileged systemctl start docker; then
      echo "Could not start the Docker service. Check the system service logs and rerun setup.sh." >&2
      exit 1
    fi
  else
    if ! run_privileged service docker start; then
      echo "Could not start the Docker service. Check the system service logs and rerun setup.sh." >&2
      exit 1
    fi
  fi
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is still unreachable by this user. Check service status and permissions for /var/run/docker.sock, then rerun setup.sh." >&2
  exit 1
fi
docker_memory_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || true)
docker_server_version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)
docker_server_version=${docker_server_version%%-*}
docker_server_version=${docker_server_version%%+*}
if [ -z "$docker_server_version" ] || ! version_at_least "$docker_server_version" "$DOCKER_ENGINE_MIN_VERSION"; then
  echo "Docker Engine $DOCKER_ENGINE_MIN_VERSION or newer is required for Buildx builds; detected '${docker_server_version:-unavailable}'. Upgrade Docker Engine and rerun setup.sh." >&2
  exit 1
fi
buildx_version=$(buildx_current_version || true)
if [ -z "$buildx_version" ] || ! version_at_least "$buildx_version" "$BUILDX_MIN_VERSION"; then
  install_buildx_plugin
fi
buildx_version=$(buildx_current_version || true)
if [ -z "$buildx_version" ] || ! version_at_least "$buildx_version" "$BUILDX_MIN_VERSION"; then
  echo "Docker Buildx $BUILDX_MIN_VERSION or newer is required; detected '${buildx_version:-missing}'." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  install_compose_plugin
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is unavailable. Install the plugin from https://docs.docker.com/compose/install/linux/." >&2
  exit 1
fi
COMPOSE='docker compose'
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

if ! check_host_resources /proc/meminfo "$SERVER_DATA_PATH" 4096 1024 10240 1024 "$docker_memory_bytes"; then
  echo "Host resource preflight failed. Free memory or disk space before setting up a Minecraft runtime node." >&2
  exit 1
fi

$COMPOSE config --quiet
echo "Setup complete. Configuration is in .env (mode 600)."
echo "Server data: $SERVER_DATA_PATH (group $DATA_GID, setgid)."
echo "Start the agent with: $COMPOSE up -d --build"
echo "Verified Docker Engine, Compose $(docker compose version --short 2>/dev/null || docker compose version), and Buildx $buildx_version (minimum $BUILDX_MIN_VERSION)."
echo "Review the pending enrollment in CraftPilot before it receives node credentials."
