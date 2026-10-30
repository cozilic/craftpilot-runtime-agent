<p align="center">
  <img src="assets/craftpilot-runtime-agent-logo.png" alt="CraftPilot Runtime Agent" width="720">
</p>

# CraftPilot Runtime Agent

The CraftPilot Runtime Agent connects a trusted host to a CraftPilot master so Minecraft servers can run on infrastructure you control. It communicates with the master over HTTPS and manages Minecraft containers through the host’s Docker daemon. The same Python agent and Minecraft Linux-container runtime are used on every platform; setup differs by host operating system.

> **Security warning:** The agent mounts the Docker socket, which grants effectively host-root control. Run it only on a trusted, dedicated host. Never expose the Docker socket over TCP, and protect the agent’s state volume and configuration as secrets.

> ## 🚨 Tested on Linux only
> **Only the Linux installation has been tested.** The macOS and Windows/WSL 2 setup instructions and launchers are included, but have **not been tested end to end** on those platforms.

## What it supports

The current agent supports a focused set of operations:

- **Initial snapshot staging** from the configured CraftPilot master.
- **Start and stop** for managed Minecraft containers.
- **Console command delivery** over authenticated RCON.
- **Node and server telemetry**, including container status, resource usage, uptime, disk usage, and a bounded log stream.

The agent does **not** currently migrate existing worlds or provide remote file operations. A running container does not by itself guarantee player connectivity: the node operator must configure the firewall and ensure the CraftPilot Gate can reach the node’s advertised Minecraft address and ports.

## Requirements

Use a trusted host with:

- A supported operating system: Linux, macOS with Docker Desktop, or Windows 10/11 with WSL 2 and Docker Desktop’s WSL integration.
- Docker Engine 19.03 or later, Docker Compose v2, and Docker Buildx 0.17.0 or later. Docker Desktop includes these tools on macOS and Windows.
- HTTPS access to the CraftPilot master.

The runtime container includes Python 3.13 and the Docker CLI; Python does not need to be installed on the host. The Docker daemon must manage both the agent and Minecraft containers, and must see the server-data path passed to Minecraft containers.

**Windows runs the agent in a WSL 2 Linux distribution.** Docker Desktop’s WSL integration must be enabled for that distribution. Keep the working copy and server data in the WSL Linux filesystem (under `~/craftpilot-runtime-agent`), not on `/mnt/c`; this preserves Linux file permissions required by Minecraft and avoids Docker Desktop path translation problems. The Windows PowerShell launcher copies/synchronizes the repository to that WSL location and leaves its `.env` and server data in place between runs.

On macOS, use a server-data directory under your home directory and ensure Docker Desktop file sharing permits the directory. The macOS setup checks that Docker can access its socket and that an agent-mounted file is visible to a Minecraft sibling-container bind mount before it starts the agent.

The setup preflight on Linux requires at least 1 GiB of currently available host RAM, a reported Docker daemon memory limit of at least 1 GiB, and 1 GiB of free space on the server-data filesystem. It warns below the recommended levels of 4 GiB RAM and 10 GiB free disk. These are estimates, not guarantees that a particular modpack or workload will fit.

## Install and start

### Linux

Install Docker Engine, Compose v2 and Buildx for your distribution, then clone and prepare the agent:

```sh
git clone https://github.com/cozilic/craftpilot-runtime-agent.git
cd craftpilot-runtime-agent
sh setup.sh
docker compose up -d --build
docker compose logs -f runtime-agent
```

`setup.sh` checks the host tools, Docker daemon, Compose, Buildx, available resources, and resolved Compose configuration. It prepares a private `.env`, detects the Docker socket’s group ID, and creates the shared server-data directory. Review the generated `.env` before starting the service. It may offer to install missing packages or Docker plugins; it asks before installing or making privileged system changes.

The automatic Docker Engine installation in `setup.sh` targets Debian, Ubuntu, Fedora, RHEL 8–10, CentOS Stream 9–10, Amazon Linux 2, and Amazon Linux 2023. Other distributions receive manual installation guidance.

### macOS

Install and start [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/), then use Terminal:

```sh
git clone https://github.com/cozilic/craftpilot-runtime-agent.git
cd craftpilot-runtime-agent
sh setup-macos.sh
```

The script creates `.env` if needed, preserves existing settings, chooses `~/CraftPilot/runtime-agent/server-data` when the Linux default is still configured, validates Docker socket access and the daemon’s data mount, then builds and starts the agent. If Docker Desktop reports an unavailable socket, enable its default Unix socket option. If the data-mount check fails, allow the data directory in **Docker Desktop → Settings → Resources → File Sharing**. Review `.env` before first start.

### Windows 10/11

Install [WSL 2](https://learn.microsoft.com/windows/wsl/install) and [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/). In Docker Desktop, select the WSL 2 backend and enable integration for the Linux distribution you will use (Ubuntu is recommended). Install Git in that distribution.

Run PowerShell as your normal Windows user (not as Administrator), from a checkout of this repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\setup-windows.ps1
```

The launcher uses the integrated WSL Docker CLI and creates or updates a working copy at `~/craftpilot-runtime-agent` inside Ubuntu (or the first available WSL distribution). The first run copies the checked-out repository; later runs synchronize source files while preserving the WSL `.env`, Git metadata, and server data. The agent, its data and any Docker builds then use Linux paths inside WSL. If you want to manage Git in WSL directly, clone the repository there and run the setup from that checkout instead.

The launcher verifies Linux-container mode, Compose, Buildx, the WSL Docker socket, and Minecraft UID/GID write access before building and starting the agent. Review `~/craftpilot-runtime-agent/.env` inside WSL before enrolling the node. To view logs after setup, open Ubuntu/your selected WSL distribution and run:

```sh
cd ~/craftpilot-runtime-agent
docker compose logs -f runtime-agent
```

Do not run the Linux `setup.sh` from PowerShell or execute the Compose project from a Windows-mounted `/mnt/c` checkout; Linux socket and sibling-container data mounts require the WSL filesystem and Docker integration.

## Enroll the node

Enrollment is initiated by the agent; you do not need to create a node or copy a node ID and token manually during normal setup.

1. Start the agent. It creates a unique, expiring enrollment request and polls the CraftPilot master over HTTPS.
2. A sysadmin reviews and approves or denies the request in **Ops & log → Runtime nodes**. The request ID is available in the agent logs.
3. After approval, the agent stores its node credential with restrictive permissions and acknowledges the enrollment.
4. The node appears in CraftPilot and can receive supported server operations.

Use one agent and one state volume per node. The named `agent_state` volume stores the enrollment state, long-lived node credential, heartbeat sequence, and per-server RCON passwords. Protect and back up this volume as a secret.

If the state volume is lost, ask a sysadmin to disable and re-enroll the node; do not copy credentials from another node. A disabled node’s saved credential cannot resume polling or execute commands. Re-enabling requires an explicit sysadmin action and a fresh registration token in `.env`.

## Configuration

The checked-in `.env.example` provides safe defaults, including `CRAFTPILOT_MASTER_URL=https://craftpilot.se`. Linux setup fills in the host-specific `DOCKER_GID`; the macOS and Windows launchers configure the Docker Desktop/WSL socket group. No registration token is needed for initial self-enrollment.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CRAFTPILOT_MASTER_URL` | `https://craftpilot.se` | CraftPilot master origin. |
| `CRAFTPILOT_SERVER_DATA_HOST_PATH` | Linux: `/srv/craftpilot/servers`; Desktop defaults are set by the platform setup | Directory mounted into the agent. Keep it dedicated to server data. |
| `CRAFTPILOT_DOCKER_SERVER_DATA_ROOT` | Same as `CRAFTPILOT_SERVER_DATA_HOST_PATH` | Path that the Docker daemon uses as the source for Minecraft container mounts; set separately only when Docker’s daemon path differs. |
| `DOCKER_GID` | Detected by setup | Numeric supplementary group for the Docker socket. Docker access is privileged. |
| `CRAFTPILOT_REGISTRATION_TOKEN` | Empty | One-time token used only when an explicitly disabled node is re-enabled. Remove it after registration succeeds. |
| `CRAFTPILOT_SERVER_DATA_GID` | `33` | Group used for shared server data; matches the default Minecraft container user (`33:33`). |
| `CRAFTPILOT_DOCKER_NETWORK` | `craftpilot-servers` | Isolated bridge network for game containers. |
| `CRAFTPILOT_ALLOWED_IMAGES` | Temurin JRE 8, 11, 17, 21 and 25 images | Comma-separated exact image allowlist. |
| `CRAFTPILOT_POLL_SECONDS` | `30` | Polling interval; allowed range is 5–120 seconds. |
| `CRAFTPILOT_STOP_GRACE_SECONDS` | `30` | Graceful stop timeout; allowed range is 0–45 seconds. |
| `CRAFTPILOT_LOG_LEVEL` | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |

Setup updates only platform-dependent settings; other `.env` values are preserved. Keep `.env` private and do not commit it.

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

On Windows, run these commands inside the selected WSL distribution in `~/craftpilot-runtime-agent`; rerunning `setup-windows.ps1` from the Windows checkout also syncs and rebuilds the WSL copy.

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

Run the setup preflight tests from the repository root. These shell-based checks do **not** mean that macOS or Windows/WSL installation has been tested.

```sh
sh test_setup_resources.sh
```

Run the Docker-free agent test suite (requires Python 3.13 or later on the test machine):

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

Before production use, validate enrollment and credential persistence, heartbeat replay rejection, start/stop, lease expiry and retry, revoked-credential waiting and authorized re-enrollment, node outage behavior, firewall rules, and actual Minecraft connectivity. Perform end-to-end runtime checks on a disposable Linux Docker host and a staging CraftPilot master; a successful container start alone does not prove players can connect.

## Manual Docker installation

If you prefer to install Docker yourself on Linux, use the official instructions for your distribution:

- [Docker Engine installation](https://docs.docker.com/engine/install/)
- [Docker Compose installation](https://docs.docker.com/compose/install/linux/)
- [AWS Docker guidance for Amazon Linux](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-docker.html)

On macOS and Windows, use [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/) or [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) with its WSL 2 backend on Windows.
