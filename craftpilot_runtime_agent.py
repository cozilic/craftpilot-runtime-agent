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
DEFAULT_IMAGES = frozenset(
    {
        "eclipse-temurin:8-jre",
        "eclipse-temurin:11-jre",
        "eclipse-temurin:17-jre",
        "eclipse-temurin:21-jre",
        "eclipse-temurin:25-jre",
    }
)


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
    node_id: str
    registration_token: str | None
    state_dir: Path
    data_root: Path
    images: frozenset[str]
    network: str = "craftpilot-servers"
    poll_seconds: int = 30
    stop_grace_seconds: int = 30
    docker_timeout_seconds: int = 45

    @classmethod
    def from_env(cls) -> "Config":
        master = os.environ.get("CRAFTPILOT_MASTER_URL", "").rstrip("/")
        node_id = os.environ.get("CRAFTPILOT_NODE_ID", "").lower()
        token = os.environ.get("CRAFTPILOT_REGISTRATION_TOKEN") or None
        state_dir = Path(os.environ.get("CRAFTPILOT_AGENT_STATE_DIR", "/var/lib/craftpilot-agent"))
        data_root = Path(os.environ.get("CRAFTPILOT_SERVER_DATA_ROOT", "/srv/craftpilot/servers"))
        configured_images = os.environ.get("CRAFTPILOT_ALLOWED_IMAGES", "")
        images = frozenset(item.strip() for item in configured_images.split(",") if item.strip()) or DEFAULT_IMAGES
        network = os.environ.get("CRAFTPILOT_DOCKER_NETWORK", "craftpilot-servers")
        poll_seconds = _bounded_int("CRAFTPILOT_POLL_SECONDS", 30, 5, 120)
        stop_grace = _bounded_int("CRAFTPILOT_STOP_GRACE_SECONDS", 30, 0, 45)

        if not UUID_RE.fullmatch(node_id):
            raise AgentError("CRAFTPILOT_NODE_ID must be a node UUID")
        if not master.startswith("https://"):
            raise AgentError("CRAFTPILOT_MASTER_URL must use HTTPS")
        if not NAME_RE.fullmatch(network):
            raise AgentError("CRAFTPILOT_DOCKER_NETWORK is invalid")
        if not images:
            raise AgentError("CRAFTPILOT_ALLOWED_IMAGES must not be empty")

        return cls(master, node_id, token, state_dir, data_root, images, network, poll_seconds, stop_grace)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise AgentError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise AgentError(f"{name} must be between {minimum} and {maximum}")
    return value


class DockerRuntime:
    """Executes only fixed Docker operations; never invokes a host shell."""

    def __init__(self, config: Config, runner=subprocess.run):
        self.config = config
        self.runner = runner

    def execute(
        self,
        command_type: str,
        payload: dict[str, Any],
        credential: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
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
        raise AgentError("unsupported command type")

    def _stage_snapshot(
        self,
        server_id: str,
        payload: dict[str, Any],
        credential: str | None,
        command_id: str | None,
    ) -> dict[str, Any]:
        metadata = payload.get("snapshot")
        download_url = payload.get("download_url")
        if not isinstance(metadata, dict) or not isinstance(download_url, str):
            raise AgentError("snapshot command metadata is invalid")
        expected_bytes = metadata.get("bytes")
        expected_sha256 = metadata.get("sha256")
        if (isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int)
                or not 1 <= expected_bytes <= 4 * 1024 * 1024 * 1024
                or not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)):
            raise AgentError("snapshot size or digest is invalid")
        parsed = urllib.parse.urlsplit(download_url)
        master = urllib.parse.urlsplit(self.config.master_url)
        filename = metadata.get("filename")
        expected_filename = f"{server_id}-{command_id}.zip" if isinstance(command_id, str) else ""
        expected_path = (
            f"/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/snapshot/{filename}"
            if isinstance(filename, str) and command_id is not None else ""
        )
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
        archive_path = staging / "snapshot.zip"
        extract_path = staging / "extracted"
        extract_path.mkdir(mode=0o700)
        try:
            request = urllib.request.Request(download_url, headers={
                "Accept": "application/zip",
                "Authorization": f"Bearer {credential}",
            })
            digest = hashlib.sha256()
            received = 0
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

            extracted_bytes = 0
            file_count = 0
            with zipfile.ZipFile(archive_path) as archive:
                for item in archive.infolist():
                    name = item.filename
                    mode = item.external_attr >> 16
                    member = Path(name)
                    file_type = stat.S_IFMT(mode)
                    if (not name or member.is_absolute() or "\\" in name
                            or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                            or (file_type not in (0, stat.S_IFREG, stat.S_IFDIR))
                            or (item.flag_bits & 0x1)):
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
            os.replace(extract_path, destination)
            return {
                "success": True,
                "result_code": "SNAPSHOT_STAGED",
                "message": expected_sha256,
                "container_id": str(expected_bytes),
            }
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _start(self, server_id: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        existing = self._inspect(name, "{{.State.Running}}")
        if existing is not None:
            self._verify_managed(server_id, name)
        if existing == "true":
            return {"success": True, "result_code": "ALREADY_RUNNING", "container_id": self._container_id(name)}

        if existing is None:
            data_path = self._server_data_path(server_id)
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

            network = spec.get("network")
            if not isinstance(network, str) or network != self.config.network or not NAME_RE.fullmatch(network):
                raise AgentError("container network does not match the configured node network")
            mount_path = spec.get("mount_path")
            if mount_path != "/data":
                raise AgentError("container mount path is not supported")
            self._ensure_network()
            args = [
                "docker", "run", "--detach", "--name", name,
                "--label", "craftpilot.managed=1",
                "--label", f"craftpilot.server_id={server_id}",
                "--network", network,
                "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
                "--cpus", cpus, "--restart", "no", "--user", container_user,
                "--volume", f"{data_path}:/data:rw",
                "--publish", f"{port}:25565/tcp",
                image, *command,
            ]
            output = self._run(args, timeout=self.config.docker_timeout_seconds)
            candidate_id = output.strip().splitlines()[-1] if output.strip() else ""
            if candidate_id and not DOCKER_ID_RE.fullmatch(candidate_id):
                raise DockerError("Docker returned an invalid container id")
        else:
            self._run(["docker", "start", name], timeout=self.config.docker_timeout_seconds)

        if self._inspect(name, "{{.State.Running}}") != "true":
            raise DockerError("container did not remain running after start")
        return {"success": True, "result_code": "STARTED", "container_id": self._container_id(name)}

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
            self._run(
                ["docker", "stop", "--time", str(self.config.stop_grace_seconds), name],
                timeout=self.config.stop_grace_seconds + 15,
            )
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

    @staticmethod
    def _container_command(value: Any) -> list[str]:
        if (not isinstance(value, list) or len(value) != 3
                or value[0] != "sh" or value[1] != "-c"
                or not isinstance(value[2], str) or not value[2]
                or len(value[2]) > 16384 or "\0" in value[2]):
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
        result = self.runner(
            ["docker", "network", "inspect", self.config.network],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            return
        if "no such network" not in result.stderr.lower() and "not found" not in result.stderr.lower():
            raise DockerError("Docker network inspection failed")
        created = self.runner(
            ["docker", "network", "create", "--driver", "bridge", self.config.network],
            capture_output=True, text=True, timeout=15, check=False,
        )
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
        if (not isinstance(labels, dict)
                or labels.get("craftpilot.managed") != "1"
                or labels.get("craftpilot.server_id") != server_id):
            raise AgentError("refusing to manage a container not owned by this server")

    def _inspect(self, name: str, template: str) -> str | None:
        result = self.runner(
            ["docker", "inspect", "--format", template, name],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if "no such object" in result.stderr.lower() or "no such container" in result.stderr.lower():
            return None
        raise DockerError("Docker container inspection failed")

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
            if info.st_mode & 0o077:
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
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, separators=(",", ":"))
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary, self.path)
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
        self.config = config
        self.runtime = runtime or DockerRuntime(config)
        self.store = store or StateStore(config.state_dir)
        self.state: dict[str, Any] | None = None
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def ensure_registered(self) -> None:
        state = self.store.load()
        if state is not None:
            credential = state.get("credential")
            sequence = state.get("sequence")
            if (state.get("node_id") != self.config.node_id
                    or not isinstance(credential, str)
                    or not re.fullmatch(r"[A-Za-z0-9]{64}", credential)
                    or isinstance(sequence, bool)
                    or not isinstance(sequence, int)
                    or not 0 <= sequence <= 2147483647):
                raise AgentError("saved node credentials do not match the configured node")
            self.state = state
            return
        token = self.config.registration_token
        if not token or not 32 <= len(token) <= 128:
            raise AgentError("registration token is required for first enrollment")
        result = self._request(
            "POST", f"/api/runtime/nodes/{self.config.node_id}/register",
            {"registration_token": token},
        )
        credential = result.get("credential")
        if result.get("node_id") != self.config.node_id or not isinstance(credential, str) or len(credential) != 64:
            raise AgentError("master returned an invalid registration response")
        self.state = {"node_id": self.config.node_id, "credential": credential, "sequence": 0}
        self.store.save(self.state)
        os.environ.pop("CRAFTPILOT_REGISTRATION_TOKEN", None)

    def poll_once(self) -> None:
        if self.state is None:
            self.ensure_registered()
        assert self.state is not None
        sequence = self.state["sequence"] + 1
        if sequence > 2147483647:
            raise AgentError("heartbeat sequence exhausted; re-enroll this node")
        self.state["sequence"] = sequence
        self.store.save(self.state)
        claim = self._request(
            "POST", f"/api/runtime/nodes/{self.config.node_id}/commands/claim",
            {"sequence": sequence}, credential=True,
        )
        command = claim.get("command")
        if command is None:
            return
        if not isinstance(command, dict):
            raise AgentError("master returned an invalid command")
        command_id = command.get("id")
        lease_token = command.get("lease_token")
        command_type = command.get("type")
        payload = command.get("payload")
        if (not isinstance(command_id, str) or not UUID_RE.fullmatch(command_id)
                or not isinstance(lease_token, str) or len(lease_token) != 64
                or command_type not in ("start", "stop", "snapshot") or not isinstance(payload, dict)):
            raise AgentError("master returned an invalid leased command")

        try:
            result = self.runtime.execute(
                command_type,
                payload,
                credential=self.state["credential"],
                command_id=command_id,
            )
        except (AgentError, DockerError) as exc:
            result = {"success": False, "result_code": "RUNTIME_ERROR", "message": str(exc)[:300]}
        body = {"lease_token": lease_token, **result}
        self._request(
            "POST", f"/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/ack", body,
            credential=True,
        )

    def run_forever(self) -> None:
        self.ensure_registered()
        while True:
            started = time.monotonic()
            try:
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
            delay = max(0.0, self.config.poll_seconds - (time.monotonic() - started))
            time.sleep(delay)

    def _request(self, method: str, path: str, body: dict[str, Any], credential: bool = False) -> dict[str, Any]:
        if not path.startswith("/api/") or ".." in path:
            raise AgentError("invalid master API path")
        assert self.state is not None or not credential
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if credential:
            assert self.state is not None
            headers["Authorization"] = f"Bearer {self.state['credential']}"
        request = urllib.request.Request(
            self.config.master_url + path,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                if response.status != 200:
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
            print(f"CraftPilot runtime agent for node {config.node_id} started.", file=sys.stderr)
            RuntimeNodeAgent(config).run_forever()
    except AgentError as exc:
        print(f"CraftPilot runtime agent: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
