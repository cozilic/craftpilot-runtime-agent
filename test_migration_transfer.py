"""Docker-free security tests for encrypted server migration transfers."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

import migration_transfer as transfer

MIGRATION_ID = "ef62a0d4-599e-4810-b3e7-5d78015e230a"
SERVER_ID = "e7b4b542-70b0-4cf7-a2ac-2a6597de165d"
SOURCE_NODE = "c4f6babc-0d57-4ab7-a71f-a6358498335a"
DESTINATION_NODE = "fc94aeda-4379-477a-8bb8-47ad51c0ea76"


class MigrationTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_root = self.root / SERVER_ID
        self.source_root.mkdir()
        (self.source_root / "server.properties").write_text("motd=hello\n", encoding="utf-8")
        (self.source_root / "world").mkdir()
        (self.source_root / "world" / "level.dat").write_bytes(b"world-data" * 4096)
        self.source_state = self.root / "source-state"
        self.destination_state = self.root / "destination-state"
        self.destination_root = self.root / "destination"
        self.receiver_private_key, self.receiver_public_key = transfer.create_receiver_keypair()
        self.metadata: dict[str, object] = {}
        self.chunks: list[bytes] = []

        def save_manifest(value: dict[str, object]) -> None:
            self.metadata.update(value)

        def save_chunk(_index: int, value: bytes) -> None:
            self.chunks.append(value)

        self.export_result = transfer.export_encrypted(
            self.source_root, self.source_state, MIGRATION_ID, SOURCE_NODE, DESTINATION_NODE,
            self.receiver_public_key, save_manifest, save_chunk,
        )

    def _import(self, get_chunk=None):
        callback = get_chunk or (lambda index: self.chunks[index])
        return transfer.import_encrypted(
            self.destination_root, self.destination_state, MIGRATION_ID, SERVER_ID, SOURCE_NODE,
            DESTINATION_NODE, self.receiver_private_key, self.metadata, callback,
        )

    def test_round_trip_encrypts_relayed_chunks_then_commits_staged_tree(self) -> None:
        self.assertEqual(1, self.metadata["protocol"])
        self.assertEqual(len(self.chunks), self.metadata["chunk_count"])
        self.assertNotIn(b"motd=hello", b"".join(self.chunks))
        staged = self._import()
        self.assertEqual(self.export_result["archive_sha256"], staged["archive_sha256"])
        payload = self.destination_root / ".craftpilot-migrations" / MIGRATION_ID / "payload"
        self.assertEqual("motd=hello\n", (payload / "server.properties").read_text(encoding="utf-8"))
        transfer.commit_staged(self.destination_root, self.destination_state, MIGRATION_ID, SERVER_ID,
                               str(self.metadata["archive_sha256"]))
        self.assertEqual(b"world-data" * 4096, (self.destination_root / SERVER_ID / "world" / "level.dat").read_bytes())
        self.assertFalse((self.destination_root / ".craftpilot-migrations" / MIGRATION_ID).exists())
        transfer.commit_staged(self.destination_root, self.destination_state, MIGRATION_ID, SERVER_ID,
                               str(self.metadata["archive_sha256"]))

    def test_archive_uses_actual_file_read_not_advisory_access_check(self) -> None:
        with patch.object(transfer.os, "access", return_value=False):
            archive_bytes, files, _digest = transfer._build_archive(self.source_root, self.root / "readable.zip")

        self.assertGreater(archive_bytes, 0)
        self.assertEqual(2, files)

    def test_archive_refuses_files_the_runtime_agent_cannot_read(self) -> None:
        original_write = zipfile.ZipFile.write

        def reject_server_properties(archive, filename, *args, **kwargs):
            if Path(filename).name == "server.properties":
                raise PermissionError("permission denied")
            return original_write(archive, filename, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "write", reject_server_properties):
            with self.assertRaisesRegex(transfer.MigrationTransferError, "cannot read"):
                transfer._build_archive(self.source_root, self.root / "unreadable.zip")

    def test_tampered_or_reordered_ciphertext_is_rejected_and_staging_removed(self) -> None:
        tampered = list(self.chunks)
        tampered[0] = tampered[0][:-1] + bytes([tampered[0][-1] ^ 1])
        with self.assertRaises(transfer.MigrationTransferError):
            self._import(lambda index: tampered[index])
        self.assertFalse((self.destination_root / ".craftpilot-migrations" / MIGRATION_ID).exists())

    def test_metadata_identity_and_path_binding_are_authenticated(self) -> None:
        self.metadata["destination_node_id"] = SOURCE_NODE
        with self.assertRaises(transfer.MigrationTransferError):
            self._import()

    def test_populated_destination_is_never_overwritten(self) -> None:
        self._import()
        destination = self.destination_root / SERVER_ID
        destination.mkdir()
        (destination / "important.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(transfer.MigrationTransferError):
            transfer.commit_staged(self.destination_root, self.destination_state, MIGRATION_ID, SERVER_ID,
                                   str(self.metadata["archive_sha256"]))
        self.assertEqual("keep", (destination / "important.txt").read_text(encoding="utf-8"))

    def test_source_deletion_is_confined_to_server_uuid_directory(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        invalid_server_id = "../outside"
        (outside / "data").write_text("keep", encoding="utf-8")
        with self.assertRaises(transfer.MigrationTransferError):
            transfer.delete_source(self.root, invalid_server_id)
        (self.root / SERVER_ID / "data").write_text("delete", encoding="utf-8")
        transfer.delete_source(self.root, SERVER_ID)
        self.assertFalse((self.root / SERVER_ID).exists())
        self.assertTrue(outside.exists())

    def test_archive_extraction_rejects_zip_slip(self) -> None:
        archive = self.root / "malicious.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../escape.txt", "bad")
        with self.assertRaises(transfer.MigrationTransferError):
            transfer._safe_extract(archive, self.root / "unsafe-stage")
        self.assertFalse((self.root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
