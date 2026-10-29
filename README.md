# CraftPilot Linux runtime-node agent

The agent is an early runtime slice for remote CraftPilot nodes. It supports authenticated initial server `SNAPSHOT` staging, fixed Minecraft container `START`/`STOP`/`CONSOLE` operations, and sequenced status/resource/log telemetry. It does not synchronize or migrate existing worlds, provide remote file operations, or establish end-to-end connectivity by itself. The node operator must configure firewall access to server ports and ensure the CraftPilot Gate host can reach the node's advertised public address.

## Requirements and security

- A trusted, dedicated Linux host with Docker Engine 19.03 or later, Docker Compose v2 and Docker Buildx 0.17.0 or later. The runtime image includes Python 3.13 and the Docker CLI, so the host does not need Python.
- HTTPS access from the node to the CraftPilot master.
- The local Docker daemon must manage both this agent and the Minecraft containers. The configured server-data host directory must be visible at the same path to the daemon.
- The Docker socket (`/var/run/docker.sock`) gives effectively host-root access. The Compose service mounts it so the agent can create and manage allowlisted server containers; run only on a trusted host and never expose the socket over TCP.
- Configure the host firewall and verify that CraftPilot Gate can connect to each advertised Minecraft port.

## Quick start on a new node

On a trusted Linux node, start the agent and it will request enrollment from CraftPilot. A sysadmin reviews the request and must approve it before the agent receives its node identity and credentials. You no longer need to create a node or copy a node ID/token by hand. The setup script prepares the host-specific Compose configuration and the shared server-data directory. On the Linux node:

```sh
git clone https://github.com/cozilic/craftpilot-runtime-agent.git
cd craftpilot-runtime-agent
sh setup.sh
docker compose up -d --build
docker compose logs -f runtime-agent
```

`sh setup.sh` checks the required host utilities, Docker CLI and daemon, Docker Compose v2, Buildx 0.17.0 or newer, available host memory, Docker daemon memory limit, free space on the configured server-data filesystem, and resolved Compose configuration before preparing the agent. The resource preflight requires at least 1 GiB currently available host RAM, a reported Docker daemon memory limit of at least 1 GiB, and 1 GiB free disk to proceed; it warns below recommended levels of 4 GiB RAM and 10 GiB free disk, and allows setup to continue at or above those minimums. The daemon memory value is read from `docker info --format '{{.MemTotal}}'`, so it accounts for a Docker Desktop VM or configured daemon limit separately from host-wide available RAM. If the daemon does not report a parseable positive limit, setup warns and skips that particular estimate rather than assuming a value. Resource thresholds are estimates—not a guarantee that a particular modpack, player count, or workload will fit. Missing host tools trigger a consent prompt for installation from the supported distribution package manager. If Docker Engine is missing, it identifies `/etc/os-release` and offers installation on Debian, Ubuntu, Fedora, RHEL 8–10, CentOS Stream 9–10, Amazon Linux 2, or Amazon Linux 2023. Missing Docker, Compose, Buildx, and required host-tool installations require interactive approval; non-interactive runs stop with manual setup guidance rather than making system changes. If Compose or a sufficiently recent Buildx is missing, it offers to install pinned official plugin releases and verifies their SHA-256 checksums before installing them under `/usr/local/lib/docker/cli-plugins`. Unsupported distributions get manual-install links instead of guessed repository configuration. Then it copies `.env.example` to a private `.env` file (mode `600`), detects `DOCKER_GID` from `/var/run/docker.sock`, prepares `/srv/craftpilot/servers` with the Minecraft runtime group (default GID 33), and validates the resolved Compose configuration. The Compose file targets the Compose v2 plugin; `docker-compose` v1 is not supported for this build workflow. Existing `.env` settings are preserved except `DOCKER_GID`. Review `.env` if the host uses another server-data path or group. The agent mounts the Docker socket, which grants effectively host-root control; only run it on a trusted host.

The runtime container includes Python 3.13 and the Docker CLI; no host Python installation is needed to run the agent. Setup requires standard shell utilities (including `curl`, `sha256sum`, `stat`, and `install`) and needs either root or working `sudo` access for system package/plugin installation and ownership changes. The Docker Engine package names and install path vary on Amazon Linux, so setup uses AWS's distribution-specific packages there. It installs the official Docker Compose and Buildx CLI plugins only after consent; on other supported distributions it uses Docker's distribution repository for Engine and the same checksum-verified plugins where needed. Running it through `sh` avoids depending on the executable bit being preserved by Git, ZIP downloads, or the host filesystem. It preserves any existing `.env` settings and only updates `DOCKER_GID`. If directory ownership changes require it, the script asks `sudo` for permission; alternatively run it as root. Review `.env` before starting if the host uses a different server-data path or group.

The Compose file builds the current checkout and persists the enrollment request and, after approval, the agent credential and heartbeat sequence in the `agent_state` volume. The agent displays its request ID in logs; Sysadmin → Ops & log → Runtime nodes shows pending requests. Once approved, it fetches its identity automatically and appears online. For the default `/srv/craftpilot/servers` path, both the agent and each server container use that same bind-mounted host directory; `CRAFTPILOT_DOCKER_SERVER_DATA_ROOT` configures the path passed to the Docker daemon. The initial data directory and staged snapshot directories use the configured data GID (default 33) and setgid. Compose gives the agent a `0002` umask so new files created during later work remain group-writable. After enrollment succeeds, the agent saves its long-lived credential with restrictive permissions; protect and back up the `agent_state` volume as a secret. Use only one agent and state volume per node. If state is lost, ask a sysadmin to disable/re-enroll the node rather than copying credentials. Disconnecting a node revokes its credential. A running agent with that saved credential enters an inert waiting state and does not poll or execute commands; a sysadmin must explicitly re-enable the node, issue a fresh registration token, and the operator must set `CRAFTPILOT_REGISTRATION_TOKEN` in `.env` before rebuilding/restarting the agent. An old or missing token never reenables the disabled node.

To install an updated agent revision, pull the desired repository branch or tag and run `docker compose up -d --build`. Check compatibility with the CraftPilot master before upgrading.

## Configuration

The tracked `.env.example` supplies safe defaults, including `CRAFTPILOT_MASTER_URL=https://craftpilot.se`. `./setup.sh` fills in the required host-specific `DOCKER_GID`; no enrollment token or node ID is required for sysadmin-approved self-enrollment. `CRAFTPILOT_SERVER_DATA_HOST_PATH` defaults to `/srv/craftpilot/servers` and must be an absolute dedicated directory visible to the local Docker daemon at that same host path.

Optional agent variables (set in `.env` if needed):

- `CRAFTPILOT_REGISTRATION_TOKEN`: one-time registration token for an explicitly re-enabled node; leave empty during normal self-enrollment and remove it after registration succeeds.
- `CRAFTPILOT_SERVER_DATA_GID`: group ID used for shared server-data directories; default `33` (matches CraftPilot's default container user `33:33`).
- `CRAFTPILOT_DOCKER_NETWORK`: game-container bridge network; default `craftpilot-servers`.
- `CRAFTPILOT_ALLOWED_IMAGES`: comma-separated exact image allowlist. Defaults to Temurin JRE 8, 11, 17, 21 and 25 images.
- `CRAFTPILOT_POLL_SECONDS`: polling interval 5–120; default 30.
- `CRAFTPILOT_STOP_GRACE_SECONDS`: graceful stop timeout 0–45; default 30.
- `CRAFTPILOT_LOG_LEVEL`: standard Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`); default `INFO`.

The agent writes timestamped structured lifecycle logs to stderr, available with `docker compose logs -f runtime-agent`. At the default `INFO` level it records command IDs/types and server IDs, snapshot staging, container creation/start, console command delivery, graceful/forced stop requests, stop completion, status transitions, and command failures. Console command text, credentials, and RCON passwords are never logged. Set `CRAFTPILOT_LOG_LEVEL=DEBUG` for Python diagnostic detail; `DEBUG` does not currently add payload or secret logging.

If you prefer to install Docker manually, follow the official Docker Engine instructions for your distribution at https://docs.docker.com/engine/install/; on Amazon Linux, use AWS's guide at https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-docker.html. Compose v2 is installed separately; see https://docs.docker.com/compose/install/linux/. Verify with `docker --version`, `docker info`, `docker compose version`, and `docker buildx version`. Docker Engine must be at least 19.03 and Buildx at least 0.17.0 for builds. The legacy standalone `docker-compose` command is not supported for this build workflow; use the Compose v2 plugin (`docker compose`). No host Python installation is required to run the agent: its container uses Python 3.13 and includes the Docker CLI. The test command does require Python 3.13+ on the machine running it.

For self-enrollment, no registration token is required. The agent creates a unique, expiring request secret, stores it with private permissions, and polls over HTTPS until the sysadmin approves or denies it. Approval creates an identity and credential for this single node; the agent saves that credential before acknowledging approval. Keep `.env` private and never commit it. The Compose build uses the code in the clone; publishing a GHCR image is a separate release step and is not required for this setup. To stop the agent, use `docker compose down`; this preserves its named state volume and server data. Do not run `docker compose down -v` unless you intend to delete the saved agent credentials/state.

## Runtime and safety boundaries

- Only fixed `snapshot`, `start`, `stop` and `console` operations are supported; console commands are sent over authenticated RCON, never interpolated into a host-shell command.
- For console support, the agent persists a random per-server RCON password in its private state volume, updates `server.properties` atomically, and connects only to the container's IPv4 address on the configured isolated Docker network. RCON is not published to the host; the agent is attached to that private network. Protect the state volume as a secret because it contains both the node credential and RCON passwords.
- After claiming/acknowledging a command, the agent reports each matching managed container's state, Docker CPU/memory usage, uptime, server-data disk usage and at most 100 timestamped log lines. For each assigned server with a required startup JAR, the master supplies its basename and minimum accepted size; the agent checks that file under the server data root and reports `artifact_present`. Log cursors are kept in agent state and advance only after the master accepts the report. Docker stats/log collection failures leave status reporting available and do not stop the polling loop. The master rejects stale/replayed reports, treats stale node/report pairs as unavailable, broadcasts accepted console/player changes and keeps bounded log history on disk.
- RCON and telemetry have been covered by Docker-free agent and master feature tests. Test actual command delivery, server log output, restart-persistent RCON credentials and firewall/proxy reachability on a disposable Linux Docker node before production use.
- Containers are named from server UUIDs and are reused/stopped only if CraftPilot ownership labels match. Images must exactly match the local allowlist; resource limits, port, user, network, mount point and data paths are validated.
- Initial snapshots are downloaded only from the configured HTTPS master origin with the node credential; byte count and SHA-256 are verified. ZIP traversal, symlinks, special files, and excessive decompression are rejected. Files are extracted to a temporary directory and installed only into a missing or empty destination; populated destinations are never overwritten.
- Docker socket access remains privileged. Restrict host access, enrollment credentials, token file, and agent state accordingly. HTTPS redirects are not followed and certificate verification is not disabled.

## Tests and staging

Run the shell preflight tests from this repo root:

```sh
sh test_setup_resources.sh
```

Run Docker-free agent tests (requires Python 3.13+ on the test machine):

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

Before production use, test registration and credential persistence, heartbeat replay rejection, START/STOP, lease expiry/retry, revoked-credential waiting and authorized re-enrollment, node outage, firewalling and actual Minecraft connectivity on a disposable Linux Docker host and staging master. A successful container start alone does not prove that players can connect.
