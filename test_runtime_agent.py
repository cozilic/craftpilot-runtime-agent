"""Focused, Docker-free tests for CraftPilot's Linux runtime agent."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).with_name("craftpilot_runtime_agent.py")
SPEC = importlib.util.spec_from_file_location("craftpilot_runtime_agent", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
agent_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = agent_module
SPEC.loader.exec_module(agent_module)

Config = agent_module.Config
AgentError = agent_module.AgentError
DockerRuntime = agent_module.DockerRuntime
RuntimeNodeAgent = agent_module.RuntimeNodeAgent
FileTreeIndex = agent_module.file_tree_index.FileTreeIndex

SERVER_ID = "e7b4b542-70b0-4cf7-a2ac-2a6597de165d"
CONTAINER_NAME = "craftpilot-e7b4b542"


def make_config(root: Path) -> Config:
    return Config(
        master_url="https://master.example",
        node_id="c4f6babc-0d57-4ab7-a71f-a6358498335a",
        registration_token=None,
        state_dir=root / "state",
        data_root=root / "data",
        images=frozenset({"eclipse-temurin:21-jre"}),
    )


def make_spec(config: Config) -> dict[str, object]:
    return {
        "name": CONTAINER_NAME,
        "image": "eclipse-temurin:21-jre",
        "command": ["sh", "-c", "exec java -jar server.jar nogui"],
        "port": 25571,
        "memory_mb": 2048,
        "cpus": 2.0,
        "network": config.network,
        "mount_path": "/data",
        "container_user": "33:33",
    }


class FakeDocker:
    def __init__(self, inspect_values: list[str], run_output: str = "a" * 64):
        self.inspect_values = list(inspect_values)
        self.run_output = run_output
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["docker", "inspect", "--format"]:
            value = self.inspect_values.pop(0)
            if value == "__missing__":
                return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
            return SimpleNamespace(returncode=0, stdout=value + "\n", stderr="")
        if args[:3] == ["docker", "network", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if args[:3] == ["docker", "network", "connect"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["docker", "stats", "--no-stream"]:
            return SimpleNamespace(returncode=0, stdout="12.5%|512MiB / 2GiB\n", stderr="")
        if args[:2] == ["docker", "logs"]:
            return SimpleNamespace(returncode=0, stdout="2026-10-26T10:00:00.000000000Z [Server thread/INFO]: ready\n", stderr="")
        if args[:2] == ["du", "-sb"]:
            return SimpleNamespace(returncode=0, stdout="10485760\t/data\n", stderr="")
        if args[:3] == ["docker", "run", "--detach"]:
            return SimpleNamespace(returncode=0, stdout=self.run_output + "\n", stderr="")
        if args[:2] == ["docker", "start"]:
            return SimpleNamespace(returncode=0, stdout=CONTAINER_NAME, stderr="")
        if args[:2] == ["docker", "stop"] or args[:2] == ["docker", "kill"]:
            return SimpleNamespace(returncode=0, stdout=CONTAINER_NAME, stderr="")
        if args[:2] == ["docker", "cp"] and args[-1] == "-":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"Unexpected Docker call: {args}")


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        base = make_config(self.root)
        self.config = Config(
            master_url=base.master_url,
            node_id=None,
            registration_token=None,
            state_dir=base.state_dir,
            data_root=base.data_root,
            images=base.images,
        )
        self.agent = RuntimeNodeAgent(self.config, runtime=SimpleNamespace(config=self.config), store=agent_module.StateStore(self.config.state_dir))

    def test_first_start_creates_and_persists_request_then_saves_approved_credentials(self):
        calls = []
        request_id = "9f4a2d91-728a-4e9e-a4a2-a580f263da66"
        node_id = "9f4a2d91-728a-4e9e-a4a2-a580f263da67"
        secret = "s" * 64
        credential = "c" * 64

        def fake_request(method, path, body, credential=False):
            calls.append((method, path, body))
            if path == "/api/runtime/enrollments":
                return {"request_id": request_id, "request_secret": secret}
            if path.endswith("/poll"):
                return {"status": "approved", "node_id": node_id, "credential": credential_value, "master_url": self.config.master_url}
            if path.endswith("/complete"):
                return {"status": "completed"}
            raise AssertionError(path)

        credential_value = credential
        self.agent._request = fake_request
        self.assertFalse(self.agent.ensure_registered())
        saved = self.agent.store.load()
        self.assertEqual(request_id, saved["enrollment_request_id"])
        self.assertEqual(secret, saved["enrollment_request_secret"])
        self.assertTrue(self.agent.poll_enrollment_once())
        self.assertEqual(node_id, self.agent.config.node_id)
        self.assertEqual(credential, self.agent.state["credential"])
        self.assertNotIn("enrollment_request_id", self.agent.state)
        self.assertEqual("/api/runtime/enrollments", calls[0][1])
        self.assertTrue(calls[-1][1].endswith("/complete"))
        self.assertEqual(node_id, self.agent.store.load()["node_id"])

    def test_rejected_request_fails_closed_without_running_commands(self):
        request_id = "9f4a2d91-728a-4e9e-a4a2-a580f263da66"
        self.agent._request = lambda *_args, **_kwargs: {"request_id": request_id, "request_secret": "s" * 64}
        self.agent.ensure_registered()
        self.agent._request = lambda *_args, **_kwargs: {"status": "rejected"}
        with self.assertRaisesRegex(AgentError, "rejected"):
            self.agent.poll_enrollment_once()
        self.assertIsNone(self.agent.state)

    def test_revoked_credential_stays_inert_until_operator_supplies_fresh_token(self):
        node_id = "9f4a2d91-728a-4e9e-a4a2-a580f263da67"
        credential = "c" * 64
        self.config = Config(
            master_url=self.config.master_url,
            node_id=node_id,
            registration_token=None,
            state_dir=self.config.state_dir,
            data_root=self.config.data_root,
            images=self.config.images,
        )
        self.agent = RuntimeNodeAgent(
            self.config, runtime=SimpleNamespace(config=self.config), store=agent_module.StateStore(self.config.state_dir),
        )
        self.agent.state = {"node_id": node_id, "credential": credential, "sequence": 7}

        self.agent._mark_credential_revoked()
        self.assertTrue(self.agent.credential_revoked)
        self.assertIsNone(self.agent.state)
        self.assertEqual({"node_id": node_id, "credential_revoked": True}, self.agent.store.load())

        calls = []
        self.agent._request = lambda *args, **kwargs: calls.append(args) or self.fail("must not enroll without an operator-provided token")
        self.assertFalse(self.agent.ensure_registered())
        self.assertTrue(self.agent.credential_revoked)
        self.assertIsNone(self.agent.state)
        self.assertEqual([], calls)

        token = "t" * 64
        self.agent.config = Config(
            master_url=self.config.master_url,
            node_id=node_id,
            registration_token=token,
            state_dir=self.config.state_dir,
            data_root=self.config.data_root,
            images=self.config.images,
        )
        self.agent.runtime.config = self.agent.config

        def request(method, path, body, credential=False):
            calls.append((method, path, body, credential))
            return {"node_id": node_id, "credential": "n" * 64, "master_url": self.config.master_url}

        self.agent._request = request
        self.assertTrue(self.agent.ensure_registered())
        self.assertFalse(self.agent.credential_revoked)
        self.assertEqual("n" * 64, self.agent.state["credential"])
        self.assertEqual(1, len(calls))
        self.assertEqual("/api/runtime/nodes/%s/register" % node_id, calls[0][1])
        self.assertEqual(token, calls[0][2]["registration_token"])
        self.assertIsNone(self.agent.config.registration_token)
        self.assertNotIn("credential_revoked", self.agent.store.load())

    def test_revoked_credential_waits_when_no_fresh_registration_token_is_available(self):
        node_id = "9f4a2d91-728a-4e9e-a4a2-a580f263da67"
        self.agent.store.save({"node_id": node_id, "credential_revoked": True})
        calls = []
        self.agent._request = lambda *args, **kwargs: calls.append(args) or self.fail("must not register without a token")

        self.assertFalse(self.agent.ensure_registered())
        self.assertTrue(self.agent.credential_revoked)
        self.assertEqual([], calls)
        self.assertEqual({"node_id": node_id, "credential_revoked": True}, self.agent.store.load())

    def test_revoked_credential_keeps_waiting_when_registration_token_is_stale(self):
        node_id = "9f4a2d91-728a-4e9e-a4a2-a580f263da67"
        config = Config(
            master_url=self.config.master_url,
            node_id=node_id,
            registration_token="t" * 64,
            state_dir=self.config.state_dir,
            data_root=self.config.data_root,
            images=self.config.images,
        )
        self.agent = RuntimeNodeAgent(config, runtime=SimpleNamespace(config=config), store=self.agent.store)
        self.agent.store.save({"node_id": node_id, "credential_revoked": True})
        calls = []

        def unauthorized(*_args, **_kwargs):
            calls.append("attempt")
            raise agent_module.HttpStatusError(401)

        self.agent._request = unauthorized
        self.assertFalse(self.agent.ensure_registered())
        self.assertTrue(self.agent.credential_revoked)
        self.assertIsNone(self.agent.state)
        self.assertEqual(["attempt"], calls)
        self.assertEqual({"node_id": node_id, "credential_revoked": True}, self.agent.store.load())

    def test_only_401_from_authenticated_requests_is_a_revoked_credential(self):
        self.agent.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        self.agent._opener = SimpleNamespace(open=lambda *_args, **_kwargs: self.fail("expected HTTP error"))
        request = agent_module.urllib.request.Request("https://master.example/api/runtime/nodes/test/heartbeat")

        for authenticated, expected in ((True, agent_module.CredentialRejectedError),
                                        (False, agent_module.HttpStatusError)):
            with self.subTest(authenticated=authenticated):
                def raise_unauthorized(*_args, **_kwargs):
                    raise agent_module.urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

                self.agent._opener.open = raise_unauthorized
                with self.assertRaises(expected):
                    self.agent._request("POST", "/api/runtime/nodes/test/heartbeat", {}, credential=authenticated)


class MemoryStateStore:
    def __init__(self):
        self.saved: list[dict[str, object]] = []

    def save(self, state):
        self.saved.append(json.loads(json.dumps(state)))


class RuntimeAgentRevocationTests(unittest.TestCase):
    def test_run_forever_does_not_poll_or_exit_after_credential_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            agent = RuntimeNodeAgent(config, runtime=SimpleNamespace(config=config), store=MemoryStateStore())
            calls = []
            agent.ensure_registered = lambda: True
            agent.poll_once = lambda: (_ for _ in ()).throw(agent_module.CredentialRejectedError(401))
            agent._mark_credential_revoked = lambda: (setattr(agent, "credential_revoked", True), calls.append("revoked"))

            def sleep(_delay):
                calls.append("sleep")
                raise KeyboardInterrupt

            with patch.object(agent_module.time, "sleep", side_effect=sleep):
                with self.assertRaises(KeyboardInterrupt):
                    agent.run_forever()

            self.assertEqual(["revoked", "sleep"], calls)


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = make_config(self.root)
        self.config.data_root.mkdir()
        (self.config.data_root / SERVER_ID).mkdir()
        (self.config.data_root / SERVER_ID / "existing.txt").write_text("pre-staged")
        self.runtime = DockerRuntime(self.config, FakeDocker([]))

    def _make_snapshot_payload(self, archive_path: Path) -> tuple[str, dict[str, object]]:
        command_id = "f1d64bb9-2585-4d7e-b0e2-463732a212ac"
        raw = archive_path.read_bytes()
        filename = f"{SERVER_ID}-{command_id}.zip"
        return command_id, {
            "server_id": SERVER_ID,
            "snapshot": {"filename": filename, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
            "download_url": f"https://master.example/api/runtime/nodes/{self.config.node_id}/commands/{command_id}/snapshot/{filename}",
        }

    def test_snapshot_stages_verified_archive_atomically_and_refuses_existing_data(self):
        archive_path = self.root / "snapshot.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("server.properties", "motd=hello")
            archive.writestr("world/level.dat", "level")
        archive_bytes = archive_path.read_bytes()
        (self.config.data_root / SERVER_ID / "existing.txt").unlink()
        self.runtime = DockerRuntime(self.config, FakeDocker([]))

        command_id, payload = self._make_snapshot_payload(archive_path)

        class FakeResponse:
            status = 200

            def __init__(self, body: bytes):
                self.body = body
                self.offset = 0

            def read(self, size=-1):
                if self.offset >= len(self.body):
                    return b""
                chunk = self.body[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        class FakeOpener:
            def open(self, request, timeout=15):
                self.request = request
                return FakeResponse(archive_bytes)

        opener = FakeOpener()
        self.runtime._opener = opener
        result = self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        self.assertEqual(f"Bearer {'c' * 64}", opener.request.get_header("Authorization"))
        staged = self.config.data_root / SERVER_ID
        self.assertEqual("SNAPSHOT_STAGED", result["result_code"])
        self.assertEqual("motd=hello", (staged / "server.properties").read_text())
        self.assertEqual("level", (staged / "world/level.dat").read_text())
        (staged / "server.properties").write_text("motd=changed")
        with self.assertRaises(AgentError):
            self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        self.assertEqual("motd=changed", (staged / "server.properties").read_text())

    def test_snapshot_populates_an_existing_empty_destination_directory(self):
        (self.config.data_root / SERVER_ID / "existing.txt").unlink()
        archive_path = self.root / "snapshot.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("server.properties", "motd=hello")
        command_id, payload = self._make_snapshot_payload(archive_path)
        class FakeResponse:
            status = 200
            def __init__(self, body: bytes):
                self.body = body
                self.offset = 0
            def read(self, size=-1):
                chunk = self.body[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk
            def __enter__(self): return self
            def __exit__(self, *_args): return None
        class FakeOpener:
            def open(self, _request, timeout=15): return FakeResponse(archive_path.read_bytes())
        self.runtime._opener = FakeOpener()

        result = self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)

        self.assertEqual("SNAPSHOT_STAGED", result["result_code"])
        self.assertEqual("motd=hello", (self.config.data_root / SERVER_ID / "server.properties").read_text())

    def test_snapshot_refuses_a_nonempty_destination_without_changing_it(self):
        self.runtime._opener = object()
        archive_path = self.root / "snapshot.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("server.properties", "motd=hello")
        command_id, payload = self._make_snapshot_payload(archive_path)
        class FakeResponse:
            status = 200
            def __init__(self, body: bytes):
                self.body = body
                self.offset = 0
            def read(self, size=-1):
                chunk = self.body[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk
            def __enter__(self): return self
            def __exit__(self, *_args): return None
        class FakeOpener:
            def open(self, _request, timeout=15): return FakeResponse(archive_path.read_bytes())
        self.runtime._opener = FakeOpener()

        with self.assertRaises(AgentError):
            self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)

        self.assertTrue((self.config.data_root / SERVER_ID).is_dir())
        self.assertEqual("pre-staged", (self.config.data_root / SERVER_ID / "existing.txt").read_text())

    def test_snapshot_retry_is_idempotent_only_for_an_identical_snapshot(self):
        (self.config.data_root / SERVER_ID / "existing.txt").unlink()
        self.runtime._opener = object()
        archive_path = self.root / "snapshot.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("server.properties", "motd=hello")
        command_id, payload = self._make_snapshot_payload(archive_path)

        class FakeResponse:
            status = 200

            def __init__(self, body: bytes):
                self.body = body
                self.offset = 0

            def read(self, size=-1):
                if self.offset >= len(self.body):
                    return b""
                chunk = self.body[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        class FakeOpener:
            def open(self, _request, timeout=15):
                return FakeResponse(archive_path.read_bytes())

        self.runtime._opener = FakeOpener()
        first = self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        (self.config.data_root / SERVER_ID / "server.properties").unlink()
        (self.config.data_root / SERVER_ID).rmdir()
        second = self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        self.assertEqual(first, second)
        self.assertEqual("motd=hello", (self.config.data_root / SERVER_ID / "server.properties").read_text())

        (self.config.data_root / SERVER_ID / "server.properties").write_text("motd=changed")
        class ExistingDirectoryOpener:
            def open(self, _request, timeout=15):
                return FakeResponse(archive_path.read_bytes())
        self.runtime._opener = ExistingDirectoryOpener()
        with self.assertRaises(AgentError):
            self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        self.assertEqual("motd=changed", (self.config.data_root / SERVER_ID / "server.properties").read_text())

    def test_snapshot_rejects_wrong_host_bad_digest_and_zip_slip(self):
        self.runtime._opener = object()
        archive_path = self.root / "snapshot.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../escape.txt", "unsafe")
        command_id, payload = self._make_snapshot_payload(archive_path)
        payload["download_url"] = payload["download_url"].replace("master.example", "attacker.example")
        with self.assertRaises(AgentError):
            self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        command_id, payload = self._make_snapshot_payload(archive_path)
        payload["snapshot"]["filename"] = "../escape.zip"
        with self.assertRaises(AgentError):
            self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        payload["snapshot"]["filename"] = f"{SERVER_ID}-{command_id}.zip"
        class FakeResponse:
            status = 200
            def __init__(self, body: bytes):
                self.body = body
                self.offset = 0
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, size=-1):
                chunk = self.body[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk
        class FakeOpener:
            def open(self, _request, timeout=15): return FakeResponse(archive_path.read_bytes())
        self.runtime._opener = FakeOpener()
        with self.assertRaises(AgentError):
            self.runtime.execute("snapshot", payload, credential="c" * 64, command_id=command_id)
        self.assertEqual(["existing.txt"], [path.name for path in (self.config.data_root / SERVER_ID).iterdir()])

    def test_remote_file_upload_mutation_returns_verified_size_and_digest(self):
        payload = {
            "operation": "upload",
            "path": "mods",
            "upload": {"bytes": 12, "sha256": "a" * 64},
        }
        with patch.object(self.runtime, "_upload_server_file", return_value={"bytes": 12, "sha256": "a" * 64}) as upload:
            result = self.runtime.execute("file_mutate", {"server_id": SERVER_ID, **payload},
                                          credential="c" * 64, command_id="f1d64bb9-2585-4d7e-b0e2-463732a212ac")

        self.assertEqual("FILE_MUTATED", result["result_code"])
        self.assertEqual({"operation": "upload", "ok": True, "bytes": 12, "sha256": "a" * 64}, result["result"])
        upload.assert_called_once()

    def test_remote_file_upload_result_is_posted_before_command_acknowledgment(self):
        command_id = "f1d64bb9-2585-4d7e-b0e2-463732a212ac"
        command = {
            "id": command_id,
            "lease_token": "l" * 64,
            "type": "file_mutate",
            "payload": {"server_id": SERVER_ID, "operation": "upload"},
        }
        requests = []

        class FakeRuntime:
            def execute(self, command_type, payload, credential=None, command_id=None):
                self_command = (command_type, payload, credential, command_id)
                requests.append(("execute", self_command))
                return {"success": True, "result_code": "FILE_MUTATED",
                        "result": {"operation": "upload", "ok": True, "bytes": 12, "sha256": "a" * 64}}

        agent = RuntimeNodeAgent(self.config, runtime=FakeRuntime(), store=MemoryStateStore())
        agent.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 0}

        def request(method, path, body, credential=False):
            if path.endswith("/commands/claim"):
                return {"command": command}
            requests.append((path.rsplit("/", 1)[-1], body))
            return {"ok": True}

        agent._request = request
        agent._report_servers = lambda _sequence: None
        agent._sync_file_tree_cache = lambda: None

        agent.poll_once()

        result_request, ack_request = requests[1:]
        self.assertEqual("result", result_request[0])
        self.assertEqual({"lease_token": "l" * 64,
                          "result": {"operation": "upload", "ok": True, "bytes": 12, "sha256": "a" * 64}},
                         result_request[1])
        self.assertEqual("ack", ack_request[0])
        self.assertTrue(ack_request[1]["success"])

    def test_file_tree_sync_posts_bounded_deltas_then_acknowledges_them(self):
        agent = RuntimeNodeAgent(self.config, runtime=self.runtime, store=MemoryStateStore())
        agent.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 0}
        index = agent.file_tree_index
        index._ready = True
        index._store_directory(SERVER_ID, 1, [{
            "name": "worlds", "type": "dir", "size": None, "modified_at": "2026-10-28T10:00:00Z",
        }])
        for i in range(55):
            index._queue_sync_locked(f"{SERVER_ID}/folder-{i}", {
                "path": f"folder-{i}", "parent": "", "entries": [],
            })
        requests = []

        def request(method, path, body, credential=False):
            self.assertTrue(credential)
            requests.append((method, path, body))
            if path.endswith("/servers"):
                return {"servers": [SERVER_ID]}
            if path.endswith("/file-tree/progress"):
                return {"ok": True}
            return {"ok": True}

        agent._request = request
        agent._sync_file_tree_cache()

        self.assertEqual("GET", requests[0][0])
        batches = [request for request in requests if request[1].endswith("/file-tree")]
        self.assertEqual(3, len(batches))
        self.assertLessEqual(len(batches[0][2]["directories"]), 20)
        self.assertEqual(SERVER_ID, batches[0][2]["directories"][0]["server_id"])
        self.assertEqual("", batches[0][2]["directories"][0]["path"])
        self.assertEqual([], index.pending_sync_updates({SERVER_ID}))

    def test_file_tree_sync_failure_keeps_delta_for_retry(self):
        agent = RuntimeNodeAgent(self.config, runtime=self.runtime, store=MemoryStateStore())
        agent.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 0}
        index = agent.file_tree_index
        index._ready = True
        index._store_directory(SERVER_ID, 1, [])

        def request(_method, path, _body, credential=False):
            self.assertTrue(credential)
            if path.endswith("/servers"):
                return {"servers": [SERVER_ID]}
            if path.endswith("/file-tree/progress"):
                return {"ok": True}
            raise agent_module.HttpStatusError(503)

        agent._request = request
        agent._sync_file_tree_cache()

        self.assertEqual(1, len(index.pending_sync_updates({SERVER_ID})))

    def test_file_tree_index_serves_repeated_listing_without_rescanning(self):
        index = FileTreeIndex(self.config.data_root, self.config.state_dir)
        index._ready = True
        scan = patch.object(FileTreeIndex, "scan_path", wraps=FileTreeIndex.scan_path)
        with scan as scan_directory:
            self.runtime.file_tree_index = index
            first = self.runtime._list_server_files(SERVER_ID, "")
            second = self.runtime._list_server_files(SERVER_ID, "")

        self.assertEqual(first, second)
        self.assertEqual(1, scan_directory.call_count)
        self.assertTrue(index.index_path.is_file())
        if os.name != "nt":
            self.assertEqual(0o600, index.index_path.stat().st_mode & 0o777)

    def test_file_tree_index_invalidates_on_directory_mtime_change(self):
        index = FileTreeIndex(self.config.data_root, self.config.state_dir)
        index._ready = True
        self.runtime.file_tree_index = index
        first = self.runtime._list_server_files(SERVER_ID, "")
        (self.config.data_root / SERVER_ID / "new.txt").write_text("new")
        second = self.runtime._list_server_files(SERVER_ID, "")

        self.assertNotEqual([entry["name"] for entry in first["entries"]],
                            [entry["name"] for entry in second["entries"]])
        self.assertIn("new.txt", [entry["name"] for entry in second["entries"]])

    def test_file_tree_index_persists_and_rejects_corrupt_json(self):
        index = FileTreeIndex(self.config.data_root, self.config.state_dir)
        index._ready = True
        self.runtime.file_tree_index = index
        expected = self.runtime._list_server_files(SERVER_ID, "")

        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        index._persist_locked()
        restored = FileTreeIndex(self.config.data_root, self.config.state_dir)
        self.assertIn(SERVER_ID, restored._directories)
        self.assertEqual(expected["entries"], restored._directories[SERVER_ID]["entries"])
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        restored.index_path.write_text("not json", encoding="utf-8")
        corrupt = FileTreeIndex(self.config.data_root, self.config.state_dir)
        self.assertEqual({}, corrupt._directories)
        corrupt._ready = True
        self.runtime.file_tree_index = corrupt
        rebuilt = self.runtime._list_server_files(SERVER_ID, "")
        self.assertEqual(expected, rebuilt)

    @unittest.skipUnless(sys.platform.startswith("linux"), "inotify is available only on Linux")
    def test_file_tree_index_inotify_refreshes_external_directory_changes(self):
        import time

        index = FileTreeIndex(self.config.data_root, self.config.state_dir)
        index.start()
        self.addCleanup(index.close)
        self.assertTrue(index.wait_until_ready())
        key = FileTreeIndex.directory_key(SERVER_ID, "")
        self.assertIn(key, index._directories)

        (self.config.data_root / SERVER_ID / "external.txt").write_text("external")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            record = index._directories.get(key, {})
            if "external.txt" in {entry["name"] for entry in record.get("entries", [])}:
                break
            time.sleep(0.01)

        self.assertIn("external.txt", {entry["name"] for entry in index._directories[key]["entries"]})

    def test_file_tree_index_invalidate_forces_rescan(self):
        index = FileTreeIndex(self.config.data_root, self.config.state_dir)
        index._ready = True
        self.runtime.file_tree_index = index
        first = self.runtime._list_server_files(SERVER_ID, "")
        index.invalidate(SERVER_ID, "")
        (self.config.data_root / SERVER_ID / "new.txt").write_text("new")
        second = self.runtime._list_server_files(SERVER_ID, "")

        self.assertNotEqual(first, second)
        self.assertIn("new.txt", [entry["name"] for entry in second["entries"]])

    def test_remote_file_list_and_read_use_fd_relative_paths_and_reject_symlink_escape(self):
        (self.config.data_root / SERVER_ID / "docs").mkdir()
        (self.config.data_root / SERVER_ID / "docs" / "notes.txt").write_text("hello remote")
        listed = self.runtime._list_server_files(SERVER_ID, "docs")
        self.assertEqual("docs", listed["path"])
        self.assertEqual("notes.txt", listed["entries"][0]["name"])
        read = self.runtime._read_server_file(SERVER_ID, "docs/notes.txt")
        self.assertEqual("hello remote", read["content"])

        outside = self.root / "outside.txt"
        outside.write_text("secret")
        try:
            (self.config.data_root / SERVER_ID / "escape.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating a symlink requires Windows developer mode or elevated privileges")
        with self.assertRaises(AgentError):
            self.runtime._read_server_file(SERVER_ID, "escape.txt")
        with self.assertRaises(AgentError):
            self.runtime._read_server_file(SERVER_ID, "../outside.txt")

    def test_remote_file_listing_rejects_a_symlink_server_root(self):
        server_root = self.config.data_root / SERVER_ID
        server_root.rename(self.root / "actual-server-data")
        try:
            server_root.symlink_to(self.root / "actual-server-data", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating a symlink requires Windows developer mode or elevated privileges")
        with self.assertRaises(AgentError):
            self.runtime._list_server_files(SERVER_ID, "")

    def test_start_creates_only_expected_container_with_limits_and_labels(self):
        (self.config.data_root / SERVER_ID / "existing.txt").unlink()
        (self.config.data_root / SERVER_ID / "server.properties").write_text("motd=Test\nenable-rcon=false\n")
        runner = FakeDocker(["__missing__", "true", "b" * 64])
        runtime = DockerRuntime(self.config, runner)
        runtime.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 0}
        runtime.state_store = MemoryStateStore()

        with self.assertLogs("craftpilot.runtime_agent", level="INFO") as captured:
            result = runtime.execute("start", {"server_id": SERVER_ID, "container": make_spec(self.config)})

        self.assertEqual("STARTED", result["result_code"])
        self.assertTrue(any("server_container_create_started" in message for message in captured.output))
        self.assertTrue(any("server_started" in message for message in captured.output))
        properties = (self.config.data_root / SERVER_ID / "server.properties").read_text()
        self.assertIn("enable-rcon=true", properties)
        self.assertIn("rcon.port=25575", properties)
        self.assertIn("rcon.ip=0.0.0.0", properties)
        self.assertRegex(properties, r"rcon.password=[a-f0-9]{64}")
        docker_run = next(call for call in runner.calls if call[:3] == ["docker", "run", "--detach"])
        self.assertIn("--memory", docker_run)
        self.assertIn("--cpus", docker_run)
        self.assertIn("--restart", docker_run)
        self.assertIn("craftpilot.server_id=" + SERVER_ID, docker_run)
        self.assertIn("eclipse-temurin:21-jre", docker_run)

    def test_rcon_settings_use_persistent_private_password_and_atomic_properties_write(self):
        properties = self.config.data_root / SERVER_ID / "server.properties"
        properties.write_text("motd=Keep this\nenable-rcon=false\nenable-rcon=false\nrcon.port=25575\n")
        store = MemoryStateStore()
        self.runtime.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        self.runtime.state_store = store

        self.runtime._ensure_rcon(SERVER_ID)
        first_password = self.runtime.state["rcon_passwords"][SERVER_ID]
        self.runtime._ensure_rcon(SERVER_ID)
        content = properties.read_text()

        self.assertEqual(first_password, self.runtime.state["rcon_passwords"][SERVER_ID])
        self.assertEqual(1, content.count("enable-rcon="))
        self.assertIn("enable-rcon=true", content)
        self.assertIn("rcon.port=25575", content)
        self.assertIn("rcon.ip=0.0.0.0", content)
        self.assertEqual(1, content.count("rcon.password="))
        self.assertIn(f"rcon.password={first_password}", content)
        self.assertIn("motd=Keep this", content)
        self.assertGreaterEqual(len(store.saved), 2)
        if os.name != "nt":
            self.assertNotEqual(0, properties.stat().st_mode & 0o020, "Minecraft's shared group must be able to save server.properties")

    def test_start_refuses_missing_rcon_state_before_creating_container(self):
        (self.config.data_root / SERVER_ID / "existing.txt").unlink()
        (self.config.data_root / SERVER_ID / "server.properties").write_text("motd=Test\n")
        runner = FakeDocker(["__missing__"])

        with self.assertRaisesRegex(AgentError, "RCON state"):
            DockerRuntime(self.config, runner).execute("start", {"server_id": SERVER_ID, "container": make_spec(self.config)})
        self.assertFalse(any(call[:3] == ["docker", "run", "--detach"] for call in runner.calls))

    def test_docker_server_data_path_can_be_overridden_for_desktop_daemon(self):
        self.config = Config(
            master_url="https://master.example",
            node_id="c4f6babc-0d57-4ab7-a71f-a6358498335a",
            registration_token=None,
            state_dir=self.root / "state",
            data_root=self.root / "data",
            images=frozenset({"eclipse-temurin:21-jre"}),
            docker_data_root="C:/CraftPilot/runtime-data",
        )
        (self.config.data_root / SERVER_ID / "server.properties").write_text("motd=Test\n")
        state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 0}
        runner = FakeDocker(["__missing__", "true", "b" * 64])
        result_runtime = DockerRuntime(self.config, runner)
        result_runtime.state = state
        result_runtime.state_store = MemoryStateStore()
        result = result_runtime.execute(
            "start", {"server_id": SERVER_ID, "container": make_spec(self.config)},
        )
        self.assertEqual("STARTED", result["result_code"])
        runtime_runner = FakeDocker(["__missing__", "true", "b" * 64])
        runtime = DockerRuntime(self.config, runtime_runner)
        runtime.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 0}
        runtime.state_store = MemoryStateStore()
        runtime.execute("start", {"server_id": SERVER_ID, "container": make_spec(self.config)})
        docker_run = next(call for call in runtime_runner.calls if call[:3] == ["docker", "run", "--detach"])
        self.assertIn("C:/CraftPilot/runtime-data/" + SERVER_ID + ":/data:rw", docker_run)

    def test_start_refuses_unallowlisted_image_before_invoking_docker_run(self):
        runner = FakeDocker(["__missing__"])
        spec = make_spec(self.config)
        spec["image"] = "attacker/unknown:latest"

        with self.assertRaises(AgentError):
            DockerRuntime(self.config, runner).execute("start", {"server_id": SERVER_ID, "container": spec})
        self.assertFalse(any(call[:3] == ["docker", "run", "--detach"] for call in runner.calls))

    def test_start_refuses_a_server_data_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        shutil.rmtree(self.config.data_root / SERVER_ID)
        try:
            (self.config.data_root / SERVER_ID).symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Creating a symlink requires Windows developer mode or elevated privileges")
            raise
        runner = FakeDocker(["__missing__"])

        with self.assertRaises(AgentError):
            DockerRuntime(self.config, runner).execute("start", {"server_id": SERVER_ID, "container": make_spec(self.config)})
        self.assertFalse(any(call[:3] == ["docker", "run", "--detach"] for call in runner.calls))

    def test_console_validates_container_ownership_and_rejects_line_breaks(self):
        runtime = DockerRuntime(self.config, FakeDocker(["true", json.dumps({
            "craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID,
        })]))
        runtime.state = {"rcon_passwords": {SERVER_ID: "a" * 64}}
        runtime._send_rcon = lambda *_args: None
        result = runtime.execute("console", {"server_id": SERVER_ID, "command": "list"})
        self.assertEqual("CONSOLE_EXECUTED", result["result_code"])
        self.assertEqual(hashlib.sha256(b"list").hexdigest(), result["message"])

        for command in ("", "say\nstop", "say\rstop", "say\x00stop", "x" * 16385):
            with self.subTest(command=repr(command)):
                with self.assertRaises(AgentError):
                    runtime.execute("console", {"server_id": SERVER_ID, "command": command})

    def test_console_refuses_foreign_container_and_offline_state(self):
        foreign = DockerRuntime(self.config, FakeDocker(["true", json.dumps({
            "craftpilot.managed": "1", "craftpilot.server_id": "00000000-0000-0000-0000-000000000000",
        })]))
        with self.assertRaises(AgentError):
            foreign.execute("console", {"server_id": SERVER_ID, "command": "list"})

        offline = DockerRuntime(self.config, FakeDocker(["false"]))
        with self.assertRaisesRegex(AgentError, "not running"):
            offline.execute("console", {"server_id": SERVER_ID, "command": "list"})

    def test_server_metrics_parse_docker_units_and_tolerate_bad_stats(self):
        self.assertEqual((12.5, 512, 2048), RuntimeNodeAgent._parse_docker_stats("12.5%|512MiB / 2GiB"))
        self.assertEqual((None, None, None), RuntimeNodeAgent._parse_docker_stats("not available"))
        self.assertEqual(1536, RuntimeNodeAgent._parse_size_mb("1.5 GiB"))
        self.assertEqual(1, RuntimeNodeAgent._parse_size_mb("1 MB"))
        self.assertIsNone(RuntimeNodeAgent._parse_size_mb("unknown"))

    def test_staged_server_data_without_container_reports_offline(self):
        runtime = DockerRuntime(self.config, FakeDocker(["__missing__"]))
        agent = RuntimeNodeAgent(self.config, runtime=runtime)

        report = agent._server_report(SERVER_ID, 7)

        self.assertEqual("OFFLINE", report["status"])
        self.assertIsNone(report["cpu_percent"])
        self.assertIsNone(report["uptime_seconds"])
        self.assertEqual([], report["logs"])

    def test_runtime_agent_reports_assigned_staged_server_without_container(self):
        state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        runtime = DockerRuntime(self.config, FakeDocker(["__missing__"]))
        store = SimpleNamespace(save=lambda _current: None)
        agent = RuntimeNodeAgent(self.config, runtime=runtime, store=store)
        agent.state = state
        agent._assigned_server_ids = lambda: []
        reports = []

        def request(method, path, body, credential=False):
            self.assertTrue(credential)
            if method == "GET" and path.endswith("/servers"):
                return {"servers": [SERVER_ID]}
            if method == "POST" and path.endswith("/servers/report"):
                reports.append(body)
                return {"ok": True}
            raise AssertionError(f"Unexpected telemetry request: {method} {path}")

        agent._request = request
        agent._report_servers(2)

        self.assertEqual(1, len(reports))
        self.assertEqual(SERVER_ID, reports[0]["server_id"])
        self.assertEqual("OFFLINE", reports[0]["status"])

    def test_server_report_returns_status_metrics_logs_and_data_disk_usage(self):
        (self.config.data_root / SERVER_ID / "existing.txt").write_text("x" * 100)
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        runner = FakeDocker([
            "true", labels, "running", start, "10485760\t/data\n",
            "2026-10-26T10:00:00.000000000Z [Server thread/INFO]: ready",
        ])
        runtime = DockerRuntime(self.config, runner)
        report = RuntimeNodeAgent(self.config, runtime=runtime)._server_report(SERVER_ID, 7)

        self.assertEqual("ONLINE", report["status"])
        self.assertEqual(12.5, report["cpu_percent"])
        self.assertEqual(512, report["memory_used_mb"])
        self.assertEqual(2048, report["memory_limit_mb"])
        self.assertIsInstance(report["uptime_seconds"], int)
        self.assertGreaterEqual(report["disk_used_mb"], 0)
        self.assertEqual("[Server thread/INFO]: ready", report["logs"][0]["text"])
        self.assertIsNone(report["players_online"])
        log_call = next(call for call in runner.calls if call[:2] == ["docker", "logs"])
        self.assertIn("--timestamps", log_call)
        self.assertIn("--tail", log_call)

    def test_container_logs_merge_both_streams_filter_timestamp_and_reject_bad_cursor(self):
        runner = FakeDocker([])
        runtime = DockerRuntime(self.config, runner)
        output = (
            "2026-10-26T10:00:02Z stdout-later\n"
            "2026-10-26T10:00:01Z stdout-earlier\n"
            "not-a-timestamp invalid\n"
        )
        stderr = "2026-10-26T10:00:03Z stderr-error\n"
        runtime.runner = lambda _args, **_kwargs: SimpleNamespace(returncode=0, stdout=output, stderr=stderr)

        logs = runtime._container_logs(CONTAINER_NAME, "2026-10-26T10:00:01+00:00")

        self.assertEqual(["stdout-later", "stderr-error"], [line["text"] for line in logs])
        with self.assertRaisesRegex(AgentError, "cursor"):
            runtime._container_logs(CONTAINER_NAME, "not-a-timestamp")


    def test_runtime_agent_reports_only_servers_currently_assigned_by_master(self):
        class FakeRuntime:
            config = self.config

            def _run(self, *_args, **_kwargs):
                return ""

        state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        store = SimpleNamespace(save=lambda _current: None)
        agent = RuntimeNodeAgent(self.config, runtime=FakeRuntime(), store=store)
        agent.state = state
        assignment_snapshots = iter([[SERVER_ID], [SERVER_ID], [SERVER_ID]])
        agent._assigned_server_ids = lambda: next(assignment_snapshots)
        report_calls = []
        agent._server_report = lambda server_id, *_args: report_calls.append(server_id) or {
            "server_id": server_id, "sequence": 2, "status": "OFFLINE", "logs": [],
        }
        requests = []

        def request(method, path, body, credential=False):
            requests.append((method, path, credential))
            if path.endswith("/servers"):
                return {"servers": []}
            raise AssertionError(f"Unexpected telemetry request: {path}")

        agent._request = request
        agent._report_servers(2)

        self.assertEqual([], report_calls)
        self.assertEqual([("GET", f"/api/runtime/nodes/{self.config.node_id}/servers", True)], requests)

    def test_runtime_agent_skips_all_telemetry_if_assignment_fetch_fails(self):
        class FakeRuntime:
            config = self.config

            def _run(self, *_args, **_kwargs):
                return ""

        state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        agent = RuntimeNodeAgent(
            self.config, runtime=FakeRuntime(), store=SimpleNamespace(save=lambda _current: None),
        )
        agent.state = state
        agent._assigned_server_ids = lambda: [SERVER_ID]
        reports = []
        agent._server_report = lambda *args: reports.append(args)
        agent._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(agent_module.HttpStatusError(503))

        with self.assertLogs("craftpilot.runtime_agent", level="WARNING"):
            agent._report_servers(2)

        self.assertEqual([], reports)

    def test_404_server_report_is_suppressed_until_master_assignment_changes(self):
        class FakeRuntime:
            config = self.config

            def _run(self, *_args, **_kwargs):
                return ""

        state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        saved = []
        store = SimpleNamespace(save=lambda current: saved.append(json.loads(json.dumps(current))))
        agent = RuntimeNodeAgent(self.config, runtime=FakeRuntime(), store=store)
        agent.state = state
        assignment_snapshots = iter([[SERVER_ID], [SERVER_ID], [SERVER_ID]])
        agent._assigned_server_ids = lambda: next(assignment_snapshots)
        report_calls = []
        agent._server_report = lambda server_id, *_args: report_calls.append(server_id) or {
            "server_id": server_id, "sequence": 2, "status": "OFFLINE", "logs": [],
        }
        responses = iter([
            {"servers": [SERVER_ID]}, agent_module.HttpStatusError(404),
            {"servers": [SERVER_ID]}, {"servers": []},
        ])
        def request(method, path, *_args, **_kwargs):
            response = next(responses)
            if isinstance(response, agent_module.HttpStatusError):
                raise response
            return response

        agent._request = request

        with self.assertLogs("craftpilot.runtime_agent", level="INFO") as captured:
            agent._report_servers(2)
            agent._report_servers(3)

        self.assertEqual([SERVER_ID], report_calls)
        self.assertIn(SERVER_ID, state["unassigned_servers"])
        self.assertTrue(any("server_report_suppressed" in message and SERVER_ID in message for message in captured.output))
        agent._server_report = lambda server_id, sequence, *_args: {
            "server_id": server_id, "sequence": sequence, "status": "OFFLINE", "logs": [],
        }
        agent._request = lambda method, path, *_args, **_kwargs: (
            {"servers": []} if path.endswith("/servers") else {"ok": True}
        )
        agent._assigned_server_ids = lambda: [SERVER_ID]
        agent._report_servers(4)
        self.assertNotIn(SERVER_ID, state["unassigned_servers"])

    def test_server_report_classifies_clean_exit_as_offline_and_nonzero_exit_as_crashed(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})

        for exit_code, expected_status in (("0", "OFFLINE"), ("137", "CRASHED")):
            with self.subTest(exit_code=exit_code):
                runner = FakeDocker(["false", labels, "exited", exit_code, "2026-10-26T10:00:00Z|0|123"])
                agent = RuntimeNodeAgent(self.config, runtime=DockerRuntime(self.config, runner))
                agent.state = {"sequence": 1, "intentional_stops": {}}

                report = agent._server_report(SERVER_ID, 9)

                self.assertEqual(expected_status, report["status"])

    def test_successful_stop_command_persists_stop_intent_before_acknowledgment(self):
        command_id = "63473593-e3d4-4f12-8778-73a54ef93d98"
        calls = []
        state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        store = SimpleNamespace(save=lambda saved: calls.append(("save", json.loads(json.dumps(saved)))))
        agent = RuntimeNodeAgent(self.config, runtime=SimpleNamespace(execute=lambda *_args, **_kwargs: {
            "success": True, "result_code": "STOPPED",
        }), store=store)
        agent.state = state

        def request(_method, path, _body, credential=False):
            calls.append(("request", path))
            if path.endswith("/commands/claim"):
                return {"command": {
                    "id": command_id, "lease_token": "l" * 64, "type": "stop", "payload": {"server_id": SERVER_ID},
                }}
            return {"ok": True}

        agent._request = request

        def observe_report(_sequence):
            self.assertEqual({SERVER_ID: command_id}, state["intentional_stops"])

        agent._report_servers = observe_report
        with self.assertLogs("craftpilot.runtime_agent", level="INFO") as captured:
            agent.poll_once()

        self.assertTrue(any("command_received" in message and command_id in message for message in captured.output))
        self.assertTrue(any("command_completed" in message and "result=STOPPED" in message for message in captured.output))
        self.assertEqual({SERVER_ID: command_id}, state["intentional_stops"])
        saved_index = next(index for index, call in enumerate(calls)
                           if call[0] == "save" and call[1].get("intentional_stops") == {SERVER_ID: command_id})
        ack_index = next(index for index, call in enumerate(calls)
                         if call[0] == "request" and call[1].endswith(f"/commands/{command_id}/ack"))
        self.assertLess(saved_index, ack_index)

    def test_migration_stop_persists_intent_before_acknowledgment(self):
        command_id = "05bb4925-1a55-49e0-a940-115f06572ee9"
        migration_id = "a2716e76-3e0c-40eb-9337-8d77a7aa23c6"
        calls = []
        state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 1}
        store = SimpleNamespace(save=lambda saved: calls.append(("save", json.loads(json.dumps(saved)))))
        agent = RuntimeNodeAgent(self.config, runtime=SimpleNamespace(execute=lambda *_args, **_kwargs: {
            "success": True, "result_code": "MIGRATION_STOPPED",
        }), store=store)
        agent.state = state

        def request(_method, path, _body, credential=False):
            calls.append(("request", path))
            if path.endswith("/commands/claim"):
                return {"command": {
                    "id": command_id, "lease_token": "l" * 64, "type": "migration_stop",
                    "payload": {"server_id": SERVER_ID, "migration_id": migration_id},
                }}
            return {"ok": True}

        agent._request = request
        agent._report_servers = lambda _sequence: None
        agent.poll_once()

        self.assertEqual({SERVER_ID: command_id}, state["intentional_stops"])
        saved_index = next(index for index, call in enumerate(calls)
                           if call[0] == "save" and call[1].get("intentional_stops") == {SERVER_ID: command_id})
        ack_index = next(index for index, call in enumerate(calls)
                         if call[0] == "request" and call[1].endswith(f"/commands/{command_id}/ack"))
        self.assertLess(saved_index, ack_index)

    def test_running_report_clears_old_intentional_stop(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        runner = FakeDocker(["true", labels, "running", "2026-10-26T10:00:00Z|0|123"])
        store = SimpleNamespace(save=lambda _state: None)
        runtime = DockerRuntime(self.config, runner)
        agent = RuntimeNodeAgent(self.config, runtime=runtime, store=store)
        agent.state = {"sequence": 1, "intentional_stops": {SERVER_ID: "63473593-e3d4-4f12-8778-73a54ef93d98"}}
        runtime.state = agent.state

        report = agent._server_report(SERVER_ID, 9)

        self.assertEqual("ONLINE", report["status"])
        self.assertNotIn(SERVER_ID, agent.state["intentional_stops"])

    def test_server_report_does_not_mark_acknowledged_graceful_stop_as_crashed(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        runner = FakeDocker(["false", labels, "exited", "137", "2026-10-26T10:00:00Z|0|123"])
        store = SimpleNamespace(save=lambda state: None)
        runtime = DockerRuntime(self.config, runner)
        agent = RuntimeNodeAgent(self.config, runtime=runtime, store=store)
        agent.state = {"sequence": 1, "intentional_stops": {SERVER_ID: "63473593-e3d4-4f12-8778-73a54ef93d98"}}
        runtime.state = agent.state

        report = agent._server_report(SERVER_ID, 9)

        self.assertEqual("OFFLINE", report["status"])
        self.assertIn(SERVER_ID, agent.state["intentional_stops"])

    def test_server_report_keeps_status_when_stats_or_logs_fail(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})

        class PartiallyFailingDocker(FakeDocker):
            def __call__(self, args, **kwargs):
                self.calls.append(list(args))
                if args[:3] == ["docker", "inspect", "--format"]:
                    value = self.inspect_values.pop(0)
                    return SimpleNamespace(returncode=0, stdout=value + "\n", stderr="")
                if args[:3] == ["docker", "stats", "--no-stream"] or args[:2] == ["docker", "logs"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="Docker unavailable")
                if args[:2] == ["du", "-sb"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="du unavailable")
                raise AssertionError(args)

        runner = PartiallyFailingDocker(["true", labels, "running", "2026-10-26T10:00:00Z|0|123"])
        report = RuntimeNodeAgent(self.config, runtime=DockerRuntime(self.config, runner))._server_report(SERVER_ID, 8)

        self.assertEqual("ONLINE", report["status"])
        self.assertIsNone(report["cpu_percent"])
        self.assertIsNone(report["memory_used_mb"])
        self.assertEqual([], report["logs"])
        self.assertIsNone(report["disk_used_mb"])

    def test_stop_refuses_container_with_foreign_labels(self):
        runner = FakeDocker(["true", json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": "other"})])

        with self.assertRaises(AgentError):
            DockerRuntime(self.config, runner).execute("stop", {"server_id": SERVER_ID, "force": False})
        self.assertFalse(any(call[:2] == ["docker", "stop"] for call in runner.calls))

    def test_stop_uses_minecraft_graceful_stop_and_waits_for_container_exit(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        runner = FakeDocker(["true", labels, "running", "exited"])
        runtime = DockerRuntime(self.config, runner)
        rcon_commands = []
        runtime._send_rcon = lambda _server_id, _name, command: rcon_commands.append(command)

        with self.assertLogs("craftpilot.runtime_agent", level="INFO") as captured:
            with patch.object(agent_module.time, "sleep", return_value=None) as sleep:
                result = runtime.execute("stop", {"server_id": SERVER_ID, "force": False})

        self.assertTrue(any("server_graceful_stop_requested" in message for message in captured.output))
        self.assertTrue(any("server_stopped" in message for message in captured.output))
        self.assertEqual("STOPPED", result["result_code"])
        self.assertEqual(["stop"], rcon_commands)
        self.assertEqual(1, sleep.call_count)
        self.assertFalse(any(call[:2] == ["docker", "stop"] for call in runner.calls))
        self.assertFalse(any(call[:2] == ["docker", "kill"] for call in runner.calls))

    def test_migration_export_uses_shared_data_mount_for_stopped_container(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        runner = FakeDocker(["false", labels])
        runtime = DockerRuntime(self.config, runner)
        runtime.state = {"node_id": self.config.node_id, "credential": "c" * 64}
        command_id = "05bb4925-1a55-49e0-a940-115f06572ee9"
        migration_id = "a2716e76-3e0c-40eb-9337-8d77a7aa23c6"
        destination_id = "fc94aeda-4379-477a-8bb8-47ad51c0ea76"
        payload = {
            "server_id": SERVER_ID,
            "migration_id": migration_id,
            "source_node_id": self.config.node_id,
            "destination_node_id": destination_id,
            "destination_public_key": "bm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5u",
            "_migration_command_id": command_id,
            "_migration_lease_token": "l" * 64,
        }
        runtime._migration_request = lambda *_args, **_kwargs: {"ok": True}
        expected_result = {"archive_sha256": "a" * 64, "archive_bytes": 23}
        with patch.object(agent_module.migration_transfer, "export_encrypted", return_value=expected_result) as export:
            with patch.object(agent_module.subprocess, "Popen") as popen:
                result = runtime.execute("migration_export", payload)

        self.assertEqual("MIGRATION_EXPORTED", result["result_code"])
        export.assert_called_once()
        self.assertEqual((self.config.data_root / SERVER_ID).resolve(), export.call_args.args[0])
        self.assertNotIn("archive_stream", export.call_args.kwargs)
        popen.assert_not_called()
        self.assertTrue(any(call[:3] == ["docker", "inspect", "--format"] for call in runner.calls))

    def test_migration_transfer_error_is_acknowledged_and_does_not_stop_the_poll_loop(self):
        command_id = "05bb4925-1a55-49e0-a940-115f06572ee9"
        commands = iter([{
            "id": command_id,
            "lease_token": "l" * 64,
            "type": "migration_export",
            "payload": {"server_id": SERVER_ID},
        }, None])
        acknowledgements = []
        runtime = DockerRuntime(self.config, FakeDocker([]))
        store = MemoryStateStore()
        agent = RuntimeNodeAgent(self.config, runtime=runtime, store=store)
        agent.state = {"node_id": self.config.node_id, "credential": "c" * 64, "sequence": 0}
        runtime.state = agent.state

        def request(method, path, body, credential=False):
            self.assertTrue(credential)
            if path.endswith("/commands/claim"):
                return {"command": next(commands)}
            if path.endswith(f"/commands/{command_id}/ack"):
                acknowledgements.append(body)
                return {"ok": True}
            raise AssertionError(f"Unexpected request: {method} {path}")

        agent._request = request
        agent._report_servers = lambda _sequence: None
        agent._sync_file_tree_cache = lambda: None
        transfer_error = agent_module.migration_transfer.MigrationTransferError(
            "server data archive from Docker is invalid",
        )
        with patch.object(runtime, "_migration", side_effect=transfer_error):
            with self.assertLogs("craftpilot.runtime_agent", level="ERROR") as captured:
                agent.poll_once()
                agent.poll_once()

        self.assertEqual(1, len(acknowledgements))
        self.assertFalse(acknowledgements[0]["success"])
        self.assertEqual("RUNTIME_ERROR", acknowledgements[0]["result_code"])
        self.assertIn("server data archive from Docker is invalid", acknowledgements[0]["message"])
        self.assertTrue(any("command_execution_failed" in message for message in captured.output))
        self.assertEqual(2, agent.state["sequence"])

    def test_migration_export_refuses_running_or_foreign_container(self):
        command_id = "05bb4925-1a55-49e0-a940-115f06572ee9"
        migration_id = "a2716e76-3e0c-40eb-9337-8d77a7aa23c6"
        destination_id = "fc94aeda-4379-477a-8bb8-47ad51c0ea76"
        payload = {
            "server_id": SERVER_ID,
            "migration_id": migration_id,
            "source_node_id": self.config.node_id,
            "destination_node_id": destination_id,
            "destination_public_key": "bm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5u",
            "_migration_command_id": command_id,
            "_migration_lease_token": "l" * 64,
        }
        running = DockerRuntime(self.config, FakeDocker(["true", json.dumps({
            "craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID,
        })]))
        with self.assertRaisesRegex(AgentError, "while the server container is running"):
            running.execute("migration_export", payload)

        foreign = DockerRuntime(self.config, FakeDocker(["false", json.dumps({
            "craftpilot.managed": "1", "craftpilot.server_id": "00000000-0000-0000-0000-000000000000",
        })]))
        with self.assertRaisesRegex(AgentError, "not owned"):
            foreign.execute("migration_export", payload)

    def test_migration_stop_uses_docker_graceful_signal_when_rcon_state_is_missing(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        runner = FakeDocker(["true", labels, "running", "exited"])
        runtime = DockerRuntime(self.config, runner)
        runtime.state = {"node_id": self.config.node_id, "credential": "c" * 64}
        runtime.state_store = MemoryStateStore()
        command_id = "05bb4925-1a55-49e0-a940-115f06572ee9"
        migration_id = "a2716e76-3e0c-40eb-9337-8d77a7aa23c6"
        source_id = self.config.node_id
        destination_id = "fc94aeda-4379-477a-8bb8-47ad51c0ea76"
        payload = {
            "server_id": SERVER_ID,
            "migration_id": migration_id,
            "source_node_id": source_id,
            "destination_node_id": destination_id,
            "_migration_command_id": command_id,
            "_migration_lease_token": "l" * 64,
        }

        result = runtime.execute("migration_stop", payload)

        self.assertEqual({"success": True, "result_code": "MIGRATION_STOPPED"}, result)
        self.assertTrue(any(call[:2] == ["docker", "kill"] and "--signal=TERM" in call for call in runner.calls))
        self.assertFalse(any(call[:2] == ["docker", "kill"] and "--signal=KILL" in call for call in runner.calls))
        self.assertFalse(any(call[:2] == ["docker", "stop"] for call in runner.calls))
        self.assertFalse(any(call[:2] == ["docker", "exec"] for call in runner.calls))

    def test_migration_graceful_sigterm_waits_past_old_docker_stop_timeout(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        runner = FakeDocker(["true", labels, "running", "running", "exited"])
        runtime = DockerRuntime(self.config, runner)
        runtime.state = {"node_id": self.config.node_id, "credential": "c" * 64}
        runtime.state_store = MemoryStateStore()
        command_id = "05bb4925-1a55-49e0-a940-115f06572ee9"
        migration_id = "a2716e76-3e0c-40eb-9337-8d77a7aa23c6"
        payload = {
            "server_id": SERVER_ID,
            "migration_id": migration_id,
            "source_node_id": self.config.node_id,
            "destination_node_id": "fc94aeda-4379-477a-8bb8-47ad51c0ea76",
            "_migration_command_id": command_id,
            "_migration_lease_token": "l" * 64,
        }

        with patch.object(agent_module.time, "monotonic", side_effect=[0.0, 6000.0]):
            with patch.object(agent_module.time, "sleep", return_value=None) as sleep:
                result = runtime.execute("migration_stop", payload)

        self.assertEqual("MIGRATION_STOPPED", result["result_code"])
        self.assertEqual(2, sleep.call_count)
        self.assertTrue(any(call[:3] == ["docker", "kill", "--signal=TERM"] for call in runner.calls))
        self.assertFalse(any(call[:3] == ["docker", "kill", "--signal=KILL"] for call in runner.calls))
        self.assertFalse(any(call[:2] == ["docker", "stop"] for call in runner.calls))

    def test_command_cannot_choose_a_container_name(self):
        payload = {"server_id": SERVER_ID, "container": make_spec(self.config)}
        payload["container"]["name"] = "other-container"
        with self.assertRaises(AgentError):
            DockerRuntime(self.config, FakeDocker([])).execute("start", payload)

    def test_command_cannot_choose_a_network_or_mount_point(self):
        for key, value in (("network", "host"), ("mount_path", "/")):
            with self.subTest(key=key):
                spec = make_spec(self.config)
                spec[key] = value
                runner = FakeDocker(["__missing__"])
                with self.assertRaises(AgentError):
                    DockerRuntime(self.config, runner).execute("start", {"server_id": SERVER_ID, "container": spec})
                self.assertFalse(any(call[:3] == ["docker", "run", "--detach"] for call in runner.calls))


if __name__ == "__main__":
    unittest.main()
