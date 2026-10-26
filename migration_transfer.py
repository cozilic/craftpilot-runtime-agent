"""End-to-end encrypted, chunked server migration primitives.

Only ciphertext crosses the master relay. Keys use fresh X25519 ephemeral
material, HKDF-SHA256 and AES-256-GCM with a unique nonce per ordered chunk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CHUNK_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILES = 50_000
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
PROTOCOL = b"craftpilot-server-migration:v1"


class MigrationTransferError(RuntimeError):
    pass


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object, expected: int, label: str) -> bytes:
    if not isinstance(value, str) or len(value) > expected * 2:
        raise MigrationTransferError(f"{label} is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise MigrationTransferError(f"{label} is invalid") from exc
    if len(decoded) != expected:
        raise MigrationTransferError(f"{label} is invalid")
    return decoded


def create_receiver_keypair() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                        serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(private_raw), _b64(public_raw)


def _derive_key(private: X25519PrivateKey, peer_public: bytes, salt: bytes,
                migration_id: str, source_node_id: str, destination_node_id: str) -> bytes:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer_public))
    except ValueError as exc:
        raise MigrationTransferError("migration peer public key is invalid") from exc
    info = b"|".join((PROTOCOL, migration_id.encode("ascii"), source_node_id.encode("ascii"),
                      destination_node_id.encode("ascii")))
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(shared)


def _aad(migration_id: str, source_node_id: str, destination_node_id: str,
         index: int, count: int) -> bytes:
    return b"|".join((PROTOCOL, migration_id.encode("ascii"), source_node_id.encode("ascii"),
                      destination_node_id.encode("ascii"), str(index).encode("ascii"),
                      str(count).encode("ascii")))


def _nonce(prefix: bytes, index: int) -> bytes:
    if len(prefix) != 4 or not 0 <= index < 2**64:
        raise MigrationTransferError("migration chunk nonce is invalid")
    return prefix + index.to_bytes(8, "big")


def _build_archive(server_root: Path, destination: Path) -> tuple[int, int, str]:
    root = server_root.resolve(strict=True)
    if (not root.is_dir() or server_root.is_symlink()
            or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", root.name)):
        raise MigrationTransferError("server data root is not a safe UUID directory")
    total = 0
    count = 0
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3,
                         allowZip64=True) as archive:
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in sorted(directories):
                path = current_path / name
                if path.is_symlink() or not path.is_dir():
                    raise MigrationTransferError("server data contains a symlink or special entry")
                safe_directories.append(name)
                relative = path.relative_to(root).as_posix().rstrip("/") + "/"
                archive.writestr(relative, b"")
            directories[:] = safe_directories
            for name in sorted(filenames):
                path = current_path / name
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise MigrationTransferError("server data changed while being archived") from exc
                if not stat.S_ISREG(info.st_mode):
                    raise MigrationTransferError("server data contains a symlink or special entry")
                total += info.st_size
                count += 1
                if total > MAX_ARCHIVE_BYTES or count > MAX_FILES:
                    raise MigrationTransferError("server data exceeds migration limits")
                archive.write(path, path.relative_to(root).as_posix())
    archive_bytes = destination.stat().st_size
    if not 0 < archive_bytes <= MAX_ARCHIVE_BYTES:
        raise MigrationTransferError("migration archive exceeds its size limit")
    digest = hashlib.sha256()
    with destination.open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return archive_bytes, count, digest.hexdigest()


def export_encrypted(server_root: Path, state_dir: Path, migration_id: str,
                     source_node_id: str, destination_node_id: str, destination_public_key: str,
                     send_manifest: Callable[[dict[str, object]], None],
                     send_chunk: Callable[[int, bytes], None]) -> dict[str, object]:
    """Build and relay an encrypted archive without exposing plaintext to master."""
    receiver_public = _unb64(destination_public_key, 32, "destination public key")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    with tempfile.TemporaryDirectory(prefix="migration-export-", dir=state_dir) as work:
        archive_path = Path(work) / "server.zip"
        archive_bytes, files, digest = _build_archive(server_root, archive_path)
        count = (archive_bytes + CHUNK_BYTES - 1) // CHUNK_BYTES
        ephemeral = X25519PrivateKey.generate()
        ephemeral_public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw,
                                                                 serialization.PublicFormat.Raw)
        salt, prefix = os.urandom(32), os.urandom(4)
        key = _derive_key(ephemeral, receiver_public, salt, migration_id, source_node_id, destination_node_id)
        metadata: dict[str, object] = {
            "protocol": 1,
            "server_id": server_root.resolve(strict=True).name.lower(),
            "source_node_id": source_node_id,
            "destination_node_id": destination_node_id,
            "source_public_key": _b64(ephemeral_public),
            "salt": _b64(salt),
            "nonce_prefix": _b64(prefix),
            "chunk_bytes": CHUNK_BYTES,
            "chunk_count": count,
            "archive_bytes": archive_bytes,
            "archive_sha256": digest,
            "files": files,
        }
        # Register authenticated key material before any chunks arrive. It is
        # public protocol metadata; no secret or plaintext file path is sent.
        send_manifest(metadata)
        cipher = AESGCM(key)
        with archive_path.open("rb") as source:
            for index in range(count):
                plaintext = source.read(CHUNK_BYTES)
                if not plaintext:
                    raise MigrationTransferError("migration archive ended before its declared chunk count")
                send_chunk(index, cipher.encrypt(_nonce(prefix, index), plaintext,
                                                 _aad(migration_id, source_node_id, destination_node_id, index, count)))
            if source.read(1):
                raise MigrationTransferError("migration archive grew while being transferred")
        return metadata


def _safe_extract(archive_path: Path, payload_root: Path) -> int:
    payload_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    expanded = 0
    files = 0
    root = payload_root.resolve(strict=True)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for item in archive.infolist():
                name = item.filename
                member = PurePosixPath(name.rstrip("/"))
                mode = item.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if (not name or "\\" in name or member.is_absolute()
                        or any(part in ("", ".", "..") for part in member.parts)
                        or file_type not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or item.flag_bits & 0x1):
                    raise MigrationTransferError("migration archive contains an unsafe path or file type")
                target = payload_root.joinpath(*member.parts)
                resolved = target.resolve(strict=False)
                if resolved != root and root not in resolved.parents:
                    raise MigrationTransferError("migration archive path escapes staging")
                expanded += item.file_size
                if expanded > MAX_EXPANDED_BYTES:
                    raise MigrationTransferError("migration archive exceeds extraction limits")
                if item.is_dir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                files += 1
                if files > MAX_FILES:
                    raise MigrationTransferError("migration archive contains too many files")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise MigrationTransferError("migration archive contains duplicate paths")
                with archive.open(item, "r") as source, target.open("xb") as output:
                    copied = 0
                    while chunk := source.read(CHUNK_BYTES):
                        copied += len(chunk)
                        if copied > item.file_size:
                            raise MigrationTransferError("migration archive entry exceeded its declared size")
                        output.write(chunk)
                    if copied != item.file_size:
                        raise MigrationTransferError("migration archive entry was truncated")
        return files
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        shutil.rmtree(payload_root, ignore_errors=True)
        if isinstance(exc, MigrationTransferError):
            raise
        raise MigrationTransferError("migration archive is invalid") from exc
    except BaseException:
        shutil.rmtree(payload_root, ignore_errors=True)
        raise


def import_encrypted(data_root: Path, state_dir: Path, migration_id: str,
                     server_id: str, source_node_id: str, destination_node_id: str,
                     receiver_private_key: str, metadata: dict[str, object],
                     get_chunk: Callable[[int], bytes]) -> dict[str, object]:
    """Authenticate/decrypt every ordered chunk, verify ZIP, then leave in staging."""
    expected = metadata
    if (not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", migration_id)
            or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", server_id)
            or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", source_node_id)
            or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", destination_node_id)
            or expected.get("server_id") != server_id.lower()
            or expected.get("source_node_id") != source_node_id.lower()
            or expected.get("destination_node_id") != destination_node_id.lower()):
        raise MigrationTransferError("migration metadata identity does not match the authorized transfer")
    if (expected.get("protocol") != 1 or expected.get("chunk_bytes") != CHUNK_BYTES
            or isinstance(expected.get("chunk_count"), bool)
            or not isinstance(expected.get("chunk_count"), int)
            or not 1 <= int(expected["chunk_count"]) <= (MAX_ARCHIVE_BYTES + CHUNK_BYTES - 1) // CHUNK_BYTES
            or isinstance(expected.get("archive_bytes"), bool)
            or not isinstance(expected.get("archive_bytes"), int)
            or not 1 <= int(expected["archive_bytes"]) <= MAX_ARCHIVE_BYTES
            or not isinstance(expected.get("archive_sha256"), str)
            or len(str(expected["archive_sha256"])) != 64):
        raise MigrationTransferError("migration metadata is invalid")
    try:
        bytes.fromhex(str(expected["archive_sha256"]))
    except ValueError as exc:
        raise MigrationTransferError("migration digest is invalid") from exc
    count, archive_bytes = int(expected["chunk_count"]), int(expected["archive_bytes"])
    source_public = _unb64(expected.get("source_public_key"), 32, "source public key")
    salt = _unb64(expected.get("salt"), 32, "migration salt")
    prefix = _unb64(expected.get("nonce_prefix"), 4, "migration nonce prefix")
    private_raw = _unb64(receiver_private_key, 32, "receiver private key")
    private = X25519PrivateKey.from_private_bytes(private_raw)
    key = _derive_key(private, source_public, salt, migration_id, source_node_id, destination_node_id)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = data_root.resolve(strict=True)
    staging_root = root / ".craftpilot-migrations"
    if staging_root.is_symlink():
        raise MigrationTransferError("migration staging root is unsafe")
    staging_root.mkdir(mode=0o700, exist_ok=True)
    staging = staging_root / migration_id
    if staging.is_symlink():
        raise MigrationTransferError("migration staging directory is unsafe")
    payload_root = staging / "payload"
    metadata_path = staging / "verified.json"
    if metadata_path.is_file() and payload_root.is_dir():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if existing.get("sha256") == expected["archive_sha256"] and existing.get("server_id") == server_id:
            return {"archive_sha256": expected["archive_sha256"], "archive_bytes": archive_bytes,
                    "files": existing.get("files", 0)}
        raise MigrationTransferError("a different migration already occupies the staging directory")
    if staging.exists() and any(staging.iterdir()):
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700, exist_ok=True)
    archive_path = staging / "archive.zip"
    digest, received = hashlib.sha256(), 0
    cipher = AESGCM(key)
    try:
        with archive_path.open("xb") as archive:
            for index in range(count):
                encrypted = get_chunk(index)
                plaintext_size = min(CHUNK_BYTES, archive_bytes - received)
                if plaintext_size <= 0 or len(encrypted) != plaintext_size + 16:
                    raise MigrationTransferError("encrypted chunk size does not match the migration manifest")
                try:
                    plaintext = cipher.decrypt(
                        _nonce(prefix, index), encrypted,
                        _aad(migration_id, source_node_id, destination_node_id, index, count),
                    )
                except InvalidTag as exc:
                    raise MigrationTransferError("migration chunk authentication failed") from exc
                received += len(plaintext)
                if received > archive_bytes:
                    raise MigrationTransferError("migration archive exceeds its declared size")
                digest.update(plaintext)
                archive.write(plaintext)
            archive.flush()
            os.fsync(archive.fileno())
        if received != archive_bytes or digest.hexdigest() != expected["archive_sha256"]:
            raise MigrationTransferError("decrypted migration archive does not match its digest")
        files = _safe_extract(archive_path, payload_root)
        archive_path.unlink()
        marker = {"migration_id": migration_id, "server_id": server_id,
                  "sha256": expected["archive_sha256"], "archive_bytes": archive_bytes, "files": files}
        temporary = staging / ".verified.tmp"
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(marker, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, metadata_path)
        return {"archive_sha256": digest.hexdigest(), "archive_bytes": received, "files": files}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _save_migration_state(state_path: Path, state_dir: Path, saved: dict[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".migration-state-", dir=state_dir)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(saved, output, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, state_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def commit_staged(data_root: Path, state_dir: Path, migration_id: str, server_id: str,
                  expected_sha256: str) -> None:
    if (not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", migration_id)
            or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", server_id)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)):
        raise MigrationTransferError("migration commit identifiers are invalid")
    server_id = server_id.lower()
    root = data_root.resolve(strict=True)
    staging_root = root / ".craftpilot-migrations"
    staging = staging_root / migration_id
    payload = staging / "payload"
    metadata_path = staging / "verified.json"
    final = root / server_id
    if staging_root.is_symlink() or staging.is_symlink() or final.is_symlink():
        raise MigrationTransferError("migration commit path is unsafe")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = state_dir / "migrations.json"
    try:
        saved = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationTransferError("saved migration state is invalid") from exc
    if not isinstance(saved, dict):
        raise MigrationTransferError("saved migration state is invalid")
    committed = saved.setdefault("committed", {})
    if not isinstance(committed, dict):
        raise MigrationTransferError("saved migration state is invalid")
    expected_record = {"server_id": server_id, "sha256": expected_sha256}
    if committed.get(migration_id) == expected_record:
        return
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationTransferError("verified migration metadata is unavailable") from exc
    if metadata.get("migration_id") != migration_id or metadata.get("server_id") != server_id or metadata.get("sha256") != expected_sha256:
        raise MigrationTransferError("verified migration metadata does not match the commit")
    if not isinstance(payload, Path) or payload.is_symlink():
        raise MigrationTransferError("verified migration payload is unsafe")
    final_resolved = final.resolve(strict=False)
    if final_resolved.parent != root:
        raise MigrationTransferError("destination server path escapes the configured root")
    # Persist a recovery intent before the atomic directory rename. If the
    # process dies after rename, a retry can finish the state update safely.
    if committed.get(migration_id) != {"server_id": server_id, "sha256": expected_sha256, "pending": True}:
        committed[migration_id] = {"server_id": server_id, "sha256": expected_sha256, "pending": True}
        _save_migration_state(state_path, state_dir, saved)
    if payload.is_dir():
        if final.exists():
            if any(final.iterdir()):
                raise MigrationTransferError("destination server directory is populated; refusing overwrite")
            final.rmdir()
        os.replace(payload, final)
    elif not final.is_dir():
        raise MigrationTransferError("verified migration payload is missing")
    committed[migration_id] = expected_record
    _save_migration_state(state_path, state_dir, saved)
    shutil.rmtree(staging, ignore_errors=True)


def delete_source(data_root: Path, server_id: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", server_id):
        raise MigrationTransferError("source server identifier is invalid")
    server_id = server_id.lower()
    root = data_root.resolve(strict=True)
    source = root / server_id
    if source.is_symlink() or not source.exists():
        raise MigrationTransferError("source server directory is missing or unsafe")
    resolved = source.resolve(strict=True)
    if resolved.parent != root or not resolved.is_dir():
        raise MigrationTransferError("source server path escapes the configured root")
    shutil.rmtree(resolved)
