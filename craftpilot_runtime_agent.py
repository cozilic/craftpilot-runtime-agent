#!/usr/bin/env python3
"""CraftPilot Linux runtime-node agent (stdlib only)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
CONTAINER_RE = re.compile(r"^craftpilot-[0-9a-f]{8}$")
DOCKER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
USER_RE = re.compile(r"^[0-9]{1,10}:[0-9]{1,10}$")
LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")
DEFAULT_IMAGES = frozenset({
    "eclipse-temurin:8-jre", "eclipse-temurin:11-jre", "eclipse-temurin:17-jre",
    "eclipse-temurin:21-jre", "eclipse-temurin:25-jre",
})


class AgentError(RuntimeError):
    pass


class HttpStatusError(AgentError):
    def __init__(self, status: int):
        super().__init__(f"master returned HTTP {status}")
        self.status = status


class DockerError(AgentError):
    pass


@dataclass(frozen=True)
class Config:
    master_url: str
    node_id: str | None
    registration_token: str | None
    state_dir: Path
    data_root: Path
    images: frozenset[str]
    network: str = "craftpilot-servers"
    poll_seconds: int = 30
    stop_grace_seconds: int = 30
    docker_timeout_seconds: int = 45
    docker_data_root: str | None = None
    data_gid: int = 33

    @classmethod
    def from_env(cls) -> "Config":
        master = os.environ.get("CRAFTPILOT_MASTER_URL", "").rstrip("/")
        node_id = os.environ.get("CRAFTPILOT_NODE_ID", "").lower() or None
        token = os.environ.get("CRAFTPILOT_REGISTRATION_TOKEN") or None
        token_file = os.environ.get("CRAFTPILOT_REGISTRATION_TOKEN_FILE")
        if token is None and token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip() or None
            except OSError as exc:
                raise AgentError("could not read CRAFTPILOT_REGISTRATION_TOKEN_FILE") from exc
        state_dir = Path(os.environ.get("CRAFTPILOT_AGENT_STATE_DIR", "/var/lib/craftpilot-agent"))
        data_root = Path(os.environ.get("CRAFTPILOT_SERVER_DATA_ROOT", "/srv/craftpilot/servers"))
        data_gid = _bounded_int("CRAFTPILOT_SERVER_DATA_GID", 33, 0, 2**31 - 1)
        configured_images = os.environ.get("CRAFTPILOT_ALLOWED_IMAGES", "")
        images = frozenset(item.strip() for item in configured_images.split(",") if item.strip()) or DEFAULT_IMAGES
        network = os.environ.get("CRAFTPILOT_DOCKER_NETWORK", "craftpilot-servers")
        poll_seconds = _bounded_int("CRAFTPILOT_POLL_SECONDS", 30, 5, 120)
        stop_grace = _bounded_int("CRAFTPILOT_STOP_GRACE_SECONDS", 30, 0, 45)
        if node_id is not None and not UUID_RE.fullmatch(node_id):
            raise AgentError("CRAFTPILOT_NODE_ID must be a node UUID")
        if not master.startswith("https://"):
            raise AgentError("CRAFTPILOT_MASTER_URL must use HTTPS")
        if not NAME_RE.fullmatch(network):
            raise AgentError("CRAFTPILOT_DOCKER_NETWORK is invalid")
        if not images:
            raise AgentError("CRAFTPILOT_ALLOWED_IMAGES must not be empty")
        return cls(
            master, node_id, token, state_dir, data_root, images, network, poll_seconds, stop_grace,
            docker_data_root=os.environ.get("CRAFTPILOT_DOCKER_SERVER_DATA_ROOT") or None,
            data_gid=data_gid,
        )


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise AgentError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise AgentError(f"{name} must be between {minimum} and {maximum}")
    return value


def _prepare_shared_server_directory(path: Path, group_id: int) -> None:
    """Give Minecraft's runtime GID access to a new staged server directory."""
    if os.name == "nt":
        return
    try:
        os.chown(path, -1, group_id)
        os.chmod(path, 0o2770)
    except OSError as exc:
        raise AgentError(f"runtime agent cannot prepare server data for GID {group_id}") from exc


class DockerRuntime:
    """Executes only fixed Docker operations; never invokes a host shell."""




    def __init__(self, config: Config, runner=subprocess.run):
        self.config = config
        self.runner = runner
        self.state_store: StateStore | None = None
        self.state: dict[str, Any] | None = None
        self.agent_container_name = os.environ.get("CRAFTPILOT_AGENT_CONTAINER_NAME", "craftpilot-runtime-agent")
        if not NAME_RE.fullmatch(self.agent_container_name):
            raise AgentError("CRAFTPILOT_AGENT_CONTAINER_NAME is invalid")
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def execute(self, command_type: str, payload: dict[str, Any], credential: str | None = None,
                command_id: str | None = None) -> dict[str, Any]:
        server_id = payload.get("server_id")
        if not isinstance(server_id, str) or not UUID_RE.fullmatch(server_id):
            raise AgentError("command has an invalid server id")
        expected_name = f"craftpilot-{server_id.replace('-', '')[:8]}"
        if not CONTAINER_RE.fullmatch(expected_name):
            raise AgentError("derived container name is invalid")
        if command_type == "snapshot":
            return self._stage_snapshot(server_id, payload, credential, command_id)
        if command_type == "start":
            container = payload.get("container")
            if not isinstance(container, dict) or container.get("name") != expected_name:
                raise AgentError("container specification does not match the server id")
            return self._start(server_id, expected_name, container)
        if command_type == "stop":
            force = payload.get("force", False)
            if not isinstance(force, bool):
                raise AgentError("stop force flag must be a boolean")
            return self._stop(server_id, expected_name, force)
        if command_type == "console":
            return self._console(server_id, expected_name, payload.get("command"))
        raise AgentError("unsupported command type")

    def _stage_snapshot(self, server_id: str, payload: dict[str, Any], credential: str | None,
                        command_id: str | None) -> dict[str, Any]:
        metadata, download_url = payload.get("snapshot"), payload.get("download_url")
        if not isinstance(metadata, dict) or not isinstance(download_url, str):
            raise AgentError("snapshot command metadata is invalid")
        expected_bytes, expected_sha256 = metadata.get("bytes"), metadata.get("sha256")
        if (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int)
                or not 1 <= expected_bytes <= 4 * 1024 * 1024 * 1024
                or not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)):
            raise AgentError("snapshot size or digest is invalid")
        parsed, master = urllib.parse.urlsplit(download_url), urllib.parse.urlsplit(self.config.master_url)
        filename = metadata.get("filename")
        expected_filename = f"{server_id}-{command_id}.zip" if isinstance(command_id, str) else ""
        expected_path = (f"/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/snapshot/{filename}"
                         if isinstance(filename, str) and command_id is not None else "")
        if (credential is None or command_id is None or not UUID_RE.fullmatch(command_id)
                or not isinstance(filename, str) or filename != expected_filename
                or parsed.scheme != "https" or parsed.netloc != master.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path != expected_path):
            raise AgentError("snapshot URL must match the command on the configured HTTPS master")
        root = self.config.data_root.resolve(strict=True)
        destination = root / server_id
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise AgentError("snapshot destination is not a safe server directory")
        if destination.exists() and any(destination.iterdir()):
            raise AgentError("snapshot destination must be missing or empty")
        staging = Path(tempfile.mkdtemp(prefix=f".{server_id}-snapshot-", dir=root))
        archive_path, extract_path = staging / "snapshot.zip", staging / "extracted"
        extract_path.mkdir(mode=0o700)
        try:
            _prepare_shared_server_directory(extract_path, self.config.data_gid)
            request = urllib.request.Request(download_url, headers={
                "Accept": "application/zip", "Authorization": f"Bearer {credential}",
            })
            digest, received = hashlib.sha256(), 0
            with self._opener.open(request, timeout=60) as response, archive_path.open("xb") as archive:
                if response.status != 200:
                    raise HttpStatusError(response.status)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > expected_bytes:
                        raise AgentError("snapshot archive exceeded its declared size")
                    digest.update(chunk)
                    archive.write(chunk)
                archive.flush()
                os.fsync(archive.fileno())
            if received != expected_bytes or digest.hexdigest() != expected_sha256:
                raise AgentError("snapshot archive size or SHA-256 does not match master metadata")
            extracted_bytes, file_count = 0, 0
            with zipfile.ZipFile(archive_path) as archive:
                for item in archive.infolist():
                    name, mode = item.filename, item.external_attr >> 16
                    member, file_type = Path(item.filename), stat.S_IFMT(mode)
                    if (not name or member.is_absolute() or "\\" in name
                            or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                            or file_type not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 0x1):
                        raise AgentError("snapshot contains an unsafe path, file type, or encrypted entry")
                    extracted_bytes += item.file_size
                    file_count += 0 if item.is_dir() else 1
                    if extracted_bytes > 4 * 1024 * 1024 * 1024 or file_count > 50000:
                        raise AgentError("snapshot exceeds local extraction limits")
                    target = (extract_path / member).resolve()
                    if target != extract_path.resolve() and extract_path.resolve() not in target.parents:
                        raise AgentError("snapshot path escapes staging directory")
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item) as source, target.open("xb") as output:
                            while chunk := source.read(1024 * 1024):
                                output.write(chunk)
            if destination.exists():
                if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
                    raise AgentError("snapshot destination is no longer empty; refusing to overwrite it")
                destination.rmdir()
            _prepare_shared_server_directory(extract_path, self.config.data_gid)
            os.replace(extract_path, destination)
            return {"success": True, "result_code": "SNAPSHOT_STAGED", "message": expected_sha256,
                    "container_id": str(expected_bytes)}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _start(self, server_id: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        existing = self._inspect(name, "{{.State.Running}}")
        if existing is not None:
            self._verify_managed(server_id, name)
        if existing == "true":
            return {"success": True, "result_code": "ALREADY_RUNNING", "container_id": self._container_id(name)}
        self._server_data_path(server_id)
        self._ensure_network()
        self._connect_agent_to_runtime_network()
        if existing is None:
            data_path = self._docker_server_data_path(server_id)
            image = spec.get("image")
            if not isinstance(image, str) or image not in self.config.images:
                raise AgentError("container image is not in the node allowlist")
            command = self._container_command(spec.get("command"))
            memory = self._int_field(spec.get("memory_mb"), "memory_mb", 512, 131072)
            port = self._int_field(spec.get("port"), "port", 1, 65535)
            cpus = self._cpu_field(spec.get("cpus"))
            container_user = spec.get("container_user")
            if not isinstance(container_user, str) or not USER_RE.fullmatch(container_user):
                raise AgentError("container user is invalid")
            network, mount_path = spec.get("network"), spec.get("mount_path")
            if not isinstance(network, str) or network != self.config.network or not NAME_RE.fullmatch(network):
                raise AgentError("container network does not match the configured node network")
            if mount_path != "/data":
                raise AgentError("container mount path is not supported")
            self._ensure_rcon(server_id)
            args = ["docker", "run", "--detach", "--name", name, "--label", "craftpilot.managed=1",
                    "--label", f"craftpilot.server_id={server_id}", "--network", network,
                    "--memory", f"{memory}m", "--memory-swap", f"{memory}m", "--cpus", cpus,
                    "--restart", "no", "--user", container_user, "--volume", f"{data_path}:/data:rw",
                    "--publish", f"{port}:25565/tcp", image, *command]
            output = self._run(args, timeout=self.config.docker_timeout_seconds)
            candidate_id = output.strip().splitlines()[-1] if output.strip() else ""
            if candidate_id and not DOCKER_ID_RE.fullmatch(candidate_id):
                raise DockerError("Docker returned an invalid container id")
        else:
            self._ensure_rcon(server_id)
            self._run(["docker", "start", name], timeout=self.config.docker_timeout_seconds)
        if self._inspect(name, "{{.State.Running}}") != "true":
            raise DockerError("container did not remain running after start")
        return {"success": True, "result_code": "STARTED", "container_id": self._container_id(name)}

    def _console(self, server_id: str, name: str, command: Any) -> dict[str, Any]:
        if (not isinstance(command, str) or not command or len(command) > 16384
                or "\x00" in command or "\n" in command or "\r" in command):
            raise AgentError("console command is invalid")
        if self._inspect(name, "{{.State.Running}}") != "true":
            raise AgentError("server container is not running")
        self._verify_managed(server_id, name)
        self._send_rcon(server_id, name, command)
        return {"success": True, "result_code": "CONSOLE_EXECUTED",
                "message": hashlib.sha256(command.encode("utf-8")).hexdigest()}

    def _ensure_rcon(self, server_id: str) -> None:
        if self.state is None or self.state_store is None:
            raise AgentError("secure RCON state is unavailable")
        passwords = self.state.setdefault("rcon_passwords", {})
        if not isinstance(passwords, dict):
            raise AgentError("saved RCON credentials are invalid")
        password = passwords.get(server_id)
        if not isinstance(password, str) or not re.fullmatch(r"[a-f0-9]{64}", password):
            password = os.urandom(32).hex()
            passwords[server_id] = password
            self.state_store.save(self.state)
        properties = self._server_data_path(server_id) / "server.properties"
        if properties.is_symlink():
            raise AgentError("server.properties is not a regular file")
        lines: list[str] = []
        original_mode = 0o644
        if properties.exists():
            try:
                descriptor = os.open(properties, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "r", encoding="utf-8") as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise AgentError("server.properties is not a regular file")
                    lines = file.read().splitlines()
                original_mode = stat.S_IMODE(properties.stat().st_mode)
            except (OSError, UnicodeError) as exc:
                raise AgentError("server.properties is unavailable for secure console setup") from exc
        replacements = {"enable-rcon": "true", "rcon.port": "25575", "rcon.password": password,
                        "broadcast-rcon-to-ops": "false", "rcon.ip": "0.0.0.0"}
        found: set[str] = set()
        updated: list[str] = []
        for line in lines:
            key, separator, _value = line.partition("=")
            if separator and key in replacements:
                if key not in found:
                    updated.append(f"{key}={replacements[key]}")
                    found.add(key)
            else:
                updated.append(line)
        for key, value in replacements.items():
            if key not in found:
                updated.append(f"{key}={value}")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".server-properties-", dir=properties.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write("\n".join(updated) + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, (original_mode & 0o600) | 0o660)
            os.replace(temporary, properties)
            self.state_store.save(self.state)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _connect_agent_to_runtime_network(self) -> None:
        result = self.runner(["docker", "network", "connect", self.config.network, self.agent_container_name],
                             capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0 and "already exists" not in result.stderr.lower():
            raise DockerError("could not attach the agent to the isolated game network")

    def _send_rcon(self, server_id: str, name: str, command: str) -> None:
        passwords = self.state.get("rcon_passwords") if isinstance(self.state, dict) else None
        password = passwords.get(server_id) if isinstance(passwords, dict) else None
        if not isinstance(password, str) or not re.fullmatch(r"[a-f0-9]{64}", password):
            raise AgentError("RCON credentials are unavailable for this server")
        raw_networks = self._run(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name], timeout=10)
        try:
            networks = json.loads(raw_networks)
            address = networks[self.config.network]["IPAddress"]
            socket.inet_pton(socket.AF_INET, address)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
            raise DockerError("server container has no address on its isolated runtime network") from exc
        def packet(request_id: int, packet_type: int, body: str) -> bytes:
            payload = body.encode("utf-8") + b"\0\0"
            length = 4 + 4 + len(payload)
            return length.to_bytes(4, "little", signed=True) + request_id.to_bytes(4, "little", signed=True) + packet_type.to_bytes(4, "little", signed=True) + payload
        def receive(connection: socket.socket) -> tuple[int, int, bytes]:
            def read_exact(size: int) -> bytes:
                chunks = bytearray()
                while len(chunks) < size:
                    part = connection.recv(size - len(chunks))
                    if not part:
                        raise DockerError("RCON connection closed unexpectedly")
                    chunks.extend(part)
                return bytes(chunks)
            length = int.from_bytes(read_exact(4), "little", signed=True)
            if length < 10 or length > 1_048_576:
                raise DockerError("RCON returned an invalid packet length")
            body = read_exact(length)
            return (int.from_bytes(body[:4], "little", signed=True),
                    int.from_bytes(body[4:8], "little", signed=True), body[8:-2])
        try:
            with socket.create_connection((address, 25575), timeout=5) as connection:
                connection.settimeout(5)
                connection.sendall(packet(1, 3, password))
                auth_id, auth_type, _ = receive(connection)
                if auth_id == -1:
                    raise AgentError("Minecraft RCON rejected the node credential")
                if auth_id != 1 or auth_type != 2:
                    raise DockerError("Minecraft RCON returned an unexpected authentication response")
                connection.sendall(packet(2, 2, command))
                response_id, response_type, _ = receive(connection)
                if response_id != 2 or response_type != 0:
                    raise DockerError("Minecraft RCON returned an unexpected command response")
        except (OSError, TimeoutError) as exc:
            raise DockerError("could not reach the Minecraft RCON endpoint") from exc

    def _stop(self, server_id: str, name: str, force: bool) -> dict[str, Any]:
        running = self._inspect(name, "{{.State.Running}}")
        if running is None:
            return {"success": True, "result_code": "ALREADY_STOPPED"}
        self._verify_managed(server_id, name)
        if running == "false":
            return {"success": True, "result_code": "ALREADY_STOPPED"}
        if running != "true":
            raise DockerError("Docker returned an invalid running state")
        if force:
            self._run(["docker", "kill", "--signal=KILL", name], timeout=15)
        else:
            self._send_rcon(server_id, name, "stop")
            deadline = time.monotonic() + self.config.stop_grace_seconds + 15
            while time.monotonic() < deadline:
                state = self._inspect(name, "{{.State.Status}}")
                if state == "exited":
                    return {"success": True, "result_code": "STOPPED"}
                if state in (None, "dead"):
                    raise DockerError("server container disappeared during graceful stop")
                time.sleep(0.5)
            raise DockerError("Minecraft did not finish graceful shutdown before the stop timeout")
        if self._inspect(name, "{{.State.Running}}") == "true":
            raise DockerError("container is still running after stop")
        return {"success": True, "result_code": "STOPPED"}

    def _server_data_path(self, server_id: str) -> Path:
        root = self.config.data_root.resolve(strict=True)
        candidate = root / server_id
        if candidate.is_symlink() or not candidate.is_dir():
            raise AgentError("server data is not staged on this node")
        resolved = candidate.resolve(strict=True)
        if resolved.parent != root or resolved == root:
            raise AgentError("server data path escapes the configured root")
        return resolved

    def _docker_server_data_path(self, server_id: str) -> str:
        if self.config.docker_data_root is None:
            return str(self._server_data_path(server_id))
        root = self.config.docker_data_root.rstrip("/\\")
        if not root or "\0" in root or "\n" in root or "\r" in root:
            raise AgentError("Docker server data root is invalid")
        return f"{root}/{server_id}"

    @staticmethod
    def _container_command(value: Any) -> list[str]:
        if (not isinstance(value, list) or len(value) != 3 or value[0] != "sh" or value[1] != "-c"
                or not isinstance(value[2], str) or not value[2] or len(value[2]) > 16384 or "\0" in value[2]):
            raise AgentError("container command does not match the supported runtime wrapper")
        return value

    @staticmethod
    def _int_field(value: Any, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise AgentError(f"container {name} is outside the allowed range")
        return value

    @staticmethod
    def _cpu_field(value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.1 <= value <= 64:
            raise AgentError("container cpus is outside the allowed range")
        return f"{value:g}"

    def _ensure_network(self) -> None:
        result = self.runner(["docker", "network", "inspect", self.config.network], capture_output=True,
                             text=True, timeout=10, check=False)
        if result.returncode == 0:
            return
        if "no such network" not in result.stderr.lower() and "not found" not in result.stderr.lower():
            raise DockerError("Docker network inspection failed")
        created = self.runner(["docker", "network", "create", "--driver", "bridge", self.config.network],
                              capture_output=True, text=True, timeout=15, check=False)
        if created.returncode != 0 and "already exists" not in created.stderr.lower():
            raise DockerError("Docker network creation failed")

    def _verify_managed(self, server_id: str, name: str) -> None:
        labels_raw = self._inspect(name, "{{json .Config.Labels}}")
        if labels_raw is None:
            raise DockerError("container disappeared while verifying ownership")
        try:
            labels = json.loads(labels_raw)
        except json.JSONDecodeError as exc:
            raise DockerError("Docker returned invalid container labels") from exc
        if (not isinstance(labels, dict) or labels.get("craftpilot.managed") != "1"
                or labels.get("craftpilot.server_id") != server_id):
            raise AgentError("refusing to manage a container not owned by this server")

    def _inspect(self, name: str, template: str) -> str | None:
        result = self.runner(["docker", "inspect", "--format", template, name], capture_output=True,
                             text=True, timeout=10, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        if "no such object" in result.stderr.lower() or "no such container" in result.stderr.lower():
            return None
        raise DockerError("Docker container inspection failed")

    def _server_disk_used_mb(self, server_id: str) -> int | None:
        try:
            output = self._run(["du", "-sb", "--", str(self._server_data_path(server_id))], timeout=10)
            return RuntimeNodeAgent._to_mib(output.split("\t", 1)[0])
        except (AgentError, DockerError, OSError, TimeoutError):
            return None

    def _container_logs(self, name: str, since: str | None = None) -> list[dict[str, str]]:
        args = ["docker", "logs", "--timestamps"]
        if since is not None:
            if not LOG_TIMESTAMP_RE.fullmatch(since):
                raise AgentError("runtime log cursor is invalid")
            args.extend(["--since", since])
        args.extend(["--tail", "100", name])
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerError("Docker log collection failed or timed out") from exc
        if result.returncode != 0:
            detail = " ".join((result.stderr or "").split())[:220]
            raise DockerError(detail or "Docker log collection failed")

        from datetime import datetime
        parsed_since = None
        if since is not None:
            try:
                parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AgentError("runtime log cursor is invalid") from exc
        entries: list[tuple[datetime, int, dict[str, str]]] = []
        for stream_index, stream in enumerate((result.stdout or "", result.stderr or "")):
            for raw_line in stream.splitlines():
                timestamp, separator, text = raw_line.partition(" ")
                if not separator or not LOG_TIMESTAMP_RE.fullmatch(timestamp) or not text:
                    continue
                try:
                    parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed_since is not None and parsed_timestamp <= parsed_since:
                    continue
                entries.append((parsed_timestamp, stream_index, {"ts": timestamp, "text": text[-8192:]}))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in entries[-100:]]

    def _container_id(self, name: str) -> str:
        value = self._inspect(name, "{{.Id}}")
        if value is None or not DOCKER_ID_RE.fullmatch(value):
            raise DockerError("Docker returned an invalid container id")
        return value


    def _run(self, args: list[str], timeout: int) -> str:
        try:
            result = self.runner(args, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerError("Docker operation failed or timed out") from exc
        if result.returncode != 0:
            detail = " ".join(result.stderr.split())[:220]
            raise DockerError(detail or "Docker operation failed")
        return result.stdout


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "agent-state.json"

    def load(self) -> dict[str, Any] | None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                os.close(descriptor)
                raise AgentError("agent state path is not a regular file")
            if os.name != "nt" and info.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError("agent state file is unreadable or invalid") from exc
        if not isinstance(state, dict):
            raise AgentError("agent state file has an invalid shape")
        return state

    def save(self, state: dict[str, Any]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".agent-state-", dir=self.directory)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, separators=(",", ":"))
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class RuntimeNodeAgent:
    def __init__(self, config: Config, runtime: DockerRuntime | None = None, store: StateStore | None = None):
        self.config, self.runtime = config, runtime or DockerRuntime(config)
        self.store = store or StateStore(config.state_dir)
        self.state: dict[str, Any] | None = None
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        if isinstance(self.runtime, DockerRuntime):
            self.runtime.state_store = self.store

    def ensure_registered(self) -> bool:
        state = self.store.load()
        if state is not None and state.get("enrollment_request_id") and not state.get("credential"):
            request_id, request_secret = state.get("enrollment_request_id"), state.get("enrollment_request_secret")
            if not isinstance(request_id, str) or not UUID_RE.fullmatch(request_id) or not isinstance(request_secret, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", request_secret):
                raise AgentError("saved enrollment request is invalid")
            self.pending_enrollment = {"request_id": request_id, "request_secret": request_secret}
            return False
        if state is not None:
            credential, node_id, sequence = state.get("credential"), state.get("node_id"), state.get("sequence")
            if ((node_id is not None and (not isinstance(node_id, str) or not UUID_RE.fullmatch(node_id)))
                    or (self.config.node_id is not None and node_id is not None and node_id != self.config.node_id)
                    or not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", credential)
                    or isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 2147483647):
                raise AgentError("saved node credentials do not match the configured node")
            self.state = state
            if isinstance(self.runtime, DockerRuntime):
                self.runtime.state = self.state
            if isinstance(node_id, str):
                self._set_node_id(node_id)
            if state.get("enrollment_request_id") and isinstance(node_id, str):
                self._complete_dynamic_enrollment()
            return isinstance(node_id, str) and "enrollment_request_id" not in self.state
        if self.config.node_id is not None:
            token = self.config.registration_token
            if not token or not 32 <= len(token) <= 128:
                raise AgentError("registration token is required for first enrollment")
            result = self._request("POST", f"/api/runtime/nodes/{self.config.node_id}/register", {"registration_token": token})
            credential = result.get("credential")
            if result.get("node_id") != self.config.node_id or not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", credential):
                raise AgentError("master returned an invalid registration response")
            master_url = result.get("master_url", self.config.master_url)
            if not isinstance(master_url, str) or master_url.rstrip("/") != self.config.master_url:
                raise AgentError("registration response master URL does not match configured HTTPS origin")
            self.state = {"node_id": self.config.node_id, "credential": credential, "sequence": 0}
            if isinstance(self.runtime, DockerRuntime):
                self.runtime.state = self.state
            self.store.save(self.state)
            os.environ.pop("CRAFTPILOT_REGISTRATION_TOKEN", None)
            return True
        return self._request_dynamic_enrollment()

    def _set_node_id(self, node_id: str) -> None:
        from dataclasses import replace
        self.config = replace(self.config, node_id=node_id)
        self.runtime.config = self.config

    def _request_dynamic_enrollment(self) -> bool:
        result = self._request("POST", "/api/runtime/enrollments", {
            "hostname": socket.gethostname()[:255] or "craftpilot-agent", "agent_version": "local-docker",
        })
        request_id, request_secret = result.get("request_id"), result.get("request_secret")
        if not isinstance(request_id, str) or not UUID_RE.fullmatch(request_id) or not isinstance(request_secret, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", request_secret):
            raise AgentError("master returned an invalid enrollment request")
        self.pending_enrollment = {"request_id": request_id, "request_secret": request_secret}
        self.store.save({"node_id": None, "enrollment_request_id": request_id, "enrollment_request_secret": request_secret})
        print(f"Enrollment request {request_id} is waiting for sysadmin approval.", file=sys.stderr)
        return False

    def poll_enrollment_once(self) -> bool:
        pending = getattr(self, "pending_enrollment", None)
        if pending is None:
            state = self.store.load()
            if state is not None and state.get("enrollment_request_id"):
                pending = {"request_id": state["enrollment_request_id"], "request_secret": state["enrollment_request_secret"]}
                self.pending_enrollment = pending
            else:
                return self.ensure_registered()
        result = self._request("POST", f"/api/runtime/enrollments/{pending['request_id']}/poll", {"request_secret": pending["request_secret"]})
        status = result.get("status")
        if status in ("pending", "rejected", "expired", "completed"):
            if status in ("rejected", "expired"):
                raise AgentError(f"runtime enrollment request was {status}")
            return status == "completed"
        node_id, credential, master_url = result.get("node_id"), result.get("credential"), result.get("master_url")
        if (status != "approved" or not isinstance(node_id, str) or not UUID_RE.fullmatch(node_id)
                or not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", credential)
                or not isinstance(master_url, str) or master_url.rstrip("/") != self.config.master_url):
            raise AgentError("master returned an invalid enrollment approval")
        self._set_node_id(node_id)
        self.state = {"node_id": node_id, "credential": credential, "sequence": 0,
                      "enrollment_request_id": pending["request_id"], "enrollment_request_secret": pending["request_secret"]}
        if isinstance(self.runtime, DockerRuntime):
            self.runtime.state = self.state
        self.store.save(self.state)
        return self._complete_dynamic_enrollment()

    def _complete_dynamic_enrollment(self) -> bool:
        assert self.state is not None
        request_id, request_secret = self.state.get("enrollment_request_id"), self.state.get("enrollment_request_secret")
        if not isinstance(request_id, str) or not isinstance(request_secret, str):
            raise AgentError("saved enrollment confirmation is invalid")
        try:
            self._request("POST", f"/api/runtime/enrollments/{request_id}/complete", {"request_secret": request_secret})
        except HttpStatusError as exc:
            if exc.status != 409:
                raise
            self.state = None
            self.store.save({"node_id": None, "enrollment_request_id": request_id, "enrollment_request_secret": request_secret})
            self.pending_enrollment = {"request_id": request_id, "request_secret": request_secret}
            return False
        self.state.pop("enrollment_request_id", None)
        self.state.pop("enrollment_request_secret", None)
        self.store.save(self.state)
        self.pending_enrollment = None
        return True

    def poll_once(self) -> None:
        if self.state is None or (self.state is not None and self.state.get("enrollment_request_id")):
            if self.state is not None and self.state.get("enrollment_request_id"):
                if not self._complete_dynamic_enrollment():
                    return
            elif not self.ensure_registered():
                self.poll_enrollment_once()
                return
        assert self.state is not None
        sequence = self.state["sequence"] + 1
        if sequence > 2147483647:
            raise AgentError("heartbeat sequence exhausted; re-enroll this node")
        self.state["sequence"] = sequence
        self.store.save(self.state)
        claim = self._request("POST", f"/api/runtime/nodes/{self.config.node_id}/commands/claim", {"sequence": sequence}, credential=True)
        command = claim.get("command")
        if command is None:
            self._report_servers(sequence)
            return
        if not isinstance(command, dict):
            raise AgentError("master returned an invalid command")
        command_id, lease_token = command.get("id"), command.get("lease_token")
        command_type, payload = command.get("type"), command.get("payload")
        if (not isinstance(command_id, str) or not UUID_RE.fullmatch(command_id) or not isinstance(lease_token, str)
                or len(lease_token) != 64 or command_type not in ("start", "stop", "snapshot", "console") or not isinstance(payload, dict)):
            raise AgentError("master returned an invalid leased command")
        if command_type == "start":
            server_id = payload.get("server_id")
            if isinstance(server_id, str) and UUID_RE.fullmatch(server_id):
                stopped_servers = self.state.setdefault("intentional_stops", {})
                if not isinstance(stopped_servers, dict) or any(
                    not isinstance(key, str) or not UUID_RE.fullmatch(key)
                    or not isinstance(value, str) or not UUID_RE.fullmatch(value)
                    for key, value in stopped_servers.items()
                ):
                    raise AgentError("saved intentional stop state is invalid")
                if stopped_servers.pop(server_id, None) is not None:
                    self.store.save(self.state)
        try:
            result = self.runtime.execute(command_type, payload, credential=self.state["credential"], command_id=command_id)
        except (AgentError, DockerError) as exc:
            result = {"success": False, "result_code": "RUNTIME_ERROR", "message": str(exc)[:300]}
        if command_type in ("start", "stop") and result.get("success"):
            server_id = payload.get("server_id")
            if isinstance(server_id, str) and UUID_RE.fullmatch(server_id):
                stopped_servers = self.state.setdefault("intentional_stops", {})
                if not isinstance(stopped_servers, dict) or any(
                    not isinstance(key, str) or not UUID_RE.fullmatch(key)
                    or not isinstance(value, str) or not UUID_RE.fullmatch(value)
                    for key, value in stopped_servers.items()
                ):
                    raise AgentError("saved intentional stop state is invalid")
                if command_type == "stop":
                    stopped_servers[server_id] = command_id
                    self.store.save(self.state)
        self._request("POST", f"/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/ack",
                      {"lease_token": lease_token, **result}, credential=True)
        self._report_servers(sequence)

    def _report_servers(self, sequence: int) -> None:
        assert self.state is not None
        cursors = self.state.setdefault("log_cursors", {})
        if not isinstance(cursors, dict):
            raise AgentError("saved runtime log cursors are invalid")
        try:
            server_ids = self._assigned_server_ids()
        except (AgentError, DockerError, OSError, TimeoutError):
            return
        for server_id in server_ids:
            cursor = cursors.get(server_id)
            if cursor is not None and (not isinstance(cursor, str) or not LOG_TIMESTAMP_RE.fullmatch(cursor)):
                raise AgentError("saved runtime log cursor is invalid")
            try:
                report = self._server_report(server_id, sequence, cursor)
                if report is None:
                    continue
                self._request("POST", f"/api/runtime/nodes/{self.config.node_id}/servers/report", report, credential=True)
                if report["logs"]:
                    cursors[server_id] = report["logs"][-1]["ts"]
                    self.store.save(self.state)
            except HttpStatusError as exc:
                if exc.status == 401:
                    raise
            except (AgentError, DockerError, OSError, TimeoutError):
                continue

    def _assigned_server_ids(self) -> list[str]:
        output = self.runtime._run(["docker", "ps", "--all", "--filter", "label=craftpilot.managed=1",
                                    "--filter", "label=craftpilot.server_id", "--format", "{{.ID}}"], timeout=10)
        server_ids: set[str] = set()
        for container_id in output.splitlines():
            container_id = container_id.strip()
            if not DOCKER_ID_RE.fullmatch(container_id):
                continue
            try:
                server_id = self.runtime._inspect(container_id, '{{index .Config.Labels "craftpilot.server_id"}}')
            except DockerError:
                continue
            if not isinstance(server_id, str) or not UUID_RE.fullmatch(server_id):
                continue
            name = self.runtime._inspect(container_id, "{{.Name}}")
            if name is not None and name.lstrip("/") == f"craftpilot-{server_id.replace('-', '')[:8]}":
                server_ids.add(server_id.lower())
        return sorted(server_ids)

    def _server_report(self, server_id: str, sequence: int, log_since: str | None = None) -> dict[str, Any] | None:
        if not UUID_RE.fullmatch(server_id):
            return None
        name = f"craftpilot-{server_id.replace('-', '')[:8]}"
        if not CONTAINER_RE.fullmatch(name):
            return None
        running = self.runtime._inspect(name, "{{.State.Running}}")
        if running is None:
            return None
        self.runtime._verify_managed(server_id, name)
        state = self.runtime._inspect(name, "{{.State.Status}}")
        intentional_stops = self.state.get("intentional_stops", {}) if self.state is not None else {}
        if not isinstance(intentional_stops, dict) or any(
            not isinstance(key, str) or not UUID_RE.fullmatch(key)
            or not isinstance(value, str) or not UUID_RE.fullmatch(value)
            for key, value in intentional_stops.items()
        ):
            raise AgentError("saved intentional stop state is invalid")
        if state == "running" and running == "true" and server_id in intentional_stops:
            intentional_stops.pop(server_id)
            if self.state is not None:
                self.store.save(self.state)
        if state in ("exited", "dead"):
            exit_code = self.runtime._inspect(name, "{{.State.ExitCode}}") if state == "exited" else None
            status = "OFFLINE" if server_id in intentional_stops or (
                state == "exited" and exit_code in ("0", None)
            ) else "CRASHED"
        else:
            status = {"running": "ONLINE", "created": "STARTING", "restarting": "STARTING",
                      "paused": "STOPPING", "dead": "CRASHED"}.get(state or "", "ERROR")
        cpu_percent = memory_used_mb = memory_limit_mb = None
        if running == "true":
            try:
                stats = self.runtime._run(["docker", "stats", "--no-stream", "--format",
                                           "{{.CPUPerc}}|{{.MemUsage}}", name], timeout=10)
                cpu_percent, memory_used_mb, memory_limit_mb = self._parse_docker_stats(stats)
            except (DockerError, OSError, TimeoutError):
                pass
        try:
            started_at = self.runtime._run(["docker", "inspect", "--format", "{{.State.StartedAt}}", name], timeout=10).strip()
        except (DockerError, OSError, TimeoutError):
            started_at = ""
        disk_used_mb = self.runtime._server_disk_used_mb(server_id)
        try:
            logs = self.runtime._container_logs(name, log_since)
        except (DockerError, OSError, TimeoutError):
            logs = []
        return {"sequence": sequence, "server_id": server_id, "status": status, "cpu_percent": cpu_percent,
                "memory_used_mb": memory_used_mb, "memory_limit_mb": memory_limit_mb,
                "uptime_seconds": self._uptime_seconds(started_at) if running == "true" else None,
                "players_online": None, "players_max": None, "disk_used_mb": disk_used_mb, "logs": logs}

    @staticmethod
    def _parse_docker_stats(output: str) -> tuple[float | None, int | None, int | None]:
        try:
            cpu, memory = output.strip().split("|", 1)
            cpu_value = float(cpu.rstrip("%"))
            if not math.isfinite(cpu_value) or cpu_value < 0:
                return None, None, None
            used, limit = memory.split("/", 1)
            return cpu_value, RuntimeNodeAgent._parse_size_mb(used), RuntimeNodeAgent._parse_size_mb(limit)
        except (ValueError, TypeError):
            return None, None, None

    @staticmethod
    def _parse_size_mb(value: str) -> int | None:
        match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\s*", value, re.I)
        if match is None:
            return None
        factors = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
                   "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
        return round(float(match.group(1)) * factors[match.group(2).upper()] / (1024**2))

    @staticmethod
    def _to_mib(value: str) -> int | None:
        try:
            return max(0, round(int(value) / (1024**2)))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _uptime_seconds(started_at: str) -> int | None:
        if not started_at or started_at.startswith("0001-"):
            return None
        try:
            from datetime import datetime, timezone
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            return max(0, int((datetime.now(timezone.utc) - start).total_seconds()))
        except (ValueError, TypeError):
            return None

    def _container_logs(self, name: str, since: str | None = None) -> list[dict[str, str]]:
        args = ["docker", "logs", "--timestamps"]
        if since is not None:
            if not LOG_TIMESTAMP_RE.fullmatch(since):
                raise AgentError("runtime log cursor is invalid")
            args.extend(["--since", since])
        args.extend(["--tail", "100", name])
        try:
            result = self.runtime.runner(args, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerError("Docker log collection failed or timed out") from exc
        if result.returncode != 0:
            detail = " ".join((result.stderr or "").split())[:220]
            raise DockerError(detail or "Docker log collection failed")

        from datetime import datetime
        parsed_since = None
        if since is not None:
            try:
                parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AgentError("runtime log cursor is invalid") from exc
        entries: list[tuple[datetime, int, dict[str, str]]] = []
        for stream_index, stream in enumerate((result.stdout or "", result.stderr or "")):
            for raw_line in stream.splitlines():
                timestamp, separator, text = raw_line.partition(" ")
                if not separator or not LOG_TIMESTAMP_RE.fullmatch(timestamp) or not text:
                    continue
                try:
                    parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed_since is not None and parsed_timestamp <= parsed_since:
                    continue
                entries.append((parsed_timestamp, stream_index, {"ts": timestamp, "text": text[-8192:]}))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in entries[-100:]]

    def run_forever(self) -> None:
        if self.ensure_registered():
            print(f"CraftPilot runtime agent for node {self.config.node_id} is online.", file=sys.stderr)
        while True:
            started = time.monotonic()
            try:
                if self.state is None:
                    self.ensure_registered()
                if self.state is not None and self.state.get("enrollment_request_id"):
                    if self.poll_enrollment_once():
                        print(f"CraftPilot runtime agent for node {self.config.node_id} is online.", file=sys.stderr)
                else:
                    self.poll_once()
            except HttpStatusError as exc:
                if exc.status == 401:
                    raise AgentError("node credential was rejected or revoked; re-enroll the node") from exc
                if exc.status == 409:
                    print("Heartbeat/lease conflict; retrying with a new sequence.", file=sys.stderr)
                else:
                    print(f"Master request failed ({exc.status}); retrying.", file=sys.stderr)
            except (AgentError, OSError, TimeoutError) as exc:
                print(f"Agent iteration failed: {str(exc)[:300]}", file=sys.stderr)
            delay = max(0.0, (15 if self.state is None or self.state.get("enrollment_request_id") else self.config.poll_seconds)
                        - (time.monotonic() - started))
            time.sleep(delay)

    def _request(self, method: str, path: str, body: dict[str, Any], credential: bool = False) -> dict[str, Any]:
        if not path.startswith("/api/") or ".." in path:
            raise AgentError("invalid master API path")
        assert self.state is not None or not credential
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if credential:
            assert self.state is not None
            headers["Authorization"] = f"Bearer {self.state['credential']}"
        request = urllib.request.Request(self.config.master_url + path,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"), headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=15) as response:
                if not 200 <= response.status < 300:
                    raise HttpStatusError(response.status)
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise HttpStatusError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentError("could not reach the CraftPilot master") from exc
        if len(raw) > 1024 * 1024:
            raise AgentError("master response exceeded the size limit")
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentError("master returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise AgentError("master response must be a JSON object")
        return decoded


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def main() -> int:
    try:
        config = Config.from_env()
        config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = config.state_dir / "agent.lock"
        with lock_path.open("a", encoding="utf-8") as lock_file:
            os.chmod(lock_path, 0o600)
            import fcntl
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentError("another agent process already holds the state lock") from exc
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            print(f"CraftPilot runtime agent started{f' for node {config.node_id}' if config.node_id else ''}.", file=sys.stderr)
            RuntimeNodeAgent(config).run_forever()
    except AgentError as exc:
        print(f"CraftPilot runtime agent: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
