#!/usr/bin/env python3
"""CraftPilot Linux runtime-node agent (stdlib only)."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import os
import re
import shutil
import signal
import socket
import ssl
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

import file_tree_index
import migration_transfer
from pathlib import Path
from typing import Any

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
CONTAINER_RE = re.compile(r"^craftpilot-[0-9a-f]{8}$")
DOCKER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$", re.I)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
USER_RE = re.compile(r"^[0-9]{1,10}:[0-9]{1,10}$")
LOG_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")
LOGGER = logging.getLogger("craftpilot.runtime_agent")

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


class CredentialRejectedError(HttpStatusError):
    """The master rejected this node's long-lived credential."""


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
    poll_seconds: int = 5
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
        poll_seconds = _bounded_int("CRAFTPILOT_POLL_SECONDS", 5, 2, 120)
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
        self.file_tree_index: file_tree_index.FileTreeIndex | None = None

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
        if command_type == "file_list":
            return {"success": True, "result_code": "FILE_LISTED",
                    "result": self._list_server_files(server_id, payload.get("path"))}
        if command_type == "file_read":
            return {"success": True, "result_code": "FILE_READ",
                    "result": self._read_server_file(server_id, payload.get("path"))}
        if command_type == "file_download":
            return self._download_server_file(
                server_id, payload.get("path"), payload.get("_file_command_id"),
                payload.get("_file_lease_token"), credential,
            )
        if command_type == "file_mutate":
            return self._mutate_server_file(server_id, payload, credential, command_id)
        if command_type in ("migration_prepare", "migration_stop", "migration_export", "migration_import", "migration_commit", "migration_delete_source"):
            try:
                return self._migration(command_type, server_id, expected_name, payload, credential)
            except migration_transfer.MigrationTransferError as exc:
                raise AgentError(str(exc)) from exc
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
            LOGGER.info("server_snapshot_staged server_id=%s bytes=%d files=%d", server_id, received, file_count)
            return {"success": True, "result_code": "SNAPSHOT_STAGED", "message": expected_sha256,
                    "container_id": str(expected_bytes)}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _migration_request(self, method: str, path: str, body: bytes | dict[str, Any] | None = None,
                           content_type: str = "application/json", command_id: str | None = None,
                           lease_token: str | None = None) -> dict[str, Any] | bytes:
        if not path.startswith("/api/runtime/nodes/") or ".." in path:
            raise AgentError("invalid migration API path")
        assert self.state is not None
        headers = {"Accept": "application/json, application/octet-stream",
                   "Authorization": f"Bearer {self.state['credential']}"}
        if command_id is not None or lease_token is not None:
            if (not isinstance(command_id, str) or not UUID_RE.fullmatch(command_id)
                    or not isinstance(lease_token, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", lease_token)):
                raise AgentError("migration relay lease is invalid")
            headers["X-Migration-Command"] = command_id
            headers["X-Migration-Lease"] = lease_token
        data: bytes | None
        if isinstance(body, dict):
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        else:
            if body is not None:
                headers["Content-Type"] = content_type
            data = body
        request = urllib.request.Request(self.config.master_url + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=60) as response:
                if not 200 <= response.status < 300:
                    raise HttpStatusError(response.status)
                raw = response.read(migration_transfer.CHUNK_BYTES + 65536)
        except urllib.error.HTTPError as exc:
            raise HttpStatusError(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentError("could not relay migration data through the CraftPilot master") from exc
        if response.headers.get_content_type() == "application/octet-stream":
            if len(raw) > migration_transfer.CHUNK_BYTES + 16:
                raise AgentError("relayed migration chunk exceeded the size limit")
            return raw
        if len(raw) > 1024 * 1024:
            raise AgentError("migration metadata exceeded the size limit")
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AgentError("master returned invalid migration metadata") from exc
        if not isinstance(decoded, dict):
            raise AgentError("migration metadata response must be a JSON object")
        return decoded

    def _migration(self, command_type: str, server_id: str, container_name: str,
                   payload: dict[str, Any], credential: str | None) -> dict[str, Any]:
        migration_id = payload.get("migration_id")
        transfer_command_id = payload.get("_migration_command_id")
        transfer_lease_token = payload.get("_migration_lease_token")
        if (not isinstance(transfer_command_id, str) or not UUID_RE.fullmatch(transfer_command_id)
                or not isinstance(transfer_lease_token, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", transfer_lease_token)):
            raise AgentError("migration transfer lease is invalid")
        source_node_id = payload.get("source_node_id")
        destination_node_id = payload.get("destination_node_id")
        if (not isinstance(migration_id, str) or not UUID_RE.fullmatch(migration_id)
                or not isinstance(source_node_id, str) or not UUID_RE.fullmatch(source_node_id)
                or not isinstance(destination_node_id, str) or not UUID_RE.fullmatch(destination_node_id)
                or source_node_id == destination_node_id):
            raise AgentError("migration command has invalid node or migration identifiers")
        if command_type == "migration_prepare":
            if self.config.node_id != destination_node_id or self.state is None or self.state_store is None:
                raise AgentError("migration key preparation is not addressed to this destination node")
            keys = self.state.setdefault("migration_receiver_keys", {})
            if not isinstance(keys, dict):
                raise AgentError("saved migration receiver keys are invalid")
            entry = keys.get(migration_id)
            if not isinstance(entry, dict) or entry.get("server_id") != server_id:
                private_key, public_key = migration_transfer.create_receiver_keypair()
                entry = {"server_id": server_id, "private_key": private_key, "public_key": public_key}
                keys[migration_id] = entry
                self.state_store.save(self.state)
            return {"success": True, "result_code": "MIGRATION_PREPARED", "message": entry["public_key"]}
        if command_type == "migration_stop":
            if self.config.node_id != source_node_id:
                raise AgentError("migration stop is not addressed to this source node")
            # The CraftPilot manager issues this fixed lifecycle operation after
            # authorizing the migration. It must work for older server data that
            # predates the agent's optional RCON credential state.
            force = payload.get("force", False)
            if not isinstance(force, bool):
                raise AgentError("migration stop force flag is invalid")
            result = self._stop(server_id, container_name, force, allow_docker_graceful=True)
            if result["result_code"] not in ("STOPPED", "ALREADY_STOPPED"):
                raise AgentError("source server did not stop before migration")
            return {"success": True, "result_code": "MIGRATION_STOPPED"}
        if command_type == "migration_export":
            if self.config.node_id != source_node_id or not isinstance(payload.get("destination_public_key"), str):
                raise AgentError("migration export is not addressed to this source node")
            def send_manifest(manifest: dict[str, object]) -> None:
                result = self._migration_request("PUT", f"/api/runtime/nodes/{self.config.node_id}/migrations/{migration_id}/manifest", {
                    **manifest, "server_id": server_id, "source_node_id": source_node_id,
                    "destination_node_id": destination_node_id,
                }, command_id=transfer_command_id, lease_token=transfer_lease_token)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise AgentError("master did not accept the encrypted migration manifest")
            def send_chunk(index: int, chunk: bytes) -> None:
                result = self._migration_request("PUT", f"/api/runtime/nodes/{self.config.node_id}/migrations/{migration_id}/chunks/{index}", chunk,
                                                 "application/octet-stream", transfer_command_id, transfer_lease_token)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise AgentError("master did not accept an encrypted migration chunk")
            server_root = self._server_data_path(server_id)
            running = self._inspect(container_name, "{{.State.Running}}")
            if running is not None:
                self._verify_managed(server_id, container_name)
                if running == "true":
                    raise AgentError("refusing to export migration data while the server container is running")
                if running != "false":
                    raise AgentError("source server container state is invalid for migration export")
            result = migration_transfer.export_encrypted(
                server_root, self.config.state_dir, migration_id,
                source_node_id, destination_node_id, payload["destination_public_key"], send_manifest, send_chunk,
            )
            return {"success": True, "result_code": "MIGRATION_EXPORTED",
                    "message": str(result["archive_sha256"]), "container_id": str(result["archive_bytes"])}
        if command_type == "migration_import":
            if self.config.node_id != destination_node_id:
                raise AgentError("migration import is not addressed to this destination node")
            state = self.state
            keys = state.get("migration_receiver_keys") if state is not None else None
            key_entry = keys.get(migration_id) if isinstance(keys, dict) else None
            if not isinstance(key_entry, dict) or key_entry.get("server_id") != server_id or not isinstance(key_entry.get("private_key"), str):
                raise AgentError("migration receiver key is unavailable")
            result_manifest = self._migration_request(
                "GET", f"/api/runtime/nodes/{self.config.node_id}/migrations/{migration_id}/manifest",
                command_id=transfer_command_id, lease_token=transfer_lease_token,
            )
            if not isinstance(result_manifest, dict) or not isinstance(result_manifest.get("manifest"), dict):
                raise AgentError("encrypted migration manifest is unavailable")
            manifest = result_manifest["manifest"]
            if (manifest.get("server_id") != server_id or manifest.get("source_node_id") != source_node_id
                    or manifest.get("destination_node_id") != destination_node_id):
                raise AgentError("encrypted migration manifest is addressed to different nodes or server")
            def get_chunk(index: int) -> bytes:
                value = self._migration_request(
                    "GET", f"/api/runtime/nodes/{self.config.node_id}/migrations/{migration_id}/chunks/{index}",
                    command_id=transfer_command_id, lease_token=transfer_lease_token,
                )
                if not isinstance(value, bytes):
                    raise AgentError("master returned invalid migration ciphertext")
                return value
            result = migration_transfer.import_encrypted(
                self.config.data_root, self.config.state_dir, migration_id, server_id,
                source_node_id, destination_node_id, key_entry["private_key"],
                manifest, get_chunk, data_gid=self.config.data_gid,
            )
            return {"success": True, "result_code": "MIGRATION_STAGED",
                    "message": str(result["archive_sha256"]), "container_id": str(result["archive_bytes"])}
        if command_type == "migration_commit":
            if self.config.node_id != destination_node_id:
                raise AgentError("migration commit is not addressed to this destination node")
            migration_transfer.commit_staged(
                self.config.data_root, self.config.state_dir, migration_id, server_id,
                str(payload.get("archive_sha256", "")),
            )
            return {"success": True, "result_code": "MIGRATION_COMMITTED"}
        if command_type == "migration_delete_source":
            if self.config.node_id != source_node_id:
                raise AgentError("migration source cleanup is not addressed to the source node")
            if not isinstance(payload.get("archive_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", payload["archive_sha256"]):
                raise AgentError("migration cleanup lacks verified archive digest")
            running = self._inspect(container_name, "{{.State.Running}}")
            if running == "true":
                raise AgentError("refusing to delete source data while the server container is running")
            if running is not None:
                self._verify_managed(server_id, container_name)
            migration_transfer.delete_source(self.config.data_root, server_id)
            return {"success": True, "result_code": "MIGRATION_SOURCE_DELETED"}
        raise AgentError("unsupported migration operation")

    def _start(self, server_id: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        existing = self._inspect(name, "{{.State.Running}}")
        if existing is not None:
            self._verify_managed(server_id, name)
        if existing == "true":
            LOGGER.info("server_start_noop server_id=%s reason=already_running", server_id)
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
            LOGGER.info("server_container_create_started server_id=%s image=%s port=%d", server_id, image, port)
            output = self._run(args, timeout=self.config.docker_timeout_seconds)
            candidate_id = output.strip().splitlines()[-1] if output.strip() else ""
            if candidate_id and not DOCKER_ID_RE.fullmatch(candidate_id):
                raise DockerError("Docker returned an invalid container id")
        else:
            self._ensure_rcon(server_id)
            LOGGER.info("server_container_start_requested server_id=%s", server_id)
            self._run(["docker", "start", name], timeout=self.config.docker_timeout_seconds)
        if self._inspect(name, "{{.State.Running}}") != "true":
            raise DockerError("container did not remain running after start")
        LOGGER.info("server_started server_id=%s", server_id)
        return {"success": True, "result_code": "STARTED", "container_id": self._container_id(name)}

    def _console(self, server_id: str, name: str, command: Any) -> dict[str, Any]:
        if (not isinstance(command, str) or not command or len(command) > 16384
                or "\x00" in command or "\n" in command or "\r" in command):
            raise AgentError("console command is invalid")
        if self._inspect(name, "{{.State.Running}}") != "true":
            raise AgentError("server container is not running")
        self._verify_managed(server_id, name)
        self._send_rcon(server_id, name, command)
        LOGGER.info("server_console_command_sent server_id=%s", server_id)
        return {"success": True, "result_code": "CONSOLE_EXECUTED",
                "message": hashlib.sha256(command.encode("utf-8")).hexdigest()}

    def _open_server_file(self, server_id: str, relative: Any, allow_directory: bool = False) -> tuple[Any, str]:
        if not isinstance(relative, str) or len(relative.encode("utf-8")) > 4096 or "\x00" in relative:
            raise AgentError("remote file path is invalid")
        if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
            raise AgentError("remote file paths must be relative")
        segments = []
        for segment in relative.replace("\\", "/").split("/"):
            if segment in ("", "."):
                continue
            if segment == ".." or len(segment.encode("utf-8")) > 255 or re.search(r"[\x00-\x1f\x7f]", segment):
                raise AgentError("remote file path contains an unsafe segment")
            segments.append(segment)
        if len(segments) > 32 or (not allow_directory and not segments):
            raise AgentError("remote file path depth or target is invalid")

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt" or not hasattr(os, "supports_dir_fd") or os.open not in os.supports_dir_fd:
            root = self.config.data_root.resolve(strict=True)
            server_root = root / server_id
            current = server_root
            try:
                if server_root.is_symlink() or not server_root.is_dir():
                    raise AgentError("server data is not a safe directory")
                for segment in segments:
                    current = current / segment
                    if current.is_symlink():
                        raise AgentError("remote file path crosses a symbolic link")
                resolved = current.resolve(strict=True)
                if resolved != server_root.resolve(strict=True) and server_root.resolve(strict=True) not in resolved.parents:
                    raise AgentError("remote file path escapes the server directory")
                if resolved.is_dir():
                    if not allow_directory:
                        raise AgentError("remote file path is a directory")
                    return resolved, "/".join(segments)
                if not resolved.is_file():
                    raise AgentError("remote file is not a regular file")
                return os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "/".join(segments)
            except (OSError, ValueError) as exc:
                raise AgentError("remote file path does not exist or crosses a symbolic link") from exc

        open_descriptors: list[int] = []
        try:
            root_fd = os.open(self.config.data_root, directory_flags)
            open_descriptors.append(root_fd)
            current_fd = os.open(server_id, directory_flags, dir_fd=root_fd)
            open_descriptors.append(current_fd)
            for segment in segments[:-1]:
                current_fd = os.open(segment, directory_flags, dir_fd=current_fd)
                open_descriptors.append(current_fd)
            if segments:
                target_fd = os.open(
                    segments[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=current_fd,
                )
                info = os.fstat(target_fd)
                if stat.S_ISDIR(info.st_mode) and not allow_directory:
                    os.close(target_fd)
                    raise AgentError("remote file path is a directory")
                if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                    os.close(target_fd)
                    raise AgentError("remote file is not a regular file")
            else:
                target_fd = os.dup(current_fd)
            return target_fd, "/".join(segments)
        except (OSError, ValueError) as exc:
            raise AgentError("remote file path does not exist or crosses a symbolic link") from exc
        finally:
            for descriptor in reversed(open_descriptors):
                os.close(descriptor)

    def _mutate_server_file(self, server_id: str, payload: dict[str, Any], credential: str | None,
                            command_id: str | None) -> dict[str, Any]:
        operation = payload.get("operation")
        if operation not in ("write", "upload", "mkdir", "touch", "rename", "delete", "zip", "unzip"):
            raise AgentError("remote file mutation is unsupported")
        relative = payload.get("path")
        if operation == "write":
            content = payload.get("content")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 4 * 1024 * 1024:
                raise AgentError("remote text content is invalid or too large")
            descriptor, normalized = self._open_server_parent(server_id, relative, allow_root=False)
            parent_fd, name = descriptor
            try:
                self._atomic_write_at(parent_fd, name, content.encode("utf-8"))
            finally:
                os.close(parent_fd)
        elif operation in ("mkdir", "touch"):
            name = self._safe_filename(payload.get("name"))
            parent_fd, normalized_parent = self._open_server_directory(server_id, relative)
            try:
                if operation == "mkdir":
                    os.mkdir(name, 0o755, dir_fd=parent_fd)
                else:
                    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644, dir_fd=parent_fd)
                    os.close(descriptor)
                self._apply_shared_permissions(parent_fd, name, directory=(operation == "mkdir"))
            finally:
                os.close(parent_fd)
        elif operation == "rename":
            name = self._safe_filename(payload.get("name"))
            source_parent, source_name = self._open_server_parent(server_id, relative)
            try:
                os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)
                try:
                    os.stat(name, dir_fd=source_parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise AgentError("a file with that name already exists")
                os.rename(source_name, name, src_dir_fd=source_parent, dst_dir_fd=source_parent)
            finally:
                os.close(source_parent)
        elif operation == "delete":
            parent_fd, name = self._open_server_parent(server_id, relative)
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    self._remove_tree_at(parent_fd, name)
                else:
                    os.unlink(name, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        elif operation == "upload":
            upload_result = self._upload_server_file(server_id, relative, payload, credential, command_id)
        elif operation == "zip":
            self._zip_server_path(server_id, relative)
        elif operation == "unzip":
            self._unzip_server_file(server_id, relative)

        result = {"operation": operation, "ok": True}
        if operation == "upload":
            result.update(upload_result)
        return {"success": True, "result_code": "FILE_MUTATED", "result": result}

    @staticmethod
    def _safe_filename(value: Any) -> str:
        if (not isinstance(value, str) or not value or value in (".", "..")
                or len(value.encode("utf-8")) > 255 or "/" in value or "\\" in value
                or re.search(r"[\x00-\x1f\x7f]", value)):
            raise AgentError("remote file name is invalid")
        return value

    def _server_directory_fd(self, server_id: str) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(self.config.data_root, flags)
        try:
            result = os.open(server_id, flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        return result

    def _open_server_directory(self, server_id: str, relative: Any) -> tuple[int, str]:
        _descriptor, normalized = self._open_server_file(server_id, relative, allow_directory=True)
        if isinstance(_descriptor, Path):
            root = self._server_data_path(server_id)
            directory = root.joinpath(*([segment for segment in normalized.split("/") if segment]))
            return os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)), normalized
        return _descriptor, normalized

    def _open_server_parent(self, server_id: str, relative: Any, allow_root: bool = False) -> tuple[int, str]:
        if not isinstance(relative, str) or len(relative.encode("utf-8")) > 4096:
            raise AgentError("remote file path is invalid")
        normalized = self._normalize_server_path(relative, allow_root=allow_root)
        segments = normalized.split("/") if normalized else []
        if not segments:
            raise AgentError("server root is not a file target")
        filename = segments.pop()
        parent = "/".join(segments)
        parent_fd, _ = self._open_server_directory(server_id, parent)
        return parent_fd, filename

    def _normalize_server_path(self, relative: Any, allow_root: bool = True) -> str:
        if not isinstance(relative, str) or len(relative.encode("utf-8")) > 4096 or "\x00" in relative:
            raise AgentError("remote file path is invalid")
        if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
            raise AgentError("remote file paths must be relative")
        segments = []
        for segment in relative.replace("\\", "/").split("/"):
            if segment in ("", "."):
                continue
            if segment == ".." or len(segment.encode("utf-8")) > 255 or re.search(r"[\x00-\x1f\x7f]", segment):
                raise AgentError("remote file path contains an unsafe segment")
            segments.append(segment)
        if len(segments) > 32 or (not allow_root and not segments):
            raise AgentError("remote file path depth or target is invalid")
        return "/".join(segments)

    def _apply_shared_permissions(self, parent_fd: int, name: str, directory: bool) -> None:
        if os.name == "nt":
            return
        flags = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(flags.st_mode):
            raise AgentError("refusing to change permissions on a symbolic link")
        path = f"/proc/self/fd/{parent_fd}/{name}"
        try:
            os.chown(path, -1, self.config.data_gid, follow_symlinks=False)
            os.chmod(path, 0o2770 if directory else 0o660, follow_symlinks=False)
        except OSError as exc:
            raise AgentError("could not apply shared server file permissions") from exc

    def _atomic_write_at(self, parent_fd: int, name: str, content: bytes) -> None:
        try:
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise AgentError("remote write target must be a regular file")
        temporary = f".cp-write-{os.urandom(8).hex()}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o660, dir_fd=parent_fd)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            self._apply_shared_permissions(parent_fd, temporary, directory=False)
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(descriptor)

    def _remove_tree_at(self, parent_fd: int, name: str) -> None:
        directory_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            for entry in os.scandir(directory_fd):
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    self._remove_tree_at(directory_fd, entry.name)
                else:
                    os.unlink(entry.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)

    def _upload_server_file(self, server_id: str, relative: Any, payload: dict[str, Any],
                            credential: str | None, command_id: str | None) -> dict[str, Any]:
        metadata = payload.get("upload")
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("filename"), str)
                or not re.fullmatch(r"[a-f0-9]{48}\.bin", metadata["filename"])
                or not isinstance(metadata.get("bytes"), int) or isinstance(metadata.get("bytes"), bool)
                or not 0 <= metadata["bytes"] <= 2 * 1024 * 1024 * 1024
                or not isinstance(metadata.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])
                or not isinstance(command_id, str) or not UUID_RE.fullmatch(command_id)
                or not isinstance(payload.get("_file_lease_token"), str)
                or not re.fullmatch(r"[A-Za-z0-9]{64}", payload["_file_lease_token"])
                or not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", credential)):
            raise AgentError("remote upload metadata or lease is invalid")
        parsed = urllib.parse.urlsplit(self.config.master_url)
        if (parsed.scheme != "https" or not parsed.hostname or not parsed.netloc
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            raise AgentError("remote upload origin is invalid")
        request_path = f"{parsed.path.rstrip('/')}/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/upload"
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=120,
                                                  context=ssl.create_default_context())
        try:
            connection.request("GET", request_path, headers={
                "Authorization": f"Bearer {credential}",
                "X-Result-Lease": payload["_file_lease_token"],
                "Accept": "application/octet-stream",
            })
            response = connection.getresponse()
            if response.status != 200:
                response.read(4096)
                raise AgentError("master rejected the staged upload")
            if response.getheader("Content-Length") != str(metadata["bytes"]):
                raise AgentError("staged upload size did not match its metadata")

            directory_fd, _normalized_dir = self._open_server_directory(server_id, relative)
            name = self._safe_filename(payload.get("name"))
            try:
                try:
                    existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                if existing is not None and (not payload.get("overwrite") or stat.S_ISDIR(existing.st_mode)):
                    raise AgentError("upload target already exists")
                temporary = f".cp-upload-{os.urandom(8).hex()}"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o660, dir_fd=directory_fd)
                digest = hashlib.sha256()
                received = 0
                try:
                    with os.fdopen(fd, "wb", closefd=False) as output:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            received += len(chunk)
                            if received > metadata["bytes"]:
                                raise AgentError("staged upload exceeds its declared size")
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if received != metadata["bytes"] or digest.hexdigest() != metadata["sha256"]:
                        raise AgentError("staged upload size or digest did not match")
                    self._apply_shared_permissions(directory_fd, temporary, directory=False)
                    os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                    os.close(fd)
            finally:
                os.close(directory_fd)
            return {"bytes": received, "sha256": digest.hexdigest()}
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise AgentError("could not download staged upload from the CraftPilot master") from exc
        finally:
            connection.close()

    def _zip_server_path(self, server_id: str, relative: Any) -> None:
        source = self._server_data_path(server_id).joinpath(*self._normalize_server_path(relative, allow_root=False).split("/"))
        if source.is_symlink() or not source.exists():
            raise AgentError("zip source does not exist or is a symbolic link")
        if source.is_dir():
            iterator = os.walk(source, followlinks=False)
            entries = []
            total = 0
            for current, dirs, files in iterator:
                if any((Path(current) / child).is_symlink() for child in dirs + files):
                    raise AgentError("zip source contains a symbolic link")
                for filename in files:
                    item = Path(current) / filename
                    total += item.stat().st_size
                    entries.append(item)
                    if len(entries) > 20000 or total > 4 * 1024 * 1024 * 1024:
                        raise AgentError("zip source exceeds archive limits")
        else:
            if not source.is_file():
                raise AgentError("zip source must be a regular file or directory")
            entries = [source]
        target = source.with_name(source.name + ".zip")
        if target.exists() or target.is_symlink():
            raise AgentError("zip archive already exists")
        temporary = target.with_name(f".cp-zip-{os.urandom(8).hex()}.tmp")
        try:
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                if source.is_dir():
                    if not entries:
                        archive.writestr(source.name + "/", "")
                    for item in entries:
                        archive.write(item, (Path(source.name) / item.relative_to(source)).as_posix())
                else:
                    archive.write(source, source.name)
            os.chmod(temporary, 0o660)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _unzip_server_file(self, server_id: str, relative: Any) -> None:
        source = self._server_data_path(server_id).joinpath(*self._normalize_server_path(relative, allow_root=False).split("/"))
        if source.is_symlink() or not source.is_file():
            raise AgentError("unzip source must be a regular archive file")
        temporary = source.parent / f".cp-unzip-{os.urandom(8).hex()}"
        temporary.mkdir(mode=0o700)
        try:
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if not members or len(members) > 20000:
                    raise AgentError("zip archive is empty or has too many entries")
                total_bytes = 0
                for member in members:
                    name = member.filename.replace("\\\\", "/")
                    mode = member.external_attr >> 16
                    file_type = stat.S_IFMT(mode)
                    parts = name.rstrip("/").split("/")
                    if (not name or name.startswith("/") or "\\\\" in member.filename
                            or any(part in ("", ".", "..") for part in parts)
                            or (len(parts) > 32) or file_type not in (0, stat.S_IFREG, stat.S_IFDIR)
                            or member.flag_bits & 1):
                        raise AgentError("zip archive contains an unsafe path, symlink, or special file")
                    total_bytes += member.file_size
                    if member.file_size > 2 * 1024 * 1024 * 1024 or total_bytes > 4 * 1024 * 1024 * 1024:
                        raise AgentError("zip archive exceeds extraction size limits")
                    if member.compress_size > 0 and member.file_size / member.compress_size > 200:
                        raise AgentError("zip archive exceeds the decompression ratio limit")
                for member in members:
                    target = (temporary / member.filename.replace("\\\\", "/")).resolve()
                    if temporary.resolve() not in target.parents and target != temporary.resolve():
                        raise AgentError("zip entry escapes temporary extraction directory")
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as src, target.open("xb") as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
            top = [child for child in temporary.iterdir()]
            existing = {child.name for child in source.parent.iterdir()}
            conflicts = existing.intersection(child.name for child in top)
            conflicts.discard(temporary.name)
            if conflicts:
                raise AgentError("zip extraction conflicts with existing files")
            for child in top:
                os.replace(child, source.parent / child.name)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _list_server_files(self, server_id: str, relative: Any) -> dict[str, Any]:
        directory_fd, normalized = self._open_server_file(server_id, relative, allow_directory=True)
        try:
            info = os.fstat(directory_fd) if not isinstance(directory_fd, Path) else directory_fd.stat()

            def scan() -> list[dict[str, Any]]:
                if isinstance(directory_fd, Path):
                    return file_tree_index.FileTreeIndex.scan_path(directory_fd)
                return file_tree_index.FileTreeIndex.scan_directory(directory_fd)

            index = self.file_tree_index
            entries = (index.list_entries(server_id, normalized, info.st_mtime_ns, scan)
                       if index is not None else scan())
            if len(entries) > 1000:
                raise AgentError("directory contains too many entries to display")
            parent = normalized.rpartition("/")[0] if "/" in normalized else ("" if normalized else None)
            return {"path": normalized, "parent": parent, "entries": entries}
        finally:
            if not isinstance(directory_fd, Path):
                os.close(directory_fd)

    def _read_server_file(self, server_id: str, relative: Any) -> dict[str, Any]:
        descriptor, normalized = self._open_server_file(server_id, relative)
        close_descriptor = False
        try:
            if isinstance(descriptor, Path):
                descriptor = os.open(descriptor, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            close_descriptor = True
            info = os.fstat(descriptor)
            max_bytes = 4 * 1024 * 1024
            if info.st_size > max_bytes:
                raise AgentError("remote text file is too large to edit")
            chunks, total = [], 0
            while True:
                chunk = os.read(descriptor, min(65536, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AgentError("remote text file is too large to edit")
                chunks.append(chunk)
            content_bytes = b"".join(chunks)
            if b"\0" in content_bytes[:8192]:
                raise AgentError("remote file is binary and cannot be read as text")
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AgentError("remote file is not valid UTF-8 text") from exc
            return {"path": normalized, "name": normalized.rsplit("/", 1)[-1],
                    "content": content, "bytes": len(content_bytes)}
        finally:
            if close_descriptor:
                os.close(descriptor)

    def _download_server_file(self, server_id: str, relative: Any, command_id: Any,
                              lease_token: Any, credential: str | None) -> dict[str, Any]:
        if (not isinstance(command_id, str) or not UUID_RE.fullmatch(command_id)
                or not isinstance(lease_token, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", lease_token)
                or not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", credential)):
            raise AgentError("remote file download lease is invalid")
        descriptor, normalized = self._open_server_file(server_id, relative)
        close_descriptor = False
        try:
            if isinstance(descriptor, Path):
                descriptor = os.open(descriptor, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            close_descriptor = True
            size = os.fstat(descriptor).st_size
            if size > 2 * 1024 * 1024 * 1024:
                raise AgentError("remote file exceeds the download size limit")
            parsed = urllib.parse.urlsplit(self.config.master_url)
            if (parsed.scheme != "https" or not parsed.hostname or not parsed.netloc
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment):
                raise AgentError("remote file result origin is invalid")
            path = f"{parsed.path.rstrip('/')}/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/result"
            connection = http.client.HTTPSConnection(
                parsed.hostname, parsed.port or 443, timeout=60, context=ssl.create_default_context(),
            )
            digest, sent = hashlib.sha256(), 0
            try:
                connection.putrequest("PUT", path)
                connection.putheader("Authorization", f"Bearer {credential}")
                connection.putheader("X-Result-Lease", lease_token)
                connection.putheader("Content-Type", "application/octet-stream")
                connection.putheader("Content-Length", str(size))
                connection.endheaders()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    digest.update(chunk)
                if sent != size:
                    raise AgentError("remote file changed while it was being downloaded")
                response = connection.getresponse()
                raw = response.read(4097)
                if response.status != 200 or len(raw) > 4096:
                    raise HttpStatusError(response.status)
                try:
                    receipt = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise AgentError("master returned invalid remote file receipt") from exc
                expected_sha = digest.hexdigest()
                if receipt.get("bytes") != size or receipt.get("sha256") != expected_sha:
                    raise AgentError("master did not verify the complete remote file")
                return {"success": True, "result_code": "FILE_DOWNLOADED",
                        "message": expected_sha, "container_id": str(size)}
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                raise AgentError("could not upload the remote file to the CraftPilot master") from exc
            finally:
                connection.close()
        finally:
            if close_descriptor:
                os.close(descriptor)

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

    def _stop(self, server_id: str, name: str, force: bool, allow_docker_graceful: bool = False) -> dict[str, Any]:
        running = self._inspect(name, "{{.State.Running}}")
        if running is None:
            LOGGER.info("server_stop_noop server_id=%s reason=container_missing", server_id)
            return {"success": True, "result_code": "ALREADY_STOPPED"}
        self._verify_managed(server_id, name)
        if running == "false":
            LOGGER.info("server_stop_noop server_id=%s reason=already_stopped", server_id)
            return {"success": True, "result_code": "ALREADY_STOPPED"}
        if running != "true":
            raise DockerError("Docker returned an invalid running state")
        if force:
            LOGGER.warning("server_force_stop_requested server_id=%s", server_id)
            self._run(["docker", "kill", "--signal=KILL", name], timeout=15)
        else:
            LOGGER.info("server_graceful_stop_requested server_id=%s", server_id)
            if allow_docker_graceful:
                # Send SIGTERM directly rather than using `docker stop`, which
                # escalates to SIGKILL automatically when its timeout expires.
                # This supports old servers without RCON while never force-killing.
                self._run(["docker", "kill", "--signal=TERM", name], timeout=15)
            else:
                self._send_rcon(server_id, name, "stop")
            # Docker was configured with a one-second implicit stop timeout
            # when the legacy server container was created. For this authorized
            # migration path, wait without that deadline and do not ever follow
            # SIGTERM with SIGKILL.
            deadline = None if allow_docker_graceful else time.monotonic() + self.config.stop_grace_seconds + 15
            while deadline is None or time.monotonic() < deadline:
                state = self._inspect(name, "{{.State.Status}}")
                if state == "exited":
                    LOGGER.info("server_stopped server_id=%s method=graceful", server_id)
                    return {"success": True, "result_code": "STOPPED"}
                if state in (None, "dead"):
                    raise DockerError("server container disappeared during graceful stop")
                time.sleep(0.5)
            raise DockerError("Minecraft did not finish graceful shutdown before the stop timeout")
        if self._inspect(name, "{{.State.Running}}") == "true":
            raise DockerError("container is still running after stop")
        LOGGER.info("server_stopped server_id=%s method=force", server_id)
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
        self.file_tree_index = file_tree_index.FileTreeIndex(config.data_root, config.state_dir)
        self._last_file_tree_progress = self.file_tree_index.progress
        self._last_reported_file_tree_progress: str | None = None
        self.file_tree_index.set_progress_callback(self._on_file_tree_progress)
        if isinstance(self.runtime, DockerRuntime):
            self.runtime.file_tree_index = self.file_tree_index
        self.state: dict[str, Any] | None = None
        self._opener = urllib.request.build_opener(_NoRedirectHandler())
        self._last_reported_states: dict[str, str] = {}
        self.pending_enrollment: dict[str, str] | None = None
        self.credential_revoked = False
        self.poll_seconds = config.poll_seconds
        self.node_settings: dict[str, Any] = {}
        if isinstance(self.runtime, DockerRuntime):
            self.runtime.state_store = self.store

    def ensure_registered(self) -> bool:
        state = self.store.load()
        if state is not None and state.get("credential_revoked") is True:
            return self._resume_revoked_node(state)
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
            self._clear_registration_token()
            return True
        return self._request_dynamic_enrollment()

    def _resume_revoked_node(self, saved_state: dict[str, Any]) -> bool:
        node_id = saved_state.get("node_id")
        if not isinstance(node_id, str) or not UUID_RE.fullmatch(node_id):
            raise AgentError("saved revoked-node state is invalid")
        if self.config.node_id is not None and self.config.node_id != node_id:
            raise AgentError("saved revoked-node state does not match the configured node")
        self._set_node_id(node_id)
        self.state = None
        self.pending_enrollment = None
        self.credential_revoked = True
        if isinstance(self.runtime, DockerRuntime):
            self.runtime.state = None

        token = self.config.registration_token
        if token is None or not 32 <= len(token) <= 128:
            return False

        try:
            result = self._request("POST", f"/api/runtime/nodes/{node_id}/register",
                                   {"registration_token": token})
        except HttpStatusError as exc:
            if exc.status != 401:
                raise
            LOGGER.warning("runtime_agent_reenrollment_token_rejected node_id=%s; waiting for a fresh token", node_id)
            return False
        except AgentError as exc:
            LOGGER.warning("runtime_agent_reenrollment_failed node_id=%s error=%s", node_id, str(exc)[:220])
            return False

        credential = result.get("credential")
        if result.get("node_id") != node_id or not isinstance(credential, str) or not re.fullmatch(r"[A-Za-z0-9]{64}", credential):
            raise AgentError("master returned an invalid registration response")
        master_url = result.get("master_url", self.config.master_url)
        if not isinstance(master_url, str) or master_url.rstrip("/") != self.config.master_url:
            raise AgentError("registration response master URL does not match configured HTTPS origin")

        self.state = {"node_id": node_id, "credential": credential, "sequence": 0}
        if isinstance(self.runtime, DockerRuntime):
            self.runtime.state = self.state
        self.store.save(self.state)
        self.credential_revoked = False
        self._clear_registration_token()
        LOGGER.info("runtime_agent_online node_id=%s", node_id)
        return True

    def _clear_registration_token(self) -> None:
        from dataclasses import replace
        self.config = replace(self.config, registration_token=None)
        self.runtime.config = self.config
        os.environ.pop("CRAFTPILOT_REGISTRATION_TOKEN", None)

    def _mark_credential_revoked(self) -> None:
        node_id = self.config.node_id
        if self.state is not None:
            node_id = self.state.get("node_id", node_id)
        if isinstance(node_id, str) and UUID_RE.fullmatch(node_id):
            self.store.save({"node_id": node_id, "credential_revoked": True})
        self.state = None
        self.pending_enrollment = None
        if isinstance(self.runtime, DockerRuntime):
            self.runtime.state = None
        self.credential_revoked = True
        LOGGER.error("runtime_agent_credential_revoked node_id=%s action=waiting_for_manual_reenrollment",
                     node_id or "unknown")

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
        LOGGER.info("enrollment_waiting request_id=%s", request_id)
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
        self._apply_node_settings(claim.get("node_settings"))
        command = claim.get("command")
        if command is None:
            self._report_servers(sequence)
            self._sync_file_tree_cache()
            return
        if not isinstance(command, dict):
            raise AgentError("master returned an invalid command")
        command_id, lease_token = command.get("id"), command.get("lease_token")
        command_type, payload = command.get("type"), command.get("payload")
        if (not isinstance(command_id, str) or not UUID_RE.fullmatch(command_id) or not isinstance(lease_token, str)
                or len(lease_token) != 64 or command_type not in ("start", "stop", "snapshot", "console", "file_list", "file_read", "file_download", "file_mutate", "migration_prepare", "migration_stop", "migration_export", "migration_import", "migration_commit", "migration_delete_source") or not isinstance(payload, dict)):
            raise AgentError("master returned an invalid leased command")
        command_server_id = payload.get("server_id")
        if not isinstance(command_server_id, str) or not UUID_RE.fullmatch(command_server_id):
            command_server_id = "invalid"
        else:
            self._clear_unassigned_server(command_server_id)
        LOGGER.info("command_received command_id=%s type=%s server_id=%s", command_id, command_type, command_server_id)
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
            if command_type.startswith("migration_") or command_type in ("file_download", "file_mutate"):
                payload = dict(payload)
                payload["_migration_command_id" if command_type.startswith("migration_") else "_file_command_id"] = command_id
                payload["_migration_lease_token" if command_type.startswith("migration_") else "_file_lease_token"] = lease_token
            result = self.runtime.execute(command_type, payload, credential=self.state["credential"], command_id=command_id)
            if command_type == "file_mutate" and result.get("success"):
                operation = payload.get("operation")
                path = payload.get("path")
                if operation in ("write", "delete", "zip", "unzip", "rename") and isinstance(path, str):
                    self.file_tree_index.invalidate(payload["server_id"], path.rpartition("/")[0])
                elif operation in ("mkdir", "touch", "upload") and isinstance(path, str):
                    self.file_tree_index.invalidate(payload["server_id"], path)
            if command_type in ("file_list", "file_read", "file_mutate") and result.get("success"):
                file_result = result.pop("result", None)
                if not isinstance(file_result, dict):
                    raise AgentError("runtime returned an invalid remote file result")
                self._request(
                    "POST", f"/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/result",
                    {"lease_token": lease_token, "result": file_result}, credential=True,
                )
        except (AgentError, DockerError) as exc:
            LOGGER.error("command_execution_failed command_id=%s type=%s server_id=%s error=%s",
                         command_id, command_type, command_server_id, str(exc)[:300])
            result = {"success": False, "result_code": "RUNTIME_ERROR", "message": str(exc)[:300]}
        if command_type in ("start", "stop", "migration_stop") and result.get("success"):
            server_id = payload.get("server_id")
            if isinstance(server_id, str) and UUID_RE.fullmatch(server_id):
                stopped_servers = self.state.setdefault("intentional_stops", {})
                if not isinstance(stopped_servers, dict) or any(
                    not isinstance(key, str) or not UUID_RE.fullmatch(key)
                    or not isinstance(value, str) or not UUID_RE.fullmatch(value)
                    for key, value in stopped_servers.items()
                ):
                    raise AgentError("saved intentional stop state is invalid")
                if command_type in ("stop", "migration_stop"):
                    stopped_servers[server_id] = command_id
                    self.store.save(self.state)
        self._request("POST", f"/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/ack",
                      {"lease_token": lease_token, **result}, credential=True)
        if result.get("success") and command_type in ("start", "stop", "snapshot", "migration_export", "migration_import", "migration_commit", "migration_delete_source"):
            self._clear_unassigned_server(command_server_id)
        if result.get("success"):
            LOGGER.info("command_completed command_id=%s type=%s server_id=%s result=%s",
                        command_id, command_type, command_server_id, result.get("result_code", "unknown"))
        self._report_servers(sequence)
        self._sync_file_tree_cache()

    def _on_file_tree_progress(self, stage: str) -> None:
        if stage == self._last_file_tree_progress:
            return
        self._last_file_tree_progress = stage
        if stage != "ready":
            LOGGER.info("file_tree_progress stage=%s", stage)
            self._report_file_tree_progress(stage)

    def _report_file_tree_progress(self, stage: str) -> None:
        if stage == self._last_reported_file_tree_progress or self.state is None:
            return
        try:
            self._request(
                "POST", f"/api/runtime/nodes/{self.config.node_id}/file-tree/progress",
                {"stage": stage}, credential=True,
            )
            self._last_reported_file_tree_progress = stage
        except (AgentError, DockerError, OSError, TimeoutError) as exc:
            LOGGER.warning("file_tree_progress_report_failed error=%s", str(exc)[:200])

    def _sync_file_tree_cache(self) -> None:
        if self.file_tree_index is None or self.state is None:
            return
        try:
            assigned_response = self._request(
                "GET", f"/api/runtime/nodes/{self.config.node_id}/servers", {}, credential=True,
            )
            assigned_ids = assigned_response.get("servers")
            if (not isinstance(assigned_ids, list)
                    or any(not isinstance(server_id, str) or not UUID_RE.fullmatch(server_id) for server_id in assigned_ids)):
                raise AgentError("master returned an invalid server assignment list")
            assigned = set(assigned_ids) - set(self._unassigned_server_ids())
            while True:
                pending = self.file_tree_index.pending_sync_updates(assigned, limit=20)
                if not pending:
                    self._report_file_tree_progress(self.file_tree_index.progress)
                    return
                stage = self.file_tree_index.progress
                if stage == "ready":
                    self._report_file_tree_progress("indexing")
                updates = [(key, revision) for key, revision, _ in pending]
                payload = {"directories": [update for _, _, update in pending]}
                self._request(
                    "POST", f"/api/runtime/nodes/{self.config.node_id}/file-tree", payload, credential=True,
                )
                self.file_tree_index.acknowledge_sync_updates(updates)
        except HttpStatusError as exc:
            if exc.status == 401:
                raise CredentialRejectedError(exc.status) from None
            LOGGER.warning("file_tree_sync_failed status=%d", exc.status)
        except (AgentError, DockerError, OSError, TimeoutError) as exc:
            LOGGER.warning("file_tree_sync_failed error=%s", str(exc)[:220])

    def _report_servers(self, sequence: int) -> None:
        assert self.state is not None
        cursors = self.state.setdefault("log_cursors", {})
        if not isinstance(cursors, dict):
            raise AgentError("saved runtime log cursors are invalid")
        unassigned_servers = self._unassigned_server_ids()
        try:
            server_ids = self._assigned_server_ids()
            assigned_response = self._request(
                "GET", f"/api/runtime/nodes/{self.config.node_id}/servers", {}, credential=True,
            )
            assigned_server_ids = assigned_response.get("servers")
            artifact_checks = assigned_response.get("artifacts", {})
            if (not isinstance(assigned_server_ids, list)
                    or any(not isinstance(server_id, str) or not UUID_RE.fullmatch(server_id)
                           for server_id in assigned_server_ids)):
                raise AgentError("master returned an invalid server assignment list")
            if not isinstance(artifact_checks, dict):
                raise AgentError("master returned invalid artifact requirements")
            for server_id, check in artifact_checks.items():
                if (not isinstance(server_id, str) or not UUID_RE.fullmatch(server_id)
                        or not isinstance(check, dict)
                        or not isinstance(check.get("jar"), str)
                        or not check["jar"] or check["jar"] in (".", "..")
                        or "/" in check["jar"] or "\\" in check["jar"]
                        or any(ord(character) < 32 or ord(character) == 127 for character in check["jar"])
                        or isinstance(check.get("min_bytes"), bool)
                        or not isinstance(check.get("min_bytes"), int)
                        or not 0 <= check["min_bytes"] <= 2**63 - 1):
                    raise AgentError("master returned an invalid artifact requirement")
            if any(server_id not in assigned_server_ids for server_id in artifact_checks):
                raise AgentError("master returned artifact requirements for an unassigned server")
        except HttpStatusError as exc:
            if exc.status == 401:
                raise
            LOGGER.warning("server_assignments_unavailable status=%d", exc.status)
            return
        except (AgentError, DockerError, OSError, TimeoutError) as exc:
            LOGGER.warning("server_assignments_unavailable error=%s", str(exc)[:220])
            return
        assigned_server_set = set(assigned_server_ids)
        cleared_server_ids = [server_id for server_id in unassigned_servers if server_id not in assigned_server_set]
        if cleared_server_ids:
            unassigned_servers[:] = [server_id for server_id in unassigned_servers if server_id in assigned_server_set]
            self.store.save(self.state)
            for server_id in cleared_server_ids:
                LOGGER.info("server_report_resumed server_id=%s reason=assignment_removed", server_id)
        staged_server_ids = self._staged_server_ids()
        server_ids = sorted((set(server_ids) | set(staged_server_ids)) & assigned_server_set)
        for server_id in server_ids:
            if server_id in unassigned_servers:
                continue
            cursor = cursors.get(server_id)
            if cursor is not None and (not isinstance(cursor, str) or not LOG_TIMESTAMP_RE.fullmatch(cursor)):
                raise AgentError("saved runtime log cursor is invalid")
            try:
                report = self._server_report(server_id, sequence, cursor, artifact_checks.get(server_id))
                if report is None:
                    continue
                self._request("POST", f"/api/runtime/nodes/{self.config.node_id}/servers/report", report, credential=True)
                previous_status = self._last_reported_states.get(server_id)
                if report["status"] != previous_status:
                    LOGGER.info("server_status_reported server_id=%s previous=%s current=%s",
                                server_id, previous_status or "unknown", report["status"])
                    self._last_reported_states[server_id] = report["status"]
                if report["logs"]:
                    cursors[server_id] = report["logs"][-1]["ts"]
                    self.store.save(self.state)
            except HttpStatusError as exc:
                if exc.status == 401:
                    raise
                if exc.status == 404:
                    unassigned_servers.append(server_id)
                    self.store.save(self.state)
                    LOGGER.warning("server_report_suppressed server_id=%s reason=not_assigned", server_id)
                    continue
                LOGGER.warning("server_report_rejected server_id=%s status=%d", server_id, exc.status)
            except (AgentError, DockerError, OSError, TimeoutError) as exc:
                LOGGER.warning("server_report_failed server_id=%s error=%s", server_id, str(exc)[:220])
                continue

    def _unassigned_server_ids(self) -> list[str]:
        assert self.state is not None
        server_ids = self.state.setdefault("unassigned_servers", [])
        if (not isinstance(server_ids, list)
                or any(not isinstance(server_id, str) or not UUID_RE.fullmatch(server_id) for server_id in server_ids)):
            raise AgentError("saved unassigned server state is invalid")
        return server_ids

    def _staged_server_ids(self) -> list[str]:
        try:
            root = self.config.data_root.resolve(strict=True)
            if not root.is_dir():
                return []
            return sorted(
                entry.name.lower() for entry in root.iterdir()
                if UUID_RE.fullmatch(entry.name) and not entry.is_symlink() and entry.is_dir()
            )
        except OSError:
            return []

    def _clear_unassigned_server(self, server_id: str) -> None:
        unassigned_servers = self._unassigned_server_ids()
        if server_id in unassigned_servers:
            unassigned_servers.remove(server_id)
            self.store.save(self.state)
            LOGGER.info("server_report_resumed server_id=%s reason=command_received", server_id)

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

    def _server_report(
        self, server_id: str, sequence: int, log_since: str | None = None,
        artifact_check: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not UUID_RE.fullmatch(server_id):
            return None
        name = f"craftpilot-{server_id.replace('-', '')[:8]}"
        if not CONTAINER_RE.fullmatch(name):
            return None
        running = self.runtime._inspect(name, "{{.State.Running}}")
        if running is None:
            try:
                self.runtime._server_data_path(server_id)
            except (AgentError, OSError):
                return None
            return {
                "sequence": sequence, "server_id": server_id, "status": "OFFLINE",
                "cpu_percent": None, "memory_used_mb": None, "memory_limit_mb": None,
                "uptime_seconds": None, "players_online": None, "players_max": None,
                "disk_used_mb": self.runtime._server_disk_used_mb(server_id),
                "artifact_present": self._artifact_present(server_id, artifact_check), "logs": [],
            }
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
                "players_online": None, "players_max": None, "disk_used_mb": disk_used_mb,
                "artifact_present": self._artifact_present(server_id, artifact_check), "logs": logs}

    def _artifact_present(self, server_id: str, check: dict[str, Any] | None) -> bool | None:
        if check is None:
            return None
        try:
            descriptor, _normalized = self.runtime._open_server_file(server_id, check["jar"])
            if isinstance(descriptor, Path):
                info = descriptor.lstat()
                return stat.S_ISREG(info.st_mode) and info.st_size >= check["min_bytes"]
            try:
                info = os.fstat(descriptor)
                return stat.S_ISREG(info.st_mode) and info.st_size >= check["min_bytes"]
            finally:
                os.close(descriptor)
        except FileNotFoundError:
            return False
        except (AgentError, OSError, KeyError, TypeError):
            return None

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
        self.file_tree_index.start()
        if self.ensure_registered():
            LOGGER.info("runtime_agent_online node_id=%s", self.config.node_id)
        while True:
            started = time.monotonic()
            try:
                if self.credential_revoked or self.state is None:
                    self.ensure_registered()
                if not self.credential_revoked:
                    if self.state is not None and self.state.get("enrollment_request_id"):
                        if self.poll_enrollment_once():
                            LOGGER.info("runtime_agent_online node_id=%s", self.config.node_id)
                    else:
                        self.poll_once()
            except CredentialRejectedError:
                self._mark_credential_revoked()
            except HttpStatusError as exc:
                if exc.status == 409:
                    LOGGER.warning("heartbeat_lease_conflict retrying_with_new_sequence")
                else:
                    LOGGER.warning("master_request_failed status=%d retrying", exc.status)
            except (AgentError, OSError, TimeoutError) as exc:
                LOGGER.error("agent_iteration_failed error=%s", str(exc)[:300])
            delay = max(0.0, (15 if self.credential_revoked or self.state is None or self.state.get("enrollment_request_id")
                              else self.poll_seconds) - (time.monotonic() - started))
            time.sleep(delay)

    def _apply_node_settings(self, settings: Any) -> None:
        if not isinstance(settings, dict):
            return
        poll_seconds = settings.get("poll_interval_seconds")
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int) or not 2 <= poll_seconds <= 120:
            raise AgentError("master returned an invalid node polling interval")
        name = settings.get("name")
        max_servers = settings.get("max_servers")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise AgentError("master returned an invalid node name")
        if isinstance(max_servers, bool) or not isinstance(max_servers, int) or not 1 <= max_servers <= 1000:
            raise AgentError("master returned an invalid node capacity")
        self.node_settings = {"name": name, "max_servers": max_servers, "poll_interval_seconds": poll_seconds}
        self.poll_seconds = poll_seconds

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
            status = exc.code
            exc.close()
            if credential and status == 401:
                raise CredentialRejectedError(status) from None
            raise HttpStatusError(status) from None
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
    logging.basicConfig(
        level=os.environ.get("CRAFTPILOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
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
            LOGGER.info("runtime_agent_started node_id=%s", config.node_id or "pending_enrollment")
            agent = RuntimeNodeAgent(config)
            agent.run_forever()
    except AgentError as exc:
        LOGGER.error("runtime_agent_fatal error=%s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if "agent" in locals():
            agent.file_tree_index.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
