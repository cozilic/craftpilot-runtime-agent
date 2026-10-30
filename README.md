<p align="center">
  <img src="assets/craftpilot-runtime-agent-logo.png" alt="CraftPilot Runtime Agent" width="720">
</p>

# CraftPilot Runtime Agent

The CraftPilot Runtime Agent connects a trusted Linux host to a CraftPilot master so Minecraft servers can run on infrastructure you control. It communicates with the master over HTTPS and manages Minecraft containers through the host’s Docker daemon.

> **Security warning:** The agent mounts the Docker socket, which grants effectively host-root control. Run it only on a trusted, dedicated host. Never expose the Docker socket over TCP, and protect the agent’s state volume and configuration as secrets.

## What it supports

The current agent supports a focused set of operations:

- **Initial snapshot staging** from the configured CraftPilot master.
- **Start and stop** for managed Minecraft containers.
- **Console command delivery** over authenticated RCON.
- **Node and server telemetry**, including container status, resource usage, uptime, disk usage, and a bounded log stream.

The agent does **not** currently migrate existing worlds or provide remote file operations. A running container does not by itself guarantee player connectivity: the node operator must configure the firewall and ensure the CraftPilot Gate can reach the node’s advertised Minecraft address and ports.

## Requirements

Install the agent on a trusted, dedicated Linux host with:

- Docker Engine 19.03 or later.
- Docker Compose v2.
- Docker Buildx 0.17.0 or later.
- HTTPS access to the CraftPilot master.

The runtime container includes Python 3.13 and the Docker CLI; Python does not need to be installed on the host. The Docker daemon must manage both the agent and Minecraft containers, and the configured server-data directory must be visible to that daemon at the same host path.

The setup preflight requires at least 1 GiB of currently available host RAM, a reported Docker daemon memory limit of at least 1 GiB, and 1 GiB of free space on the server-data filesystem. It warns below the recommended levels of 4 GiB RAM and 10 GiB free disk. These are estimates, not guarantees that a particular modpack or workload will fit.

## Install and start

On the Linux host, clone the repository and run the setup script:

```sh
git clone https://github.com/cozilic/craftpilot-runtime-agent.git
cd craftpilot-runtime-agent
sh setup.sh
docker compose up -d --build
docker compose logs -f runtime-agent
```

`setup.sh` checks the host tools, Docker daemon, Compose, Buildx, available resources, and resolved Compose configuration. It prepares a private `.env`, detects the Docker socket’s group ID, and creates the shared server-data directory. Review the generated `.env` before starting the service.

Setup may offer to install missing packages or Docker plugins and may request elevated permissions for installation or directory ownership changes. Review each prompt; missing system tools are not installed without approval. The supported automatic Docker Engine installation targets Debian, Ubuntu, Fedora, RHEL 8–10, CentOS Stream 9–10, Amazon Linux 2, and Amazon Linux 2023. Other distributions receive manual installation guidance.

The build workflow requires the Compose v2 plugin (`docker compose`); the legacy standalone `docker-compose` v1 command is not supported.

## Enroll the node

Enrollment is initiated by the agent; you do not need to create a node or copy a node ID and token manually during normal setup.

1. Start the agent. It creates a unique, expiring enrollment request and polls the CraftPilot master over HTTPS.
2. A sysadmin reviews and approves or denies the request in **Ops & log → Runtime nodes**. The request ID is available in the agent logs.
3. After approval, the agent stores its node credential with restrictive permissions and acknowledges the enrollment.
4. The node appears in CraftPilot and can receive supported server operations.

Use one agent and one state volume per node. The named `agent_state` volume stores the enrollment state, long-lived node credential, heartbeat sequence, and per-server RCON passwords. Protect and back up this volume as a secret.

If the state volume is lost, ask a sysadmin to disable and re-enroll the node; do not copy credentials from another node. A disabled node’s saved credential cannot resume polling or execute commands. Re-enabling requires an explicit sysadmin action and a fresh registration token in `.env`.

## Configuration

The checked-in `.env.example` provides safe defaults, including `CRAFTPILOT_MASTER_URL=https://craftpilot.se`. Setup fills in the host-specific `DOCKER_GID`. No registration token is needed for initial self-enrollment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CRAFTPILOT_MASTER_URL` | `https://craftpilot.se` | CraftPilot master origin. |
| `CRAFTPILOT_SERVER_DATA_HOST_PATH` | `/srv/craftpilot/servers` | Absolute, dedicated server-data path on the Docker host. The daemon must see it at this same path. |
| `DOCKER_GID` | Set by `setup.sh` | Group ID for access to the Docker socket. |
| `CRAFTPILOT_REGISTRATION_TOKEN` | Empty | One-time token used only when an explicitly disabled node is re-enabled. Remove it after registration succeeds. |
| `CRAFTPILOT_SERVER_DATA_GID` | `33` | Group used for shared server-data directories; matches the default Minecraft container user (`33:33`). |
| `CRAFTPILOT_DOCKER_NETWORK` | `craftpilot-servers` | Isolated bridge network for game containers. |
| `CRAFTPILOT_ALLOWED_IMAGES` | Temurin JRE 8, 11, 17, 21 and 25 images | Comma-separated exact image allowlist. |
| `CRAFTPILOT_POLL_SECONDS` | `30` | Polling interval; allowed range is 5–120 seconds. |
| `CRAFTPILOT_STOP_GRACE_SECONDS` | `30` | Graceful stop timeout; allowed range is 0–45 seconds. |
| `CRAFTPILOT_LOG_LEVEL` | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

Existing `.env` settings are preserved by setup except for the detected `DOCKER_GID`. Check the Compose configuration and `.env` if your host uses a different data path or group.

## Operations and safety

- The agent accepts only fixed `snapshot`, `start`, `stop`, and `console` operations. Console input is sent over authenticated RCON; it is never interpolated into a host-shell command.
- Minecraft containers are named from server UUIDs and are reused or stopped only when their CraftPilot ownership labels match. Image, resource, port, user, network, mount, and data-path settings are validated against the agent’s rules.
- RCON is not published to the host. The agent connects to the container over the configured private Docker network. Random per-server RCON passwords are stored in the private state volume, and `server.properties` updates are atomic.
- Snapshot downloads are restricted to the configured HTTPS master origin. The agent verifies byte count and SHA-256, rejects ZIP traversal, symlinks, special files, and excessive decompression, extracts to a temporary directory, and will not overwrite a populated destination.
- Telemetry includes at most 100 timestamped log lines per report. Log cursors advance only after the master accepts a report. Docker stats or log collection failures do not stop status polling.
- Logs are written to stderr and can be followed with `docker compose logs -f runtime-agent`. The default `INFO` level records lifecycle events and failures. Console command text, credentials, and RCON passwords are not logged. `DEBUG` adds Python diagnostic detail but does not enable payload or secret logging.
- HTTPS certificate verification remains enabled, and HTTPS redirects are not followed.

Restrict host access, keep `.env` and the `agent_state` volume private, and do not commit credentials. Configure firewall rules for the Minecraft ports that players need, and verify reachability from the CraftPilot Gate host.

## Update, inspect, and stop

To update to a chosen repository branch or tag, check compatibility with the CraftPilot master, pull that revision, and rebuild:

```sh
git pull
docker compose up -d --build
```

Inspect agent output with:

```sh
docker compose logs -f runtime-agent
```

Stop the agent while retaining its credentials and server data with:

```sh
docker compose down
```

Do not use `docker compose down -v` unless you intend to remove the saved agent state and credentials.

## Testing

Run the setup preflight tests from the repository root:

```sh
sh test_setup_resources.sh
```

Run the Docker-free agent test suite (requires Python 3.13 or later on the test machine):

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

Before production use, validate enrollment and credential persistence, heartbeat replay rejection, start/stop, lease expiry and retry, revoked-credential waiting and authorized re-enrollment, node outage behavior, firewall rules, and actual Minecraft connectivity. Perform end-to-end runtime checks on a disposable Linux Docker host and a staging CraftPilot master; a successful container start alone does not prove players can connect.

## Manual Docker installation

If you prefer to install Docker yourself, use the official instructions for your distribution:

- [Docker Engine installation](https://docs.docker.com/engine/install/)
- [Docker Compose installation](https://docs.docker.com/compose/install/linux/)
- [AWS Docker guidance for Amazon Linux](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-docker.html)

Verify the installation with:

```sh
docker --version
docker info
docker compose version
docker buildx version
```
