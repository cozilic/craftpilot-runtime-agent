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
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine and Docker Compose v2 (docker compose) are required." >&2
  exit 1
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

docker compose config --quiet
echo "Setup complete. Configuration is in .env (mode 600)."
echo "Server data: $SERVER_DATA_PATH (group $DATA_GID, setgid)."
echo "Start the agent with: docker compose up -d --build"
echo "Review the pending enrollment in CraftPilot before it receives node credentials."
