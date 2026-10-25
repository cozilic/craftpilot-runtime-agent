# CraftPilot Linux runtime-node agent

This early slice supports one-time initial server `SNAPSHOT` staging and remote Minecraft container `START`/`STOP`. It is a Python 3 standard-library program that calls a local Docker CLI with argument arrays; it does not provide arbitrary host shell execution. It does not synchronize or migrate an existing world, relay console/log/stat events, or support remote file operations. The master proxy routes to the node's advertised public address and server port, so the node firewall and Gate reachability must be configured by the operator. Do not offer remote placement as full production hosting until remaining operations and end-to-end network paths are verified.

## Requirements

- Linux host with Python 3 and Docker CLI/daemon.
- HTTPS access from the node to the CraftPilot master.
- A dedicated service account with narrowly controlled access to the Docker socket, its state directory and server data root. Access to the Docker socket is effectively host-root access; do not share it with untrusted users.
- The configured server data root exists and is writable. For a new remote server, pre-create an empty `<data-root>/<server-uuid>` directory or let the snapshot flow create it; the agent stages its initial server files from the master by authenticated SNAPSHOT command. An existing empty destination is populated safely. A populated destination is never overwritten; existing master-world data can be copied only if it is included in that first snapshot and the node target has no conflicting files.
- The runtime node has an operator-configured public address and inbound firewall rule for each server port. The master Gate proxy connects to `<public-address>:<server-port>`; verify that TCP path from the Gate host.

## Enroll and run

Create/enable the node in the master admin inventory. Use the node UUID and one-time registration token from that response. Provide the token using a protected service environment or secret manager; do not put it in a command argument, repository, ticket, or public log.

Required environment variables:

- `CRAFTPILOT_MASTER_URL`: HTTPS origin, e.g. `https://craftpilot.se` (no API path).
- `CRAFTPILOT_NODE_ID`: runtime node UUID from the admin inventory.
- `CRAFTPILOT_REGISTRATION_TOKEN`: one-use token for the first run only (32–128 characters).

Optional environment variables:

- `CRAFTPILOT_AGENT_STATE_DIR`: credential/sequence/lock storage; defaults to `/var/lib/craftpilot-agent`.
- `CRAFTPILOT_SERVER_DATA_ROOT`: per-server directories; defaults to `/srv/craftpilot/servers`.
- `CRAFTPILOT_ALLOWED_IMAGES`: comma-separated exact Docker image allowlist. Defaults to the master Java image list: Temurin JRE 8, 11, 17, 21 and 25.
- `CRAFTPILOT_DOCKER_NETWORK`: Docker bridge network name; defaults to `craftpilot-servers`.
- `CRAFTPILOT_POLL_SECONDS`: polling interval from 5 to 120 seconds; defaults to 30.
- `CRAFTPILOT_STOP_GRACE_SECONDS`: graceful Docker stop window from 0 to 45 seconds; defaults to 30.

Start under a supervisor such as systemd:

```sh
python3 /opt/craftpilot/runtime-agent/craftpilot_runtime_agent.py
```

Use one process and one state directory per node. On first enrollment, the agent exchanges the registration token, writes its long-lived credential and heartbeat sequence into a mode-0600 state file inside a mode-0700 directory, then removes the registration token from its own environment. Protect the state directory as a secret. If it is lost, disable/re-enroll the node; do not copy an agent credential into chat or logs.

## Docker and security boundaries

- Supported operations are exactly `snapshot`, `start` and `stop`; no command string from the master is executed on the host.
- The container name is derived from the server UUID. Existing containers are reused/stopped only when Docker labels identify the same managed CraftPilot server.
- New images must match the exact local allowlist. CPU, memory, port, user, command-wrapper, network, mount point and data path are validated before use.
- Server data paths are resolved beneath `CRAFTPILOT_SERVER_DATA_ROOT`; symlinks are rejected. SNAPSHOT downloads only from the configured HTTPS master origin with the node credential, verifies the declared byte count and SHA-256, rejects ZIP traversal/symlinks/special files and decompression limits, extracts in a temporary directory and atomically installs only into a missing or empty destination. A nonempty directory is never overwritten.
- Container labels and port mappings are set by the agent; Docker API access remains privileged. Restrict control-plane credentials and Docker socket access accordingly.
- HTTPS redirects are not followed, and the agent does not disable certificate verification.

## Staging verification checklist

Run the Docker-free local checks with `python3 -m unittest discover -s runtime-agent -p 'test_*.py'`. These fake the Docker CLI and cover authenticated snapshot delivery validation, size/digest checks, ZIP-slip rejection, safe staging and retry behavior, plus container/resource validation and data/label ownership boundaries. Then use a disposable Linux Docker host and a staging master to check registration and credential persistence, heartbeat replay rejection, fresh START, graceful STOP, forced STOP, already-running/already-stopped behavior, lease expiry/retry, revoked credentials and node outage. Verify Minecraft connectivity through the actual firewall/proxy path: a successful Docker start does not prove players can connect.
