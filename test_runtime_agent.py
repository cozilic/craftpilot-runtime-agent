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
        if args[:3] == ["docker", "run", "--detach"]:
            return SimpleNamespace(returncode=0, stdout=self.run_output + "\n", stderr="")
        if args[:2] == ["docker", "start"]:
            return SimpleNamespace(returncode=0, stdout=CONTAINER_NAME, stderr="")
        if args[:2] == ["docker", "stop"] or args[:2] == ["docker", "kill"]:
            return SimpleNamespace(returncode=0, stdout=CONTAINER_NAME, stderr="")
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

    def test_start_creates_only_expected_container_with_limits_and_labels(self):
        (self.config.data_root / SERVER_ID / "existing.txt").unlink()
        runner = FakeDocker(["__missing__", "true", "b" * 64])
        runtime = DockerRuntime(self.config, runner)

        result = runtime.execute("start", {"server_id": SERVER_ID, "container": make_spec(self.config)})

        self.assertEqual("STARTED", result["result_code"])
        docker_run = next(call for call in runner.calls if call[:3] == ["docker", "run", "--detach"])
        self.assertIn("--memory", docker_run)
        self.assertIn("--cpus", docker_run)
        self.assertIn("--restart", docker_run)
        self.assertIn("craftpilot.server_id=" + SERVER_ID, docker_run)
        self.assertIn("eclipse-temurin:21-jre", docker_run)

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
        result = DockerRuntime(self.config, FakeDocker(["__missing__", "true", "b" * 64])).execute(
            "start", {"server_id": SERVER_ID, "container": make_spec(self.config)},
        )
        self.assertEqual("STARTED", result["result_code"])
        runtime = DockerRuntime(self.config, FakeDocker(["__missing__", "true", "b" * 64]))
        runtime.execute("start", {"server_id": SERVER_ID, "container": make_spec(self.config)})
        docker_run = next(call for call in runtime.runner.calls if call[:3] == ["docker", "run", "--detach"])
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

    def test_stop_refuses_container_with_foreign_labels(self):
        runner = FakeDocker(["true", json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": "other"})])

        with self.assertRaises(AgentError):
            DockerRuntime(self.config, runner).execute("stop", {"server_id": SERVER_ID, "force": False})
        self.assertFalse(any(call[:2] == ["docker", "stop"] for call in runner.calls))

    def test_stop_uses_graceful_docker_stop(self):
        labels = json.dumps({"craftpilot.managed": "1", "craftpilot.server_id": SERVER_ID})
        runner = FakeDocker(["true", labels, "false"])

        result = DockerRuntime(self.config, runner).execute("stop", {"server_id": SERVER_ID, "force": False})

        self.assertEqual("STOPPED", result["result_code"])
        stop_call = next(call for call in runner.calls if call[:2] == ["docker", "stop"])
        self.assertIn("--time", stop_call)
        self.assertNotIn("--signal=KILL", stop_call)

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
