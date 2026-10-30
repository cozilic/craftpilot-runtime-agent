#!/usr/bin/env sh
# Prepare and start CraftPilot Runtime Agent on macOS with Docker Desktop.
set -eu
umask 077

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[ "$(uname -s)" = Darwin ] || fail "Run this script from macOS. Use setup.sh on Linux or setup-windows.ps1 on Windows."
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"
command -v docker >/dev/null 2>&1 || fail "Docker CLI not found. Install Docker Desktop for Mac: https://docs.docker.com/desktop/setup/install/mac-install/"
docker info >/dev/null 2>&1 || fail "Docker Desktop is not running. Start it and enable Linux containers."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required; update Docker Desktop."
docker buildx version >/dev/null 2>&1 || fail "Docker Buildx is required; update Docker Desktop."

[ ! -L .env ] || fail ".env must not be a symbolic link; refusing to overwrite its target."
[ ! -e .env ] || [ -f .env ] || fail ".env exists but is not a regular file."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example."
fi

DEFAULT_DATA_PATH=/srv/craftpilot/servers
DATA_PATH=$(sed -n 's/^CRAFTPILOT_SERVER_DATA_HOST_PATH=//p' .env | tail -n 1)
[ -n "$DATA_PATH" ] && [ "$DATA_PATH" != "$DEFAULT_DATA_PATH" ] || DATA_PATH="$HOME/CraftPilot/runtime-agent/server-data"
DOCKER_SOCKET_PATH=$(sed -n 's/^CRAFTPILOT_DOCKER_SOCKET_PATH=//p' .env | tail -n 1)
[ -n "$DOCKER_SOCKET_PATH" ] || DOCKER_SOCKET_PATH=/var/run/docker.sock
case "$DATA_PATH$DOCKER_SOCKET_PATH" in
  *\"*|*"'"*|*\\*|*'$'*|*'#'*|*,*) fail "Configured paths must not contain quotes, backslashes, dollar signs, #, or commas." ;;
esac
case "$DATA_PATH" in /*) ;; *) fail "CRAFTPILOT_SERVER_DATA_HOST_PATH must be absolute." ;; esac
case "$DATA_PATH" in /|/System|/Users|/Volumes|/private|/var|/etc|/opt|/tmp) fail "Choose a dedicated server-data subdirectory, not '$DATA_PATH'." ;; esac
case "$DOCKER_SOCKET_PATH" in /*) ;; *) fail "CRAFTPILOT_DOCKER_SOCKET_PATH must be an absolute macOS path." ;; esac
[ -S "$DOCKER_SOCKET_PATH" ] || fail "Docker socket '$DOCKER_SOCKET_PATH' is unavailable. Enable Docker Desktop's default Unix socket setting, then retry."
mkdir -p "$DATA_PATH"

PROBE_DIR=$(mktemp -d "$DATA_PATH/.craftpilot-bind-check.XXXXXX") || fail "Could not create a bind-mount probe under '$DATA_PATH'."
PROBE_NAME=${PROBE_DIR##*/}
PROBE_TOKEN=$(date +%s)-$$
PROBE_CONTAINER=craftpilot-path-check-$$
ENV_TMP=
printf '%s\n' "$PROBE_TOKEN" > "$PROBE_DIR/token"
cleanup() {
  [ -z "$PROBE_CONTAINER" ] || docker rm -f "$PROBE_CONTAINER" >/dev/null 2>&1 || true
  [ -z "$PROBE_DIR" ] || rm -rf "$PROBE_DIR"
  [ -z "$ENV_TMP" ] || rm -f "$ENV_TMP"
}
trap cleanup EXIT HUP INT TERM

# Discover Docker Desktop's daemon-side path for this host directory.
if ! docker create --name "$PROBE_CONTAINER" --mount "type=bind,source=$DATA_PATH,target=/probe" alpine:3.21 sleep 60 >/dev/null 2>&1; then
  fail "Docker Desktop cannot share '$DATA_PATH'. Add it in Docker Desktop > Settings > Resources > File Sharing."
fi
DOCKER_DATA_ROOT=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/probe"}}{{.Source}}{{end}}{{end}}' "$PROBE_CONTAINER" 2>/dev/null || true)
[ -n "$DOCKER_DATA_ROOT" ] || fail "Could not determine the Docker daemon path for '$DATA_PATH'."
docker rm -f "$PROBE_CONTAINER" >/dev/null 2>&1
PROBE_CONTAINER=

# Confirm both the selected socket and sibling-container view of the data mount.
if ! docker run --rm --group-add 0 \
  --mount "type=bind,source=$DOCKER_SOCKET_PATH,target=/var/run/docker.sock" \
  docker:cli docker info >/dev/null 2>&1; then
  fail "Docker is not accessible through '$DOCKER_SOCKET_PATH'. Enable Docker Desktop's default Unix socket setting, then retry: https://docs.docker.com/desktop/setup/install/mac-install/"
fi
VISIBLE_TOKEN=$(docker run --rm --group-add 0 \
  --mount "type=bind,source=$DOCKER_SOCKET_PATH,target=/var/run/docker.sock" \
  --entrypoint docker docker:cli run --rm \
  --mount "type=bind,src=$DOCKER_DATA_ROOT/$PROBE_NAME,dst=/probe,readonly" \
  --entrypoint cat alpine:3.21 /probe/token 2>/dev/null || true)
if [ "$VISIBLE_TOKEN" != "$PROBE_TOKEN" ]; then
  fail "Docker Desktop cannot see the same data directory at '$DOCKER_DATA_ROOT/$PROBE_NAME'. Check its file-sharing settings and retry."
fi
rm -rf "$PROBE_DIR"
PROBE_DIR=

ENV_TMP=$(mktemp .env.tmp.XXXXXX)
awk -v gid=0 -v data="$DATA_PATH" -v docker_data="$DOCKER_DATA_ROOT" -v socket="$DOCKER_SOCKET_PATH" '
  BEGIN { seen_gid=0; seen_data=0; seen_docker_data=0; seen_socket=0 }
  /^DOCKER_GID=/ { if (!seen_gid++) print "DOCKER_GID=" gid; next }
  /^CRAFTPILOT_SERVER_DATA_HOST_PATH=/ { if (!seen_data++) print "CRAFTPILOT_SERVER_DATA_HOST_PATH=" data; next }
  /^CRAFTPILOT_DOCKER_SERVER_DATA_ROOT=/ { if (!seen_docker_data++) print "CRAFTPILOT_DOCKER_SERVER_DATA_ROOT=" docker_data; next }
  /^CRAFTPILOT_DOCKER_SOCKET_PATH=/ { if (!seen_socket++) print "CRAFTPILOT_DOCKER_SOCKET_PATH=" socket; next }
  { print }
  END {
    if (!seen_gid) print "DOCKER_GID=" gid
    if (!seen_data) print "CRAFTPILOT_SERVER_DATA_HOST_PATH=" data
    if (!seen_docker_data) print "CRAFTPILOT_DOCKER_SERVER_DATA_ROOT=" docker_data
    if (!seen_socket) print "CRAFTPILOT_DOCKER_SOCKET_PATH=" socket
  }
' .env > "$ENV_TMP"
chmod 600 "$ENV_TMP"
mv -f "$ENV_TMP" .env
ENV_TMP=
chmod 600 .env

docker compose config --quiet || fail "Docker Compose configuration is invalid. Review .env."
echo "Building and starting CraftPilot Runtime Agent on macOS."
echo "Server data: $DATA_PATH"
echo "Docker daemon server-data path: $DOCKER_DATA_ROOT"
docker compose up -d --build
echo "Agent started. Follow logs and approve enrollment in CraftPilot:"
echo "  docker compose logs -f runtime-agent"
