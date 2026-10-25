# CraftPilot Linux runtime-node agent

The agent is an early runtime slice for remote CraftPilot nodes. It supports authenticated initial server `SNAPSHOT` staging and fixed Minecraft container `START`/`STOP` operations. It does not synchronize or migrate existing worlds, relay logs/console/stats, provide remote file operations, or establish end-to-end connectivity by itself. The node operator must configure firewall access to server ports and ensure the CraftPilot Gate host can reach the node's advertised public address.

## Requirements and security

- A trusted, dedicated Linux host with Docker Engine and Docker Compose v2.
- HTTPS access from the node to the CraftPilot master.
- The local Docker daemon must manage both this agent and the Minecraft containers. The configured server-data host directory must be visible at the same path to the daemon.
- The Docker socket (`/var/run/docker.sock`) gives effectively host-root access. The Compose service mounts it so the agent can create and manage allowlisted server containers; run only on a trusted host and never expose the socket over TCP.
- Configure the host firewall and verify that CraftPilot Gate can connect to each advertised Minecraft port.

## Quick start on a new node

On a trusted Linux node, start the agent and it will request enrollment from CraftPilot. A sysadmin reviews the request and must approve it before the agent receives its node identity and credentials. You no longer need to create a node or copy a node ID/token by hand. The host still needs local Docker and filesystem settings. On the Linux node:

```sh
git clone https://github.com/cozilic/craftpilot-runtime-agent.git
cd craftpilot-runtime-agent
```

Set the host-specific `DOCKER_GID` in `.env` before Compose starts. Find the socket group ID with `stat -c '%g' /var/run/docker.sock`. Prepare the host data directory with group 33 (the default Minecraft runtime group); Compose adds that group to the agent and staged snapshots are set group-writable for Minecraft. Protect `.env` with `chmod 600 .env`, then start from the cloned repo:

```sh
docker compose up -d --build
docker compose logs -f runtime-agent
```

The Compose file builds the current checkout and persists the enrollment request and, after approval, the agent credential and heartbeat sequence in the `agent_state` volume. The agent displays its request ID in logs; Sysadmin → Ops & log → Runtime nodes shows pending requests. Once approved, it fetches its identity automatically and appears online. For the default `/srv/craftpilot/servers` path, both the agent and each server container use that same bind-mounted host directory; `CRAFTPILOT_DOCKER_SERVER_DATA_ROOT` configures the path passed to the Docker daemon. The initial data directory and staged snapshot directories use the configured data GID (default 33) and setgid. Compose gives the agent a `0002` umask so new files created during later work remain group-writable. After enrollment succeeds, the agent saves its long-lived credential with restrictive permissions; protect and back up the `agent_state` volume as a secret. Use only one agent and state volume per node. If state is lost, ask a sysadmin to disable/re-enroll the node rather than copying credentials.

To install an updated agent revision, pull the desired repository branch or tag and run `docker compose up -d --build`. Check compatibility with the CraftPilot master before upgrading.

## Configuration

Required Compose variables:

- `CRAFTPILOT_MASTER_URL`: HTTPS origin only, e.g. `https://craftpilot.se` (no API path).
- `DOCKER_GID`: numeric group ID of `/var/run/docker.sock` on the host.

`CRAFTPILOT_NODE_ID` and `CRAFTPILOT_REGISTRATION_TOKEN` are optional legacy/manual enrollment settings. Leave them unset for sysadmin-approved self-enrollment.
- `CRAFTPILOT_SERVER_DATA_HOST_PATH`: absolute persistent host path, also visible to the Docker daemon when it creates game containers.

Optional agent variables (set in `.env` if needed):

- `CRAFTPILOT_SERVER_DATA_GID`: group ID used for shared server-data directories; default `33` (matches CraftPilot's default container user `33:33`).
- `CRAFTPILOT_DOCKER_NETWORK`: game-container bridge network; default `craftpilot-servers`.
- `CRAFTPILOT_ALLOWED_IMAGES`: comma-separated exact image allowlist. Defaults to Temurin JRE 8, 11, 17, 21 and 25 images.
- `CRAFTPILOT_POLL_SECONDS`: polling interval 5–120; default 30.
- `CRAFTPILOT_STOP_GRACE_SECONDS`: graceful stop timeout 0–45; default 30.

For self-enrollment, no registration token is required. The agent creates a unique, expiring request secret, stores it with private permissions, and polls over HTTPS until the sysadmin approves or denies it. Approval creates an identity and credential for this single node; the agent saves that credential before acknowledging approval. Keep `.env` private and never commit it. The Compose build uses the code in the clone; publishing a GHCR image is a separate release step and is not required for this setup.

## Runtime and safety boundaries

- Only fixed `snapshot`, `start` and `stop` operations are supported; no arbitrary host-shell command is exposed.
- Containers are named from server UUIDs and are reused/stopped only if CraftPilot ownership labels match. Images must exactly match the local allowlist; resource limits, port, user, network, mount point and data paths are validated.
- Initial snapshots are downloaded only from the configured HTTPS master origin with the node credential; byte count and SHA-256 are verified. ZIP traversal, symlinks, special files, and excessive decompression are rejected. Files are extracted to a temporary directory and installed only into a missing or empty destination; populated destinations are never overwritten.
- Docker socket access remains privileged. Restrict host access, enrollment credentials, token file, and agent state accordingly. HTTPS redirects are not followed and certificate verification is not disabled.

## Tests and staging

Run Docker-free checks from this repo root:

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

Before production use, test registration and credential persistence, heartbeat replay rejection, START/STOP, lease expiry/retry, revoked credentials, node outage, firewalling and actual Minecraft connectivity on a disposable Linux Docker host and staging master. A successful container start alone does not prove that players can connect.
